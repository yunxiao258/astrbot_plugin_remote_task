"""模式 A：opencode serve HTTP API 封装 + serve 后台进程托管。

API（opencode server）：
- GET  /global/health                 健康探测
- POST /session                      创建会话
- POST /session/:id/prompt_async     下发异步 prompt
- GET  /session/:id/message          拉取会话消息
- POST /session/:id/abort            中止会话
- GET  /global/event                 SSE 事件流

serve 进程由插件托管（/任务 serve 启动/停止），PID 记录在 data/serve.json。
"""

import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from typing import Callable, Coroutine

from astrbot.api import logger

from .runner import _extract_text


# ---------------- SSE 解析 ----------------

def parse_sse_payload(event: str, data: str) -> tuple[str, str, str, dict] | None:
    """解析一条 SSE 事件为 (kind, session_id, text, meta)；非任务事件返回 None。

    kind: text / tool / permission / done
    meta: 附加结构化信息（permission 事件含 permission_id 等），无附加信息为空 dict
    """
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    # "payload": {...} 包裹解包
    if "payload" in payload and isinstance(payload["payload"], dict):
        payload = payload["payload"]
    if not isinstance(payload, dict):
        return None
    ev_name = str(event or "")
    pl_type = str(payload.get("type") or "")
    # "sync" 包装：把内部 syncEvent.data 作为主载荷（type 带 ".1" 序号）
    if pl_type == "sync" and isinstance(payload.get("syncEvent"), dict):
        inner = payload["syncEvent"]
        inner_data = inner.get("data")
        if isinstance(inner_data, dict):
            payload = inner_data
            pl_type = str(inner_data.get("type") or payload.get("type") or "")
    etype = ev_name or pl_type
    full = ev_name + " " + pl_type
    # 真实事件里 sessionID 位于 properties 内（顶层一般没有）
    props = payload.get("properties")
    if not isinstance(props, dict):
        props = None
    session_id = (
        payload.get("sessionID")
        or payload.get("session_id")
        or payload.get("sessionId")
        or (props.get("sessionID") if props else None)
        or (props.get("session_id") if props else None)
        or (props.get("sessionId") if props else None)
    )
    if not session_id:
        return None
    sid = str(session_id)
    # message.part.updated：part 内声明类型（tool / text）优先分发
    if full.endswith("message.part.updated") or "message.part.updated" in full:
        props = payload.get("properties")
        part = props.get("part") if isinstance(props, dict) else None
        if isinstance(part, dict):
            ptype = str(part.get("type") or "")
            if ptype == "tool":
                tool = part.get("tool") or "工具"
                state = part.get("state") or {}
                inp = state.get("input") or {}
                if isinstance(inp, dict):
                    arg_text = " ".join(
                        f"{k}={v}" for k, v in inp.items()
                        if k in ("command", "file_path", "path", "pattern", "query")
                    )
                else:
                    arg_text = str(inp)[:120]
                return ("tool", sid, f"{tool} {arg_text}".strip(), {})
            if ptype == "text":
                text = str(part.get("text") or "").strip()
                if text:
                    return ("text", sid, text, {})
            if ptype == "reasoning":
                return None
    if "message.updated" in full:
        text = _extract_text(payload)
        if text:
            return ("text", sid, text, {})
        return None
    if "tool" in full:
        tool = payload.get("toolName") or payload.get("name") or payload.get("tool") or "工具"
        args = payload.get("toolInput") or payload.get("input") or payload.get("args") or {}
        if isinstance(args, dict):
            arg_text = " ".join(
                f"{k}={v}" for k, v in args.items()
                if k in ("command", "file_path", "path", "pattern", "query")
            )
        else:
            arg_text = str(args)[:120]
        return ("tool", sid, f"{tool} {arg_text}".strip(), {})
    if "permission" in full:
        meta: dict = {}
        detail = _extract_text(payload) or ""
        props = payload.get("properties")
        if isinstance(props, dict):
            meta = {
                "permission_id": str(props.get("id") or ""),
                "permission": str(props.get("permission") or ""),
                "patterns": list(props.get("patterns") or []),
                "always": list(props.get("always") or []),
            }
            md = props.get("metadata")
            if isinstance(md, dict):
                meta["filepath"] = str(md.get("filepath") or "")
            if not detail:
                detail = " ".join(
                    filter(None, (meta.get("permission"), meta.get("filepath")))
                )
        else:
            old = payload.get("permission")
            if isinstance(old, dict):
                detail = old.get("pattern", "") or old.get("description", "")
        return ("permission", sid, f"请求权限: {detail}" if detail else "请求权限", meta)
    if "finished" in full or "complete" in full:
        text = _extract_text(payload)
        return ("done", sid, text, {})
    return None


# ---------------- serve API 客户端 ----------------

class ServeClient:
    """连接 opencode serve HTTP API 执行任务"""

    def __init__(self, base_url: str, username: str = "", password: str = ""):
        self.base_url = base_url.rstrip("/")
        self.auth = None
        if username or password:
            raw = f"{username or 'opencode'}:{password}".encode()
            self.auth = {"Authorization": "Basic " + base64.b64encode(raw).decode()}
        self._requests = None

    def _get_requests(self):
        if self._requests is None:
            import requests

            self._requests = requests
        return self._requests

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.auth:
            h.update(self.auth)
        return h

    @staticmethod
    def _no_proxy() -> dict:
        """本地 serve 直连，禁用代理避免被 HTTP_PROXY 挂起"""
        return {"http": None, "https": None}

    def _get(self, url: str, timeout: int = 15, **kw):
        return self._get_requests().get(
            url, timeout=timeout, proxies=self._no_proxy(), **kw
        )

    def _post(self, url: str, timeout: int = 15, **kw):
        return self._get_requests().post(
            url, timeout=timeout, proxies=self._no_proxy(), **kw
        )

    def probe(self) -> bool:
        """探测 serve 是否可用"""
        try:
            r = self._get(
                f"{self.base_url}/global/health", timeout=5, headers=self._headers()
            )
            return r.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    def create_session(self, title: str = "") -> str | None:
        """创建会话，返回 sessionID；失败返回 None"""
        try:
            body = {"title": title[:100]} if title else {}
            r = self._post(
                f"{self.base_url}/session", json=body, timeout=15, headers=self._headers()
            )
            if r.status_code == 400 and title:
                # 部分版本不接受 title，回退空 body
                r = self._post(
                    f"{self.base_url}/session", json={}, timeout=15, headers=self._headers()
                )
            if r.status_code not in (200, 201):
                # 部分版本创建成功后返回 2xx 其他码（如 204）
                if not (200 <= r.status_code < 300):
                    logger.warning(f"创建会话失败: HTTP {r.status_code}")
                    return None
            data = r.json()
            sid = (
                data.get("id") or data.get("sessionID") or data.get("session_id")
                or data.get("sessionId")
            )
            return str(sid) if sid else None
        except Exception as e:  # noqa: BLE001
            logger.warning(f"创建会话异常: {e}")
            return None

    def send_prompt(self, session_id: str, prompt: str) -> bool:
        """异步下发 prompt 到会话"""
        try:
            r = self._post(
                f"{self.base_url}/session/{session_id}/prompt_async",
                json={"parts": [{"type": "text", "text": prompt}]},
                timeout=15,
                headers=self._headers(),
            )
            return 200 <= r.status_code < 300
        except Exception as e:  # noqa: BLE001
            logger.warning(f"下发 prompt 异常: {e}")
            return False

    def is_finished(self, session_id: str) -> bool:
        """轮询兜底：最后一条 assistant 消息 finish=stop 即视为完成"""
        try:
            r = self._get(
                f"{self.base_url}/session/{session_id}/message",
                timeout=10,
                headers=self._headers(),
            )
            if not (200 <= r.status_code < 300):
                return False
            msgs = r.json()
            if not isinstance(msgs, list):
                return False
            for m in reversed(msgs):
                if not isinstance(m, dict):
                    continue
                info = m.get("info") or {}
                role = info.get("role", "")
                if role and role != "assistant":
                    continue
                if info.get("finish") == "stop":
                    return True
                return False
            return False
        except Exception as e:  # noqa: BLE001
            logger.warning(f"轮询会话完成状态异常: {e}")
            return False

    def get_message_text(self, session_id: str, max_chars: int = 2000) -> str:
        """拉取会话最新 assistant 文本（serve /message 返回消息列表）"""
        try:
            r = self._get(
                f"{self.base_url}/session/{session_id}/message",
                timeout=15,
                headers=self._headers(),
            )
            if not (200 <= r.status_code < 300):
                return ""
            msgs = r.json()
            if isinstance(msgs, dict):
                msgs = msgs.get("messages") or msgs.get("items") or []
            if not isinstance(msgs, list):
                return ""
            out = []
            for m in msgs:
                if not isinstance(m, dict):
                    continue
                role = (m.get("info") or {}).get("role", "")
                if role and role != "assistant":
                    continue
                for p in m.get("parts") or []:
                    if not isinstance(p, dict):
                        continue
                    if p.get("type") == "tool":
                        continue
                    t = str(p.get("text") or "").strip()
                    if t:
                        out.append(t)
            if not out:
                return _extract_text(msgs)[:max_chars]
            return "\n".join(out)[:max_chars]
        except Exception as e:  # noqa: BLE001
            logger.warning(f"拉取会话消息异常: {e}")
            return ""

    def abort(self, session_id: str) -> bool:
        """中止会话"""
        try:
            r = self._get_requests().post(
                f"{self.base_url}/session/{session_id}/abort",
                timeout=10,
                headers=self._headers(),
                proxies=self._no_proxy(),
            )
            return 200 <= r.status_code < 300
        except Exception as e:  # noqa: BLE001
            logger.warning(f"中止会话异常: {e}")
            return False

    def respond_permission(
        self, session_id: str, permission_id: str, response: str = "once"
    ) -> bool:
        """响应挂起的权限请求（serve 审批 API）。

        response: once=仅本次同意 / always=会话内按模式记住并同意 / reject=拒绝
        """
        try:
            r = self._post(
                f"{self.base_url}/session/{session_id}/permissions/{permission_id}",
                json={"response": response},
                timeout=10,
                headers=self._headers(),
            )
            if not (200 <= r.status_code < 300):
                logger.warning(
                    f"权限审批 HTTP {r.status_code}（permission={permission_id}）"
                )
                return False
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"权限审批异常: {e}")
            return False

    async def listen_loop(self, on_event: Callable[[str, str, str], Coroutine], loop=None):
        """SSE 事件监听循环（断线自动重连）"""
        while True:
            try:
                await asyncio.to_thread(self._listen_once, on_event, loop)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning(f"serve 事件监听异常: {e}")
            await asyncio.sleep(5)

    def _listen_once(self, on_event, loop):
        r = self._get_requests().get(
            f"{self.base_url}/global/event",
            stream=True,
            timeout=None,
            headers=self._headers(),
            proxies=self._no_proxy(),
        )
        event = ""
        data_lines = []
        for raw in r.iter_lines(decode_unicode=True):
            if raw is None:
                continue
            if raw.startswith("event:"):
                event = raw[len("event:"):].strip()
            elif raw.startswith("data:"):
                data_lines.append(raw[len("data:"):].strip())
            elif raw == "":
                if event and data_lines:
                    ev = parse_sse_payload(event, "\n".join(data_lines))
                    if ev:
                        kind, sid, text, meta = ev
                        if loop is not None:
                            asyncio.run_coroutine_threadsafe(
                                on_event(kind, sid, text, meta), loop
                            )
                        else:
                            asyncio.run(on_event(kind, sid, text, meta))
                event = ""
                data_lines = []


# ---------------- serve 进程托管 ----------------

class ServeManager:
    """托管 opencode serve 后台进程，PID 记录在 data/serve.json"""

    def __init__(self, data_file: str, opencode_exe: str | None = None):
        self.data_file = data_file
        self.opencode_exe = opencode_exe
        self._info: dict = {}

    def _ensure_dir(self):
        parent = os.path.dirname(self.data_file)
        if parent:
            os.makedirs(parent, exist_ok=True)

    def load(self):
        try:
            with open(self.data_file, "r", encoding="utf-8") as fh:
                self._info = json.load(fh)
        except (OSError, json.JSONDecodeError):
            self._info = {}
        if not isinstance(self._info, dict):
            self._info = {}

    def save(self):
        self._ensure_dir()
        try:
            with open(self.data_file, "w", encoding="utf-8") as fh:
                json.dump(self._info, fh, ensure_ascii=False, indent=2)
        except OSError as e:
            logger.warning(f"保存 serve 状态失败: {e}")

    def _pid_running(self, pid: int) -> bool:
        """跨平台判断进程是否存活"""
        if not pid:
            return False
        try:
            if sys.platform == "win32":
                r = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                    capture_output=True, text=True, timeout=10,
                )
                return str(pid) in r.stdout
            os.kill(pid, 0)
            return True
        except (OSError, subprocess.SubprocessError):
            return False

    def spawn(self, port: int, host: str = "127.0.0.1", cmd_override: list[str] | None = None) -> tuple[int, str]:
        """启动 serve 后台进程，返回 (pid, 日志路径)；失败返回 (0, 原因)"""
        self.load()
        if self._pid_running(int(self._info.get("pid", 0) or 0)):
            return int(self._info["pid"]), "已在运行"
        exe = self.opencode_exe or shutil.which("opencode") or "opencode"
        log_path = os.path.join(os.path.dirname(self.data_file), "serve.log")
        try:
            with open(log_path, "w", encoding="utf-8") as logf:
                cmd = (
                    list(cmd_override)
                    if cmd_override
                    else [exe, "serve", "--port", str(port), "--hostname", host]
                )
                p = subprocess.Popen(
                    cmd,
                    stdout=logf, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                )
        except OSError as e:
            return 0, f"启动失败: {e}"
        self._info = {
            "pid": p.pid,
            "port": port,
            "host": host,
            "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "log": log_path,
        }
        self.save()
        return p.pid, "已启动"

    def stop(self) -> str:
        """停止 serve 进程"""
        self.load()
        pid = int(self._info.get("pid", 0) or 0)
        if not pid:
            return "没有记录到 serve 进程"
        if not self._pid_running(pid):
            self._info = {}
            self.save()
            return "serve 进程已退出"
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, timeout=15)
            else:
                os.kill(pid, 15)
        except (OSError, subprocess.SubprocessError) as e:
            return f"停止失败: {e}"
        self._info = {}
        self.save()
        return "已停止"

    def status(self) -> str:
        """返回 serve 进程状态文本"""
        self.load()
        pid = int(self._info.get("pid", 0) or 0)
        if not pid:
            return "插件未托管 opencode serve 进程"
        running = self._pid_running(pid)
        base = (
            f"serve PID {pid}\n启动: {self._info.get('started_at', '?')}\n"
            f"端口: {self._info.get('port', '?')} 主机: {self._info.get('host', '?')}\n"
            f"日志: {self._info.get('log', '?')}"
        )
        return base + ("\n状态: 运行中" if running else "\n状态: 已退出")