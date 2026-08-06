"""AstrBot 插件：opencode 远程任务助手。

通过群聊把任务下发给本机 opencode 执行：
- 执行模式自动选择：opencode serve API 优先，不可用时降级到本地 opencode run 进程
- 实时推送任务进度（开始/工具调用/完成），敏感命令严格 admin_umos 白名单
- 支持任务列表/详情/取消，serve 后台进程托管

安全模型：
- 下发/取消/serve 管理仅限 admin_umos（未配置时全部只读）
- run_work_dir 必填，用于本地 run 模式执行目录
- 本地 run 严格权限（默认不带 --auto），卡在权限 ask 时超时终止并提示
"""

import asyncio
import ctypes
import os
import time
import traceback
from datetime import datetime

from astrbot.api import AstrBotConfig, logger
from astrbot.api.all import MessageChain
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register

from .client import ServeClient, ServeManager
from .runner import RunProcess
from .tracker import (
    ProgressHub,
    Task,
    TaskStore,
    format_task,
    format_task_list,
    now_iso,
    status_label,
)


@register(
    "astrbot_plugin_remote_task",
    "yunxiao258",
    "opencode 远程任务助手：群里下发任务给 opencode 执行",
    "1.0.0",
    repo="https://github.com/yunxiao258/astrbot_plugin_remote_task",
)
class RemoteTaskPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg = config
        self.plugin_dir = os.path.dirname(os.path.abspath(__file__))
        data_dir = os.path.join(self.plugin_dir, "data")
        self.store = TaskStore(
            os.path.join(data_dir, "tasks.json"),
            max_tasks=max(int(config.get("max_tasks", 50)), 1),
        )
        self.store.load()
        self._loop = asyncio.get_event_loop()
        self.hub = ProgressHub(self._on_progress)
        # 执行资源
        self._watch: dict[str, asyncio.Task] = {}  # 任务监视协程
        self._runners: dict[str, RunProcess] = {}  # run 模式进程
        self._task_by_session: dict[str, str] = {}  # serve 会话 → 任务 ID
        self._serve_done: set[str] = set()
        self._serve_activity: dict[str, float] = {}  # 会话最后活动时间
        self._dedup: dict[str, float] = {}  # 跨平台同消息（message_id）去重
        # serve
        serve_url = config.get("serve_url", "") or ""
        serve_user = self._resolve_cred(config.get("serve_username", "") or "")
        serve_pass = self._resolve_cred(config.get("serve_password", "") or "")
        self.serve_client = (
            ServeClient(serve_url, serve_user, serve_pass) if serve_url else None
        )
        self.serve_manager = ServeManager(
            os.path.join(data_dir, "serve.json"),
            opencode_exe=config.get("opencode_exe", "") or None,
        )

    # ---------- 生命周期 ----------

    def on_astrbot_loaded(self):
        """插件加载后启动 serve 事件监听（自动重连）"""
        if not self.serve_client:
            return
        if not self.cfg.get("serve_listen", True):
            return
        asyncio.create_task(self._serve_listener())

    async def _serve_listener(self):
        if self.serve_client:
            await self.serve_client.listen_loop(self._on_serve_event, self._loop)

    # ---------- 命令 ----------

    @filter.command("任务", alias={"task"})
    async def task_cmd(self, event: AstrMessageEvent):
        """任务管理：/任务 <描述>、/任务 列表、/任务 详情 <id>、/任务 取消 <id>、/任务 模式、/任务 serve <状态|启动|停止>"""
        if not self.cfg.get("enabled", True):
            return
        if self._is_self_message(event):
            return
        if self._is_repeat_message(event):
            return
        try:
            text = await self._handle(event)
        except Exception as e:  # noqa: BLE001
            logger.exception("任务命令处理失败")
            text = f"任务命令执行失败: {e}"
        await self._safe_send(event, text)

    @staticmethod
    def _is_self_message(event: AstrMessageEvent) -> bool:
        """机器人自己发出去的消息（回传）不处理，避免递归"""
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if isinstance(raw, dict):
            sender = raw.get("user_id", "")
            self_id = raw.get("self_id", "")
            return bool(sender) and sender == self_id
        return False

    def _is_repeat_message(self, event: AstrMessageEvent) -> bool:
        """同一条群消息经多平台重复到达（相同 message_id）时只处理一次"""
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if not isinstance(raw, dict):
            return False
        mid = str(raw.get("message_id", "") or "")
        if not mid:
            return False
        now = time.time()
        prev = self._dedup.get(mid)
        self._dedup[mid] = now
        if len(self._dedup) > 256:
            stale = [k for k, v in self._dedup.items() if now - v > 10]
            for k in stale:
                self._dedup.pop(k, None)
        if prev is not None and now - prev < 2.0:
            return True
        return False

    @staticmethod
    async def _safe_send(event: AstrMessageEvent, text: str):
        """发送回复，失败仅记录（防止死连接阻塞或异常外抛）"""
        try:
            await event.send(MessageChain([Plain(text)]))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"回复发送失败: {e!r}")

    @staticmethod
    def _strip_self_cmd(text: str, cmd_name: str) -> str:
        """剥掉消息开头的命令名（AstrBot filter 不修改 message_str）"""
        t = text.strip()
        for v in (cmd_name, "/" + cmd_name):
            if t == v:
                return ""
            if t.startswith(v + " "):
                return t[len(v):].strip()
        return t

    @filter.command("任务列表", alias={"任务list"})
    async def task_list_cmd(self, event: AstrMessageEvent):
        """紧凑形式：/任务列表"""
        if not self.cfg.get("enabled", True):
            return
        if self._is_self_message(event):
            return
        if self._is_repeat_message(event):
            return
        await self._safe_send(event, format_task_list(self.store.list_recent(10)))

    @filter.command("任务模式")
    async def task_mode_cmd(self, event: AstrMessageEvent):
        """紧凑形式：/任务模式"""
        if not self.cfg.get("enabled", True):
            return
        if self._is_self_message(event):
            return
        if self._is_repeat_message(event):
            return
        await self._safe_send(event, self._mode_report())

    @filter.command("任务详情")
    async def task_detail_cmd(self, event: AstrMessageEvent):
        """紧凑形式：/任务详情 <id>"""
        if not self.cfg.get("enabled", True):
            return
        if self._is_self_message(event):
            return
        if self._is_repeat_message(event):
            return
        tokens = self._strip_self_cmd(event.message_str, "任务详情").split()
        if not tokens:
            text = "用法: /任务详情 <id>"
        else:
            t = self.store.get(tokens[0])
            text = format_task(t) if t else f"任务不存在: {tokens[0]}"
        await self._safe_send(event, text)

    @filter.command("任务取消")
    async def task_cancel_cmd(self, event: AstrMessageEvent):
        """紧凑形式：/任务取消 <id>"""
        if not self.cfg.get("enabled", True):
            return
        if self._is_self_message(event):
            return
        if self._is_repeat_message(event):
            return
        tokens = self._strip_self_cmd(event.message_str, "任务取消").split()
        if not tokens:
            text = "用法: /任务取消 <id>"
        elif not self._is_admin(event):
            text = self._deny()
        else:
            text = await self._cancel(tokens[0])
        await self._safe_send(event, text)

    @filter.command("任务下发", alias={"任务执行", "任务跑"})
    async def task_submit_cmd(self, event: AstrMessageEvent):
        """紧凑形式：/任务下发 <描述>"""
        if not self.cfg.get("enabled", True):
            return
        if self._is_self_message(event):
            return
        if self._is_repeat_message(event):
            return
        desc = self._strip_self_cmd(event.message_str, "任务下发")
        if desc.startswith("任务执行 "):
            desc = desc[len("任务执行 "):].strip()
        if desc.startswith("任务跑 "):
            desc = desc[len("任务跑 "):].strip()
        if not desc:
            text = "用法: /任务下发 <任务描述>"
        elif not self._is_admin(event):
            text = self._deny()
        else:
            text = await self._submit(desc, str(event.session), self._event_self_id(event))
        await self._safe_send(event, text)

    @filter.command("任务serve")
    async def task_serve_cmd(self, event: AstrMessageEvent):
        """紧凑形式：/任务serve 状态|启动|停止"""
        if not self.cfg.get("enabled", True):
            return
        if self._is_self_message(event):
            return
        if self._is_repeat_message(event):
            return
        if not self._is_admin(event):
            text = self._deny()
        else:
            tokens = self._strip_self_cmd(event.message_str, "任务serve").split()
            text = await self._handle_serve(tokens if tokens else ["状态"])
        await self._safe_send(event, text)

    async def _handle(self, event: AstrMessageEvent) -> str:
        arg = event.message_str.strip()
        # 兼容带命令前缀（如「任务 列表」）与纯参数（如「列表」）两种形式
        for prefix in ("任务", "/任务", "task", "/task"):
            if arg == prefix:
                arg = ""
                break
            if arg.startswith(prefix + " "):
                arg = arg[len(prefix):].strip()
                break
        tokens = arg.split()
        if not tokens:
            return self._usage()
        is_admin = self._is_admin(event)
        cmd = tokens[0]

        if cmd == "列表":
            return format_task_list(self.store.list_recent(10))
        if cmd == "详情":
            if len(tokens) < 2:
                return "用法: /任务 详情 <id>"
            t = self.store.get(tokens[1])
            return format_task(t) if t else f"任务不存在: {tokens[1]}"
        if cmd == "模式":
            return self._mode_report()
        if cmd == "serve":
            if not is_admin:
                return self._deny()
            return await self._handle_serve(tokens[1:] if len(tokens) > 1 else ["状态"])
        if cmd == "取消":
            if not is_admin:
                return self._deny()
            if len(tokens) < 2:
                return "用法: /任务 取消 <id>"
            return await self._cancel(tokens[1])
        if cmd in ("下发", "执行", "跑"):
            if not is_admin:
                return self._deny()
            desc = " ".join(tokens[1:])
            if not desc:
                return "用法: /任务 下发 <任务描述>（或直接 /任务 <描述>）"
            return await self._submit(desc, str(event.session), self._event_self_id(event))
        # 默认整条视为任务描述
        if not is_admin:
            return self._deny()
        return await self._submit(" ".join(tokens), str(event.session), self._event_self_id(event))

    def _usage(self) -> str:
        return (
            "任务命令可用:\n"
            "/任务 <描述> 下发任务（自动选执行模式）\n"
            "/任务列表 /任务详情 <id> /任务取消 <id>\n"
            "/任务模式 /任务 serve 状态|启动|停止"
        )

    # ---------- 权限与目标 ----------

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        """是否管理员会话（admin_umos 白名单）"""
        umos = self._admin_umos()
        return str(event.session) in umos if umos else False

    def _admin_umos(self) -> list[str]:
        v = self.cfg.get("admin_umos", "")
        if isinstance(v, str):
            return [x.strip() for x in v.split(",") if x.strip()]
        return list(v or [])

    def _deny(self) -> str:
        umos = self._admin_umos()
        if not umos:
            return (
                "本插件未配置管理员白名单（admin_umos），管理命令不可用。\n"
                "请在插件配置中填写 admin_umos（如 default:GroupMessage:1234567890）后重启 AstrBot。"
            )
        return "你没有执行此命令的权限（不在 admin_umos 白名单内）"

    @staticmethod
    def _event_self_id(event: AstrMessageEvent) -> str:
        """从事件原始消息中提取 self_id（aiocqhttp 平台多连接路由需要）"""
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if isinstance(raw, dict):
            return str(raw.get("self_id", "") or "")
        return ""

    def _broadcast_targets(self, creator_umo: str) -> list[str]:
        v = self.cfg.get("broadcast_umo", "")
        if isinstance(v, str):
            lst = [x.strip() for x in v.split(",") if x.strip()]
        else:
            lst = list(v or [])
        return lst or ([creator_umo] if creator_umo else [])

    def _first_self_id(self, platform_name: str) -> str:
        """取平台首个已连接 OneBot 客户端的 self_id（多连接路由需要）"""
        try:
            plat = next(
                (
                    p
                    for p in self.context.platform_manager.platform_insts
                    if p.meta().id == platform_name
                ),
                None,
            )
            bot = getattr(plat, "bot", None)
            clients = getattr(bot, "_wsr_api_clients", None) or getattr(bot, "_api_clients", None)
            if isinstance(clients, dict) and clients:
                return str(next(iter(clients)))
        except Exception:  # noqa: BLE001
            pass
        return ""

    async def _send_chain(self, umo: str, chain, self_id: str) -> bool:
        """发送消息链；aiocqhttp 平台带 self_id 直发（多连接时缺 self_id 会 ApiNotAvailable）"""
        try:
            from astrbot.core.platform.message_session import MessageSesion
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                AiocqhttpMessageEvent,
            )
            from astrbot.api.all import MessageType

            session = MessageSesion.from_str(umo)
            plat = next(
                (
                    p
                    for p in self.context.platform_manager.platform_insts
                    if p.meta().id == session.platform_name
                ),
                None,
            )
            if plat is not None and getattr(plat, "bot", None) is not None:
                sid = self_id or self._first_self_id(session.platform_name)
                if sid:
                    seg = await AiocqhttpMessageEvent._parse_onebot_json(chain)
                    if not seg:
                        return True
                    if session.message_type == MessageType.GROUP_MESSAGE:
                        await plat.bot.send_group_msg(
                            group_id=int(session.session_id),
                            message=seg,
                            self_id=sid,
                        )
                    elif session.message_type == MessageType.FRIEND_MESSAGE:
                        await plat.bot.send_private_msg(
                            user_id=int(session.session_id),
                            message=seg,
                            self_id=sid,
                        )
                    return True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"self_id 直发失败（回退 send_message）: {e!r}")
        return await self.context.send_message(umo, chain)

    async def _broadcast(self, text: str, creator_umo: str = "", creator_self_id: str = ""):
        for umo in self._broadcast_targets(creator_umo):
            try:
                await self._send_chain(umo, MessageChain([Plain(text)]), creator_self_id)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"播报到 {umo} 失败: {e!r}\n{traceback.format_exc()}")

    async def _on_progress(self, task_id: str, kind: str, text: str):
        """进度事件（限流后）广播到群"""
        t = self.store.get(task_id)
        if not t:
            return
        if not self.cfg.get("progress_push", True):
            return
        label = {
            "text": "回复",
            "tool": "工具调用",
            "permission": "权限",
            "done": "完成",
        }.get(kind, kind)
        prefix = f"任务 #{task_id} [{label}]\n"
        lines = text.splitlines()
        shown = lines[0] if lines else text
        if len(shown) > 160:
            shown = shown[:157] + "..."
        content = shown if label in ("回复", "done") else f"{shown}"
        await self._broadcast(prefix + content, t.creator_umo, t.creator_self_id)

    # ---------- serve 进程管理 ----------

    async def _handle_serve(self, tokens: list[str]) -> str:
        sub = tokens[0] if tokens else "状态"
        if sub in ("状态", "status"):
            return self.serve_manager.status() + "\n" + self._probe_line()
        if sub in ("启动", "start"):
            port = int(self.cfg.get("serve_port", 4096))
            host = self.cfg.get("serve_host", "127.0.0.1") or "127.0.0.1"
            pid, msg = self.serve_manager.spawn(port, host)
            # 进程启动需数秒，轮询探测避免误报"连接失败"
            line = await asyncio.to_thread(self._probe_with_retry)
            return f"serve {msg}（PID {pid or '无'}）\n" + line
        if sub in ("停止", "stop"):
            return self.serve_manager.stop()
        return "用法: /任务 serve 状态|启动|停止"

    def _probe_with_retry(self, tries: int = 8, delay: float = 1.0) -> str:
        if not self.serve_client:
            return "serve 客户端未配置（serve_url 为空）"
        for _ in range(tries):
            if self.serve_client.probe():
                return "serve 可用: 是（连接成功）"
            time.sleep(delay)
        return "serve 可用: 否（连接失败，任务将走本地 run）"

    def _probe_line(self) -> str:
        if not self.serve_client:
            return "serve 客户端未配置（serve_url 为空）"
        return "serve 可用: " + ("是（连接成功）" if self.serve_client.probe() else "否（连接失败，任务将走本地 run）")

    def _mode_report(self) -> str:
        mode, reason = self._decide_mode()
        work = self._work_dir()
        return (
            f"当前执行模式: {mode}\n原因: {reason}\n工作目录: {work or '未配置'}"
        )

    # ---------- 模式决策 ----------

    def _decide_mode(self) -> tuple[str, str]:
        if self.serve_client and self.serve_client.probe():
            return "serve", "serve 服务可用"
        work = self._work_dir()
        if not work:
            return "不可执行", "serve 不可用且 run_work_dir 未配置"
        if not os.path.isdir(work):
            return "不可执行", f"serve 不可用且工作目录不存在: {work}"
        return "run", "serve 不可用，降级本地 run"

    def _work_dir(self) -> str:
        rel = self.cfg.get("run_work_dir", "") or ""
        if not rel:
            return ""
        if not os.path.isabs(rel):
            rel = os.path.join(self.plugin_dir, rel)
        rel = os.path.expandvars(os.path.expanduser(rel))
        return rel

    # ---------- 任务执行 ----------

    async def _submit(self, desc: str, creator_umo: str, creator_self_id: str = "") -> str:
        mode, reason = self._decide_mode()
        if mode == "不可执行":
            return f"无法下发任务: {reason}"
        task = self.store.add(
            Task(
                id=self.store.next_id(),
                desc=desc,
                creator_umo=creator_umo,
                creator_self_id=creator_self_id,
                mode=mode,
            )
        )
        logger.info(f"任务 #{task.id} 下发（{mode}）: {desc}")
        await self._broadcast(
            f"[任务] {creator_umo} 下发任务 #{task.id}（{mode}）: {desc}",
            creator_umo,
            creator_self_id,
        )
        watcher = asyncio.create_task(self._execute(task))
        self._watch[task.id] = watcher
        return (
            f"任务 #{task.id} 已受理（模式: {'serve API' if mode == 'serve' else '本地 run'}）\n"
            f"说明: {reason}\n"
            f"命令: {desc}"
        )

    async def _execute(self, task: Task):
        try:
            if task.mode == "serve":
                await self._execute_serve(task)
            else:
                await self._execute_run(task)
        except asyncio.CancelledError:
            # 被管理员取消：尽力终止遗留 run 进程后退出
            runner = self._runners.pop(task.id, None)
            if runner:
                try:
                    await runner.cancel()
                except Exception:  # noqa: BLE001
                    pass
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception(f"任务 #{task.id} 执行异常")
            self.store.update(task.id, status="failed", error=str(e), finished_at=now_iso())
            await self._broadcast(f"任务 #{task.id} 执行异常: {e}", task.creator_umo, task.creator_self_id)
        finally:
            self._watch.pop(task.id, None)

    async def _execute_run(self, task: Task):
        if self._get_status(task.id) == "aborted":
            return
        runner = RunProcess(
            task_id=task.id,
            desc=task.desc,
            work_dir=self._work_dir(),
            timeout_no_output=int(self.cfg.get("run_no_output_timeout_seconds", 120)),
            auto_approve=bool(self.cfg.get("run_auto_approve", False)),
            on_event=self._on_run_event,
            opencode_exe=self.cfg.get("opencode_exe", "") or None,
        )
        self._runners[task.id] = runner
        ok = await runner.start()
        if not ok:
            self.store.update(task.id, status="failed", error=runner.error, finished_at=now_iso())
            await self._broadcast(f"任务 #{task.id} 启动失败: {runner.error}", task.creator_umo, task.creator_self_id)
            return
        self.store.update(task.id, status="running", pid=runner.pid)
        await self._broadcast(
            f"任务 #{task.id} 开始执行（本地 run，PID {runner.pid}）", task.creator_umo, task.creator_self_id
        )
        status = await runner.wait()
        t = self.store.get(task.id)
        summary = (t.summary if t else "") or (runner.error or "")
        if status == "done":
            self.store.update(task.id, status="done", summary=summary, finished_at=now_iso())
            tail = f"\n结果: {self._clip(summary)}" if summary.strip() else "\n结果: （无文本输出）"
            await self._broadcast(
                f"任务 #{task.id} 完成{tail}", task.creator_umo, task.creator_self_id
            )
        elif status == "aborted":
            self.store.update(task.id, status="aborted", finished_at=now_iso())
            await self._broadcast(f"任务 #{task.id} 已取消", task.creator_umo, task.creator_self_id)
        else:
            self.store.update(task.id, status="failed", error=runner.error, finished_at=now_iso())
            await self._broadcast(f"任务 #{task.id} 失败: {self._clip(runner.error)}", task.creator_umo, task.creator_self_id)

    async def _on_run_event(self, task_id: str, kind: str, text: str):
        """run 进程事件 → 记录 + 限流推送"""
        self.store.add_item(task_id, {"t": now_iso(), "kind": kind, "text": text})
        if kind == "text":
            self.store.update(task_id, summary=text[:2000])
        elif kind == "permission":
            await self._broadcast(
                f"任务 #{task_id} 等待权限批准（工具: {text}）\n"
                "可用 status_sync 插件的群审批放行，或等待超时中止",
                self._creator_of(task_id),
                self._creator_self_of(task_id),
            )
        self.hub.emit(task_id, kind, text)

    def _creator_of(self, task_id: str) -> str:
        t = self.store.get(task_id)
        return t.creator_umo if t else ""

    def _creator_self_of(self, task_id: str) -> str:
        t = self.store.get(task_id)
        return t.creator_self_id if t else ""

    def _get_status(self, task_id: str) -> str:
        t = self.store.get(task_id)
        return t.status if t else ""

    async def _execute_serve(self, task: Task):
        if self._get_status(task.id) == "aborted":
            return
        sid = await asyncio.to_thread(self.serve_client.create_session, task.desc)
        if not sid:
            self.store.update(task.id, status="failed", error="创建 serve 会话失败", finished_at=now_iso())
            await self._broadcast(f"任务 #{task.id} 创建会话失败", task.creator_umo, task.creator_self_id)
            return
        self._task_by_session[sid] = task.id
        if self._get_status(task.id) == "aborted":
            self._task_by_session.pop(sid, None)
            return
        if not await asyncio.to_thread(self.serve_client.send_prompt, sid, task.desc):
            self._task_by_session.pop(sid, None)
            self.store.update(task.id, status="failed", error="serve 下发 prompt 失败", finished_at=now_iso())
            await self._broadcast(f"任务 #{task.id} 下发 prompt 失败", task.creator_umo, task.creator_self_id)
            return
        self.store.update(task.id, status="running", session_id=sid)
        await self._broadcast(
            f"任务 #{task.id} 开始执行（serve API，会话 {sid[:8]}）", task.creator_umo, task.creator_self_id
        )
        timeout = max(30, int(self.cfg.get("run_no_output_timeout_seconds", 120)) * 6)
        idle_limit = max(30, int(self.cfg.get("serve_idle_complete_seconds", 60)))
        deadline = self._loop.time() + timeout
        completed = False
        last_poll = 0.0
        while self._loop.time() < deadline:
            if self._get_status(task.id) == "aborted":
                break
            if sid in self._serve_done:
                completed = True
                break
            if self._loop.time() - last_poll >= 5:
                last_poll = self._loop.time()
                if await asyncio.to_thread(self.serve_client.is_finished, sid):
                    completed = True
                    break
            if sid in self._serve_activity and (
                self._loop.time() - self._serve_activity[sid] > idle_limit
            ):
                # 长时间无会话事件，视为执行完毕
                completed = True
                break
            await asyncio.sleep(2)
        self._task_by_session.pop(sid, None)
        self._serve_activity.pop(sid, None)
        self._serve_done.discard(sid)
        if self._get_status(task.id) == "aborted":
            return
        text = await asyncio.to_thread(
            self.serve_client.get_message_text, sid, int(self.cfg.get("result_max_chars", 500))
        )
        if completed:
            status = "done"
            summary = text
        else:
            status = "failed"
            summary = ""
        if status == "done":
            self.store.update(task.id, status="done", summary=summary, finished_at=now_iso())
            tail = f"\n结果: {self._clip(summary)}" if summary.strip() else "\n结果: （无文本输出）"
            await self._broadcast(f"任务 #{task.id} 完成{tail}", task.creator_umo, task.creator_self_id)
        else:
            self.store.update(task.id, status="failed", error="serve 会话超时未完成", finished_at=now_iso())
            await self._broadcast(
                f"任务 #{task.id} 超时未完成（无完成事件，已视为失败）", task.creator_umo, task.creator_self_id
            )

    async def _on_serve_event(self, kind: str, session_id: str, text: str):
        """serve 全局事件 → 只处理本插件任务会话"""
        task_id = self._task_by_session.get(session_id)
        if not task_id:
            return
        if kind == "done":
            self._serve_done.add(session_id)
        else:
            # 任何非结束事件都视为会话活动（用于空闲判完成）
            self._serve_activity[session_id] = self._loop.time()
        self.store.add_item(task_id, {"t": now_iso(), "kind": kind, "text": text})
        if kind == "text":
            self.store.update(task_id, summary=text[:2000])
        elif kind == "permission":
            await self._broadcast(
                f"任务 #{task_id} 等待权限批准（{text}）\n可用 status_sync 插件的群审批放行",
                self._creator_of(task_id),
                self._creator_self_of(task_id),
            )
        self.hub.emit(task_id, kind, text)

    # ---------- 取消 ----------

    async def _cancel(self, task_id: str) -> str:
        t = self.store.get(task_id)
        if not t:
            return f"任务不存在: {task_id}"
        if t.status not in ("pending", "running"):
            return f"任务 #{task_id} 已结束（{status_label(t.status)}），无需取消"
        if t.mode == "serve" and t.session_id:
            ok = await asyncio.to_thread(self.serve_client.abort, t.session_id)
            self._task_by_session.pop(t.session_id, None)
            self._serve_done.discard(t.session_id)
            if not ok:
                return "取消请求已发送（serve 可能未响应 abort）"
        elif t.mode == "run" and t.pid:
            runner = self._runners.get(task_id)
            if runner:
                await runner.cancel()
            else:
                return f"未找到任务 #{task_id} 的运行进程（可能已结束）"
        # 取消监视协程，防止 pending 阶段竞态把已取消任务改写成其他状态
        watcher = self._watch.get(task_id)
        if watcher and not watcher.done():
            watcher.cancel()
        self.store.update(task_id, status="aborted", finished_at=now_iso())
        await self._broadcast(f"任务 #{task_id} 已由管理员取消", t.creator_umo, t.creator_self_id)
        return f"任务 #{task_id} 已取消"

    # ---------- 工具 ----------

    @staticmethod
    def _clip(text: str, limit: int = 500) -> str:
        text = (text or "").strip()
        if not text:
            return "（无输出）"
        if len(text) > limit:
            return text[: limit - 3].rstrip() + "..."
        return text

    def _resolve_cred(self, value: str) -> str:
        """支持 env:/file:/base64:/dpapi: 前缀的凭据解析"""
        if not value:
            return ""
        if value.startswith("env:"):
            return os.environ.get(value[4:], "")
        if value.startswith("file:"):
            try:
                with open(value[5:], "r", encoding="utf-8") as fh:
                    return fh.read().strip()
            except OSError:
                return ""
        if value.startswith("base64:"):
            import base64

            try:
                return base64.b64decode(value[7:]).decode()
            except Exception:  # noqa: BLE001
                return ""
        if value.startswith("dpapi:") and os.name == "nt":
            import base64

            raw = base64.b64decode(value[6:])
            try:
                return self._dpapi_unprotect(raw)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"dpapi 解密失败: {e}")
                return ""
        return value

    @staticmethod
    def _dpapi_unprotect(data: bytes) -> str:
        """Windows DPAPI 解密（当前用户）"""
        import ctypes.wintypes as wt

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

        blob = DATA_BLOB(len(data), ctypes.cast(data, ctypes.POINTER(ctypes.c_char)))
        out = DATA_BLOB()
        if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob), None, None, None, None, 1, ctypes.byref(out)
        ):
            raise ctypes.WinError()
        raw = ctypes.string_at(out.pbData, out.cbData)
        ctypes.windll.kernel32.LocalFree(out.pbData)
        return raw.decode("utf-8")
