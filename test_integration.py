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


class FakeContext:
    def __init__(self):
        self.sent = []

    async def send_message(self, umo, chain):
        self.sent.append((umo, chain))
        return True


class FakeEvent:
    def __init__(self, message_str, session=ADMIN_UMO):
        self.message_str = message_str
        self._session = session
        self.sent_text = None

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
        **(cfg_extra or {}),
    })
    plugin = RemoteTaskPlugin.__new__(RemoteTaskPlugin)
    plugin.cfg = cfg
    plugin.plugin_dir = _PLUGIN_DIR
    plugin.store = TaskStore(os.path.join(tmp_path, "tasks.json"), max_tasks=50)
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
    plugin.serve_client = None
    plugin.serve_manager = ServeManager(os.path.join(tmp_path, "serve.json"))
    plugin.context = FakeContext()
    return plugin


def run(coro):
    return asyncio.run(coro)


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


def run_all(tmp_path):
    test_admin_gate(tmp_path)
    test_query_commands(tmp_path)
    test_submit_no_workdir(tmp_path)
    test_submit_workdir_missing(tmp_path)
    test_submit_and_fail_chain(tmp_path)
    test_cancel_unknown(tmp_path)
    test_utils(tmp_path)
    test_mode_report_reason(tmp_path)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp_path:
        run_all(tmp_path)
    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    sys.exit(1 if FAIL else 0)