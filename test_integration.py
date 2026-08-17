"""astrbot_plugin_remote_task 集成测试：命令分发、admin 白名单、任务下发链路。

运行：python test_integration.py
需要 venv 中的 astrbot 包（@register 依赖），不启动 opencode serve。
"""

import asyncio
import os
import sys
import tempfile

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_PLUGIN_DIR))

from astrbot_plugin_remote_task.client import ServeManager
from astrbot_plugin_remote_task.main import RemoteTaskPlugin
from astrbot_plugin_remote_task.scheduler import ScheduleEntry, ScheduleStore, ScheduleManager
from astrbot_plugin_remote_task.tracker import ProgressHub, Task, TaskStore

PASS = 0
FAIL = 0

ADMIN_UMO = "default:GroupMessage:1234567890"


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS: {name}")
    else:
        FAIL += 1
        print(f"  FAIL: {name}")


class FakeCfg:
    def __init__(self, **kw):
        self.kw = kw

    def get(self, key, default=None):
        return self.kw.get(key, default)


class FakeServeClient:
    """记录 respond_permission 调用的 serve 客户端替身"""

    def __init__(self):
        self.calls = []
        self.fail = False

    def respond_permission(self, session_id, permission_id, response):
        self.calls.append((session_id, permission_id, response))
        return not self.fail


class FakeContext:
    def __init__(self):
        self.sent = []

    async def send_message(self, umo, chain):
        self.sent.append((umo, chain))
        return True


class FakeEvent:
    def __init__(self, message_str, session=ADMIN_UMO, sender_id=""):
        self.message_str = message_str
        self._session = session
        self._sender_id = sender_id
        self.sent_text = None

    def get_sender_id(self):
        return self._sender_id

    @property
    def session(self):
        class _S:
            def __init__(self, v):
                self.v = v

            def __str__(self):
                return self.v

        return _S(self._session)

    async def send(self, chain):
        texts = [c.text for c in chain.chain if hasattr(c, "text")]
        self.sent_text = "".join(texts)


_BROADCAST_LOG = []


async def fake_broadcast(msg, umo, sid, uid=""):
    _BROADCAST_LOG.append(msg)


def make_plugin(tmp_path, cfg_extra=None, session=None):
    cfg = FakeCfg(**{
        "enabled": True,
        "serve_url": "http://127.0.0.1:4096",
        "serve_port": 4096,
        "serve_host": "127.0.0.1",
        "serve_username": "",
        "serve_password": "",
        "run_work_dir": os.path.join(tmp_path, "work"),
        "run_auto_approve": False,
        "run_no_output_timeout_seconds": 120,
        "opencode_exe": os.path.join(tmp_path, "fake_opencode_not_exist.exe"),
        "admin_umos": ADMIN_UMO,
        "result_max_chars": 500,
        "max_tasks": 50,
        "progress_push": True,
        "broadcast_umo": "",
        "retry_max": 0,
        "retry_interval_seconds": 60,
        "archive_max": 200,
        **(cfg_extra or {}),
    })
    plugin = RemoteTaskPlugin.__new__(RemoteTaskPlugin)
    plugin.cfg = cfg
    plugin.plugin_dir = _PLUGIN_DIR
    plugin.store = TaskStore(
        os.path.join(tmp_path, "tasks.json"),
        max_tasks=50,
        archive_file=os.path.join(tmp_path, "archive.json"),
    )
    plugin.store.load()
    try:
        plugin._loop = asyncio.get_running_loop()
    except RuntimeError:
        plugin._loop = asyncio.new_event_loop()
    plugin.hub = ProgressHub(None)
    plugin._watch = {}
    plugin._runners = {}
    plugin._task_by_session = {}
    plugin._serve_done = set()
    plugin._task_permissions = {}
    plugin._dedup = {}
    plugin.serve_client = None
    plugin.serve_manager = ServeManager(os.path.join(tmp_path, "serve.json"))
    plugin.schedule_store = ScheduleStore(os.path.join(tmp_path, "schedule.json"))
    plugin.schedule_store.load()
    plugin.schedule_manager = ScheduleManager(plugin.schedule_store, plugin._submit)
    plugin.context = FakeContext()
    return plugin


def run(coro):
    return asyncio.run(coro)


def test_mention_creator_and_result_tail(tmp_path):
    print("[完成播报 @下发者 与结果高亮]")
    p = make_plugin(tmp_path)

    # onebot 群聊：@ 下发者
    chain = p._build_chain("任务完成", "onebot:GroupMessage:123", "10001")
    from astrbot.api.message_components import At

    first = chain.chain[0]
    check("onebot 群播报 @ 下发者", isinstance(first, At) and first.qq == 10001)
    check("文本仍随消息链", "任务完成" in "".join(c.text for c in chain.chain if hasattr(c, "text")))

    # 私聊/非 onebot 不 @
    chain2 = p._build_chain("x", "onebot:PrivateMessage:1", "10001")
    check("私聊不 @", not any(isinstance(c, At) for c in chain2.chain))
    chain3 = p._build_chain("x", "default:GroupMessage:1", "10001")
    check("非 onebot 平台不 @", not any(isinstance(c, At) for c in chain3.chain))

    # 关闭 @
    p2 = make_plugin(tmp_path, cfg_extra={"notify_mention_creator": False})
    chain4 = p2._build_chain("x", "onebot:GroupMessage:1", "10001")
    check("关闭后不 @", not any(isinstance(c, At) for c in chain4.chain))

    # 非数字下发者 ID 不 @
    chain5 = p._build_chain("x", "onebot:GroupMessage:1", "not-a-number")
    check("非法 ID 不 @", not any(isinstance(c, At) for c in chain5.chain))

    # 结果高亮
    check("无输出提示", "无文本输出" in p._result_tail(""))
    check("正常结果无警告", "⚠️" not in p._result_tail("一切都好"))
    check("错误关键词警告", "⚠️" in p._result_tail("执行遇到 error"))
    check("失败关键词警告", "⚠️" in p._result_tail("任务失败"))

    # creator_user_id 随任务持久化
    t = Task(id="m1", desc="x", creator_umo="g:1", creator_user_id="10001")
    t2 = Task.from_dict(t.to_dict())
    check("creator_user_id 往返保留", t2.creator_user_id == "10001")


def test_admin_gate(tmp_path):
    print("[admin 白名单]")
    p = make_plugin(tmp_path, cfg_extra={"admin_umos": ""})

    async def go():
        r1 = await p._handle(FakeEvent("下发 测试任务"))
        check("未配置白名单拒绝下发", "admin_umos" in r1)
        r2 = await p._handle(FakeEvent("取消 1"))
        check("未配置白名单拒绝取消", "admin_umos" in r2)
        r3 = await p._handle(FakeEvent("serve 启动"))
        check("未配置白名单拒绝 serve 管理", "admin_umos" in r3)
        r4 = await p._handle(FakeEvent("列表"))
        check("查询命令不受白名单限制", "暂无任务" in r4)
    run(go())


def test_query_commands(tmp_path):
    print("[查询命令]")
    p = make_plugin(tmp_path)
    p.store.add(Task(id="q1", desc="任务甲", creator_umo="u", mode="run",
                     status="done", summary="ok", created_at="2026-08-05 10:00:00"))

    async def go():
        r1 = await p._handle(FakeEvent("列表"))
        check("列表含任务", "任务甲" in r1)
        r2 = await p._handle(FakeEvent("详情 q1"))
        check("详情含状态", "已完成" in r2)
        r3 = await p._handle(FakeEvent("详情 nope"))
        check("未知任务提示", "不存在" in r3)
        r4 = await p._handle(FakeEvent("模式"))
        check("模式报告", "执行模式" in r4)
        r5 = await p._handle(FakeEvent("serve 状态"))
        check("serve 状态", "未托管" in r5 or "serve" in r5)
        r6 = await p._handle(FakeEvent("乱来"))
        check("未知子命令作为任务下发拒绝或提示", isinstance(r6, str))
    run(go())


def test_submit_no_workdir(tmp_path):
    print("[无工作目录拒绝]")
    p = make_plugin(tmp_path, cfg_extra={"run_work_dir": ""})

    async def go():
        r = await p._handle(FakeEvent("下发 检查一下磁盘"))
        check("拒绝并提示 run_work_dir", "run_work_dir" in r)
    run(go())


def test_submit_workdir_missing(tmp_path):
    print("[工作目录不存在拒绝]")
    p = make_plugin(tmp_path, cfg_extra={"run_work_dir": os.path.join(tmp_path, "not_exist_dir")})

    async def go():
        r = await p._handle(FakeEvent("下发 检查磁盘"))
        check("拒绝并提示目录不存在", "工作目录不存在" in r)
    run(go())


def test_submit_and_fail_chain(tmp_path):
    print("[下发→受理→启动失败收尾]")
    p = make_plugin(tmp_path)
    work = os.path.join(tmp_path, "work")
    os.makedirs(work, exist_ok=True)

    async def go():
        r = await p._handle(FakeEvent("下发 输出文件列表"))
        check("受理返回任务 ID", "任务" in r and "本地 run" in r)
        check("播报下发公告", any("下发任务" in c.chain[0].text for _, c in p.context.sent))
        # 等 watcher 收尾（opencode 不存在 → 启动失败）
        for _ in range(30):
            if not p._watch:
                break
            await asyncio.sleep(0.2)
        t = p.store.list_recent()[-1]
        check("任务状态 failed（找不到 opencode）", t.status == "failed")
        check("失败原因已记录", "启动失败" in t.error)
        r2 = await p._handle(FakeEvent("取消 " + t.id))
        check("已结束任务无需取消", "无需取消" in r2)
    run(go())


def test_cancel_unknown(tmp_path):
    print("[取消未知任务]")
    p = make_plugin(tmp_path)

    async def go():
        r = await p._handle(FakeEvent("取消 999"))
        check("提示不存在", "不存在" in r)
    run(go())


def test_utils(tmp_path):
    print("[工具函数]")
    p = make_plugin(tmp_path)
    check("_clip 截断", p._clip("x" * 1000) == "x" * 497 + "...")
    check("_clip 空值", p._clip("") == "（无输出）")
    check("_clip 短文本", p._clip("短") == "短")
    check("base64 解析", p._resolve_cred("base64:aGVsbG8=") == "hello")
    check("env 解析", p._resolve_cred("env:SystemRoot") != "")
    check("未知前缀原样", p._resolve_cred("plain") == "plain")


def test_mode_report_reason(tmp_path):
    print("[模式报告原因]")
    p = make_plugin(tmp_path, cfg_extra={"run_work_dir": ""})
    async def go():
        mode, reason = p._decide_mode()
        check("无 serve 无目录 → 不可执行", mode == "不可执行" and "run_work_dir" in reason)
    run(go())


def test_approve_permission(tmp_path):
    print("[权限审批命令]")
    p = make_plugin(tmp_path)
    fake = FakeServeClient()
    p.serve_client = fake
    p.store.add(Task(id="perm1", desc="审批任务", creator_umo=ADMIN_UMO, mode="serve",
                     status="running", session_id="sess-1"))
    p._task_permissions["perm1"] = {"session_id": "sess-1", "permission_id": "per_1", "detail": "x"}

    async def go():
        r1 = await p._handle(FakeEvent("同意 perm1"))
        check("同意放行成功", "已同意" in r1)
        check("once 语义", fake.calls[-1] == ("sess-1", "per_1", "once"))
        check("审批后清除挂起记录", "perm1" not in p._task_permissions)

        p._task_permissions["perm1"] = {"session_id": "sess-1", "permission_id": "per_2", "detail": "x"}
        r2 = await p._handle(FakeEvent("同意 perm1 always"))
        check("always 语义", fake.calls[-1][2] == "always" and "已同意" in r2)

        p._task_permissions["perm1"] = {"session_id": "sess-1", "permission_id": "per_3", "detail": "x"}
        r3 = await p._handle(FakeEvent("拒绝 perm1"))
        check("拒绝语义", fake.calls[-1][2] == "reject" and "已拒绝" in r3)

        r4 = await p._handle(FakeEvent("同意 perm1"))
        check("无挂起请求提示", "没有请求审批" in r4)

        r5 = await p._handle(FakeEvent("同意 nope"))
        check("任务不存在", "不存在" in r5)

        p.store.update("perm1", status="done")
        r6 = await p._handle(FakeEvent("同意 perm1"))
        check("已结束任务提示", "未在运行" in r6)

        p.store.update("perm1", status="running")
        p._task_permissions["perm1"] = {"session_id": "sess-1", "permission_id": "per_4", "detail": "x"}
        fake.fail = True
        r7 = await p._handle(FakeEvent("同意 perm1"))
        check("审批失败提示", "失败" in r7)
        fake.fail = False

        r8 = await p._handle(FakeEvent("同意 perm1", session="default:GroupMessage:111"))
        check("非管理员拒绝审批", "没有执行此命令的权限" in r8)
    run(go())


def test_self_and_repeat_message(tmp_path):
    print("[自消息与重复消息过滤]")
    p = make_plugin(tmp_path)

    class RawEvent(FakeEvent):
        def __init__(self, message_str, raw_message, session=ADMIN_UMO):
            super().__init__(message_str, session)
            self.message_obj = type("M", (), {"raw_message": raw_message})()

    async def go():
        ev_self = RawEvent("下发 任务", {"user_id": "10000", "self_id": "10000"})
        check("机器人自消息跳过", p._is_self_message(ev_self))
        ev_other = RawEvent("下发 任务", {"user_id": "10000", "self_id": "20000"})
        check("他人消息不跳过", not p._is_self_message(ev_other))
        ev_no_raw = RawEvent("下发 任务", None)
        check("无 raw_message 安全", not p._is_self_message(ev_no_raw))

        ev1 = RawEvent("x", {"message_id": "m1"})
        check("首条消息不重复", not p._is_repeat_message(ev1))
        check("同 id 2 秒内重复", p._is_repeat_message(ev1))
        ev2 = RawEvent("x", {"message_id": "m2"})
        check("不同 id 不重复", not p._is_repeat_message(ev2))
    run(go())


def test_deferred_save_lifecycle(tmp_path):
    print("[事件节流延迟落盘]")
    from astrbot_plugin_remote_task.tracker import now_iso

    store_path = os.path.join(tmp_path, "tasks.json")
    p = make_plugin(tmp_path)
    p.store = TaskStore(store_path, max_tasks=50)

    t = p.store.add(Task(id="l1", desc="节流任务", creator_umo=ADMIN_UMO, mode="run"))
    # 高频事件：仅内存更新，不落盘
    p.store.update("l1", save=False, summary="第一段输出")
    p.store.update("l1", save=False, summary="第二段输出")
    with open(store_path, "r", encoding="utf-8") as fh:
        import json as _json
        on_disk = _json.load(fh)
    disk_task = next((x for x in on_disk if x["id"] == "l1"), None)
    check("save=False 不写盘", disk_task is None or disk_task.get("summary") != "第二段输出")
    check("内存已更新 summary", p.store.get("l1").summary == "第二段输出")
    # 任务终结：一次落盘带上最新 summary
    p.store.update("l1", status="done", finished_at=now_iso())
    with open(store_path, "r", encoding="utf-8") as fh:
        on_disk = _json.load(fh)
    disk_task = next((x for x in on_disk if x["id"] == "l1"), None)
    check("终结时落盘最新 summary", disk_task is not None and disk_task["summary"] == "第二段输出")
    check("终结状态持久化", disk_task["status"] == "done")

    # 生命周期：重启加载后 running/pending 标记 failed
    p.store.add(Task(id="l2", desc="中断任务", creator_umo=ADMIN_UMO, status="running"))
    p.store.add(Task(id="l3", desc="排队任务", creator_umo=ADMIN_UMO, status="pending"))
    store2 = TaskStore(store_path, max_tasks=50)
    store2.load()
    t2 = store2.get("l2")
    check("重启后 running→failed", t2.status == "failed" and "中断" in t2.error)
    check("重启后 pending→failed", store2.get("l3").status == "failed")


def test_retry_chain(tmp_path):
    print("[失败自动重试]")
    p = make_plugin(tmp_path, cfg_extra={"retry_max": 2, "retry_interval_seconds": 1})
    calls = {"n": 0}
    messages = []

    async def fake_run(task):
        calls["n"] += 1
        p.store.update(task.id, status="running")
        if calls["n"] < 3:
            p.store.update(task.id, status="failed", error="模拟失败")
        else:
            p.store.update(task.id, status="done", summary="成功", finished_at="t")

    p._execute_run = fake_run

    async def go():
        t = Task(id="retry1", desc="重试任务", creator_umo=ADMIN_UMO, mode="run")
        p.store.add(t)
        _BROADCAST_LOG.clear()
        p._broadcast = fake_broadcast
        await p._execute(t)
        check("失败后重试 3 次", calls["n"] == 3)
        t2 = p.store.get("retry1")
        check("最终成功", t2.status == "done")
        check("retry_count 记录", t2.retry_count == 2)
        check("重试广播提示", any("重试" in m for m in _BROADCAST_LOG))
    run(go())


def test_retry_not_for_aborted(tmp_path):
    print("[取消不重试]")
    p = make_plugin(tmp_path, cfg_extra={"retry_max": 5, "retry_interval_seconds": 1})
    calls = {"n": 0}

    async def fake_run(task):
        calls["n"] += 1
        p.store.update(task.id, status="aborted", finished_at="t")

    p._execute_run = fake_run

    async def go():
        t = Task(id="ab1", desc="取消任务", creator_umo=ADMIN_UMO, mode="run")
        p.store.add(t)
        _BROADCAST_LOG.clear()
        p._broadcast = fake_broadcast
        await p._execute(t)
        check("aborted 不触发重试", calls["n"] == 1)
    run(go())


def test_retry_zero_disabled(tmp_path):
    print("[retry_max=0 不重试]")
    p = make_plugin(tmp_path, cfg_extra={"retry_max": 0})
    calls = {"n": 0}

    async def fake_run(task):
        calls["n"] += 1
        p.store.update(task.id, status="failed", error="模拟失败")

    p._execute_run = fake_run

    async def go():
        t = Task(id="no1", desc="不重试", creator_umo=ADMIN_UMO, mode="run")
        p.store.add(t)
        _BROADCAST_LOG.clear()
        p._broadcast = fake_broadcast
        await p._execute(t)
        check("仅执行一次", calls["n"] == 1)
        check("状态 failed", p.store.get("no1").status == "failed")
    run(go())


def test_schedule_commands(tmp_path):
    print("[定时任务命令]")
    p = make_plugin(tmp_path)

    async def go():
        r1 = await p._handle(FakeEvent("定时 0 9 * * * 每天早上汇总"))
        check("创建定时任务", "已创建定时任务 #s1" in r1)
        check("注册到调度器（无 cron_manager 为 0）", "已注册 0 个" in r1)

        r2 = await p._handle(FakeEvent("定时 bad cron"))
        check("非法 cron 拒绝", "cron 表达式格式不正确" in r2)

        r3 = await p._handle(FakeEvent("定时 0 9 * * *"))
        check("缺描述拒绝", "缺少任务描述" in r3)

        r4 = await p._handle(FakeEvent("定时 列表"))
        check("列表显示", "#s1" in r4 and "每天早上汇总" in r4)

        r5 = await p._handle(FakeEvent("定时 删除 s1"))
        check("删除成功", "已删除定时任务 s1" in r5)

        r6 = await p._handle(FakeEvent("定时 删除 s1"))
        check("重复删除提示不存在", "不存在" in r6)

        r7 = await p._handle(FakeEvent("定时 列表"))
        check("删除后列表空", "暂无定时任务" in r7)
    run(go())


def test_archive_commands(tmp_path):
    print("[归档命令]")
    p = make_plugin(tmp_path)
    p.store.add(Task(id="arc1", desc="归档任务", creator_umo=ADMIN_UMO, mode="run",
                     status="done", summary="ok", created_at="2026-08-05 10:00:00"))

    async def go():
        r1 = await p._handle(FakeEvent("归档"))
        check("归档为空提示", "归档为空" in r1)

        # 手动触发裁剪（把 arc1 挤进归档）
        for i in range(60):
            p.store._tasks.append(Task(id=f"z{i}", desc=f"x{i}", creator_umo="u"))
        p.store.prune()
        r2 = await p._handle(FakeEvent("归档"))
        check("归档列表显示条目", "共" in r2 and "#arc1" in r2)

        r3 = await p._handle(FakeEvent("归档 arc1"))
        check("归档详情", "归档任务" in r3 and "归档任务" in r3)

        r4 = await p._handle(FakeEvent("归档 nope"))
        check("未知归档提示", "不存在" in r4)
    run(go())


def test_template_commands():
    print("[任务模板库命令]")
    with tempfile.TemporaryDirectory() as t:
        p = make_plugin(t)

        async def go():
            r1 = await p._handle(FakeEvent("模板 列表"))
            check("模板列表含数量", "共 6 个" in r1)
            check("模板列表含每日备份", "每日备份" in r1)
            check("模板列表含健康检查", "定时健康检查" in r1)

            r2 = await p._handle(FakeEvent("模板 显示 每日备份"))
            check("模板详情含 cron", "0 2 * * *" in r2)
            check("模板详情含参数", "backup_dir" in r2)

            r3 = await p._handle(FakeEvent("模板 显示 nope"))
            check("未知模板提示", "不存在" in r3)

            r4 = await p._handle(FakeEvent("template list"))
            check("英文子命令可用", "共 6 个" in r4)

            r5 = await p._handle(FakeEvent("模板 创建 每日备份 backup_dir=D:\\data"))
            check("模板创建定时任务", "已创建定时任务 #s1" in r5)

            r6 = await p._handle(FakeEvent("定时 列表"))
            check("模板任务进入定时列表", "#s1" in r6 and "备份目录" in r6)

            r7 = await p._handle(FakeEvent("模板 创建 每日备份"))
            check("缺必填参数报错", "缺少参数" in r7)

            r8 = await p._handle(FakeEvent("模板 创建 nope"))
            check("未知模板创建拒绝", "不存在" in r8)

            r9 = await p._handle(FakeEvent("模板 创建 每日备份 backup_dir=D:\\data",
                                           session="default:GroupMessage:111"))
            check("非管理员拒绝创建", "没有执行此命令的权限" in r9)

            r10 = await p._handle(FakeEvent("模板 创建 daily_backup backup_dir=D:\\x backup_root=E:\\bak"))
            check("英文名+多参数创建", "已创建定时任务 #s2" in r10)

            r11 = await p._handle(FakeEvent("模板"))
            check("无子命令给用法", "用法" in r11)

            r12 = await p._handle(FakeEvent("模板 创建 每周统计日报"))
            check("有默认参数模板创建", "已创建定时任务 #s3" in r12)
            r13 = await p._handle(FakeEvent("定时 列表"))
            check("默认参数渲染进描述", "最近 7 天" in r13)
        run(go())


def test_calendar_command():
    print("[任务日历命令]")
    with tempfile.TemporaryDirectory() as t:
        p = make_plugin(t)

        async def go():
            r0 = await p._handle(FakeEvent("日历"))
            check("无定时任务日历为空", "暂无启用的定时任务" in r0)

            await p._create_schedule("0 9 * * *", "每天早上汇总", ADMIN_UMO, "self")
            await p._create_schedule("*/30 * * * *", "每半小时巡检", ADMIN_UMO, "self")

            r1 = await p._handle(FakeEvent("日历"))
            check("日历标题", "任务日历" in r1)
            check("日历含任务一", "#s1" in r1 and "每天早上汇总" in r1)
            check("日历含 09:00", "09:00" in r1)
            check("日历含任务二", "每半小时巡检" in r1)
            check("日历含日期", "2026" in r1)
            check("日历含星期", "周" in r1)

            r2 = await p._handle(FakeEvent("日历 3"))
            check("指定天数", "未来 3 天" in r2)

            r3 = await p._handle(FakeEvent("日历 abc"))
            check("非法天数提示用法", "用法" in r3)

            r4 = await p._handle(FakeEvent("日历 0"))
            check("0 天钳制为 1", "未来 1 天" in r4)

            r4b = await p._handle(FakeEvent("calendar 2"))
            check("英文子命令可用", "未来 2 天" in r4b)

            # 脏 cron 条目不崩溃、不出现
            p.schedule_store.add(ScheduleEntry("s9", "bad cron", "脏任务", ADMIN_UMO))
            r5 = await p._handle(FakeEvent("日历"))
            check("脏 cron 不崩溃", "任务日历" in r5 and "#s9" not in r5)

            r7 = await p._handle(FakeEvent("日历", session="default:GroupMessage:111"))
            check("非管理员拒绝日历", "没有执行此命令的权限" in r7)
        run(go())


def test_calendar_no_upcoming():
    print("[日历无排期提示]")
    with tempfile.TemporaryDirectory() as t:
        p = make_plugin(t)

        async def go():
            # 只有周日触发的任务，未来 1 天（今天周一）无排期
            await p._create_schedule("15 3 * * 0", "周日下午提醒", ADMIN_UMO, "self")
            r = await p._handle(FakeEvent("日历 1"))
            check("未来 1 天无排期提示", "无定时任务触发" in r)
        run(go())


def test_callback_targets(tmp_path):
    print("[回调目标解析]")
    p = make_plugin(tmp_path)
    check("@群ID 解析",
          p._resolve_callback_targets("@123", "default:GroupMessage:1") == ["default:GroupMessage:123"])
    check("用户 UMO 解析",
          p._resolve_callback_targets("u@123", "onebot:GroupMessage:1") == ["onebot:PrivateMessage:123"])
    check("完整 UMO 透传",
          p._resolve_callback_targets("onebot:GroupMessage:9", "default:x:1") == ["onebot:GroupMessage:9"])
    check("混合列表",
          p._resolve_callback_targets("onebot:GroupMessage:9, @7", "default:x:1")
          == ["onebot:GroupMessage:9", "default:GroupMessage:7"])
    check("空串为空", p._resolve_callback_targets("", "default:x:1") == [])
    check("None 为空", p._resolve_callback_targets(None, "default:x:1") == [])
    check("非法 @ 忽略", p._resolve_callback_targets("@abc", "default:x:1") == [])
    check("去重", len(p._resolve_callback_targets("@1,@1", "default:x:1")) == 1)
    check("中文逗号兼容",
          p._resolve_callback_targets("@1，@2", "default:x:1")
          == ["default:GroupMessage:1", "default:GroupMessage:2"])


def test_callback_push(tmp_path):
    print("[结果回调推送]")
    p = make_plugin(tmp_path)

    async def go():
        # 任务指定回调目标
        t1 = Task(id="cb1", desc="x", creator_umo=ADMIN_UMO, callback_target="default:GroupMessage:999")
        p.store.add(t1)
        await p._push_result_callback(t1, "任务完成摘要")
        hit = [u for u, _ in p.context.sent if u == "default:GroupMessage:999"]
        check("任务回调目标收到推送", len(hit) == 1)
        check("推送内容正确", any("任务完成摘要" in str(c.chain[0].text) for _, c in p.context.sent))

        # 无任务回调目标 + 无配置 → 不推送
        p.context.sent.clear()
        t2 = Task(id="cb2", desc="x", creator_umo=ADMIN_UMO)
        p.store.add(t2)
        await p._push_result_callback(t2, "不应发出")
        check("无回调目标不推送", len(p.context.sent) == 0)

        # 配置回调目标（@ 形式，沿用下发会话平台）
        p2 = make_plugin(tmp_path, cfg_extra={"callback_target": "@888"})
        t3 = Task(id="cb3", desc="x", creator_umo=ADMIN_UMO)
        p2.store.add(t3)
        await p2._push_result_callback(t3, "配置回调摘要")
        check("配置回调目标推送", any(u == "default:GroupMessage:888" for u, _ in p2.context.sent))
        check("配置 @ 不叠加任务回调", not any(u == "default:GroupMessage:999" for u, _ in p2.context.sent))

        # 多目标推送
        p.context.sent.clear()
        t4 = Task(id="cb4", desc="x", creator_umo=ADMIN_UMO,
                  callback_target="default:GroupMessage:1,default:GroupMessage:2")
        p.store.add(t4)
        await p._push_result_callback(t4, "多目标")
        umos = [u for u, _ in p.context.sent]
        check("多目标全部推送", umos.count("default:GroupMessage:1") == 1 and umos.count("default:GroupMessage:2") == 1)
    run(go())


def test_submit_with_callback(tmp_path):
    print("[提交携带回调参数]")
    p = make_plugin(tmp_path)
    work = os.path.join(tmp_path, "work")
    os.makedirs(work, exist_ok=True)

    desc, cb = p._extract_callback("检查磁盘 --callback @123")
    check("提取 --callback", desc == "检查磁盘" and cb == "@123")
    desc2, cb2 = p._extract_callback("--callback=@5 跑一下")
    check("提取 --callback=", desc2 == "跑一下" and cb2 == "@5")
    desc3, cb3 = p._extract_callback("普通任务")
    check("无回调参数", desc3 == "普通任务" and cb3 == "")
    desc4, cb4 = p._extract_callback("--callback @9 跑一下 --callback @8")
    check("后者覆盖", desc4 == "跑一下" and cb4 == "@8")
    desc5, cb5 = p._extract_callback("--callback @9")
    check("仅回调参数描述为空", desc5 == "" and cb5 == "@9")

    async def go():
        r = await p._handle(FakeEvent("下发 检查磁盘 --callback @123"))
        check("受理成功", "已受理" in r)
        task = p.store.list_recent()[-1]
        check("回调目标落库", task.callback_target == "default:GroupMessage:123")
        # 等 watcher 收尾（opencode 不存在 → 启动失败，触发失败回调）
        for _ in range(30):
            if not p._watch:
                break
            await asyncio.sleep(0.2)
        check("失败回调已推送", any("default:GroupMessage:123" in u for u, _ in p.context.sent))
    run(go())


def run_all(tmp_path):
    test_admin_gate(tmp_path)
    test_query_commands(tmp_path)
    test_submit_no_workdir(tmp_path)
    test_submit_workdir_missing(tmp_path)
    test_submit_and_fail_chain(tmp_path)
    test_cancel_unknown(tmp_path)
    test_approve_permission(tmp_path)
    test_utils(tmp_path)
    test_mode_report_reason(tmp_path)
    test_self_and_repeat_message(tmp_path)
    test_deferred_save_lifecycle(tmp_path)
    test_retry_chain(tmp_path)
    test_retry_not_for_aborted(tmp_path)
    test_retry_zero_disabled(tmp_path)
    test_schedule_commands(tmp_path)
    test_archive_commands(tmp_path)
    test_template_commands()
    test_calendar_command()
    test_calendar_no_upcoming()
    test_callback_targets(tmp_path)
    test_callback_push(tmp_path)
    test_submit_with_callback(tmp_path)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp_path:
        run_all(tmp_path)
    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    sys.exit(1 if FAIL else 0)