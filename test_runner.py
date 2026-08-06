"""astrbot_plugin_remote_task 单元测试：本地 run 进程管理。

运行：python test_runner.py
用真实 python 子进程模拟 opencode run --format json 的行为。
"""

import asyncio
import os
import sys
import tempfile

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_PLUGIN_DIR))

from astrbot_plugin_remote_task.runner import (
    RunProcess,
    _extract_text,
    build_run_command,
    parse_run_event,
)

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS: {name}")
    else:
        FAIL += 1
        print(f"  FAIL: {name}")


def run(coro):
    return asyncio.run(coro)


def test_build_run_command():
    print("[命令构造]")
    cmd = build_run_command("hello", auto_approve=False)
    check("默认不含 --auto", "--auto" not in cmd)
    check("含 --format json", "--format" in cmd and "json" in cmd)
    cmd2 = build_run_command("hello", auto_approve=True)
    check("auto 模式含 --auto", "--auto" in cmd2)


def test_extract_text():
    print("[文本提取]")
    check("直接字符串", _extract_text("hi") == "hi")
    check("嵌套 text", _extract_text({"message": {"text": "世界"}}) == "世界")
    check("列表内容", _extract_text([{"content": "a"}, {"content": "b"}]) == "a\nb")
    check("空 dict 为空", _extract_text({}) == "")
    check("循环引用安全", _extract_text({"a": {"b": {"c": {"d": {"e": {"f": {"g": {}}}}}}}}) == "")


def test_parse_run_event():
    print("[事件解析]")
    ev = parse_run_event('{"type":"message.updated","message":{"text":"你好"}}')
    check("message.updated→text", ev is not None and ev[0] == "text" and ev[1] == "你好")
    ev = parse_run_event('{"type":"tool","toolName":"bash","toolInput":{"command":"git push"}}')
    check("tool→工具调用", ev is not None and ev[0] == "tool" and "git push" in ev[1])
    ev = parse_run_event('{"type":"permission.request","title":"x"}')
    check("permission→权限", ev is not None and ev[0] == "permission")
    check("空行", parse_run_event("") is None)
    check("非 JSON", parse_run_event("随便一些文本") is None)
    check("缺失类型忽略", parse_run_event('{"foo":"bar"}') is None)


def test_run_done(tmp_path):
    print("[正常完成]")
    events = []
    async def on_event(tid, kind, text):
        events.append((kind, text))
    script = (
        'import json,sys\n'
        'print(json.dumps({"type":"message.updated","message":{"text":"start"}}), flush=True)\n'
        'print(json.dumps({"type":"tool","toolName":"bash","toolInput":{"command":"echo hi"}}), flush=True)\n'
        'print(json.dumps({"type":"message.updated","message":{"text":"final output"}}), flush=True)\n'
    )
    r = RunProcess("t1", "任务", tmp_path, timeout_no_output=60,
                   on_event=on_event, cmd_override=[sys.executable, "-c", script])
    async def go():
        assert await r.start()
        return await r.wait()
    status = run(go())
    check("状态 done", status == "done")
    check("事件捕获", any(k == "text" and t == "start" for k, t in events))
    check("工具事件捕获", any(k == "tool" for k, t in events))
    check("pid 已记录", r.pid > 0)


def test_run_failure_exit(tmp_path):
    print("[非零退出]")
    r = RunProcess("t2", "x", tmp_path, timeout_no_output=60,
                   cmd_override=[sys.executable, "-c", "import sys; sys.stderr.write('boom\\n'); sys.exit(3)"])
    async def go():
        assert await r.start()
        return await r.wait()
    status = run(go())
    check("失败状态", status == "failed")
    check("错误捕获 stderr", "boom" in r.error)


def test_run_no_output_timeout(tmp_path):
    print("[无输出超时]")
    r = RunProcess("t3", "x", tmp_path, timeout_no_output=30,
                   cmd_override=[sys.executable, "-c", "import time; print('ok', flush=True); time.sleep(60)"])
    async def go():
        assert await r.start()
        return await r.wait()
    status = run(go())
    check("超时→failed", status == "failed")
    check("错误含无输出提示", "无新输出" in r.error or "卡在 ask" in r.error)


def test_run_cancel(tmp_path):
    print("[取消]")
    r = RunProcess("t4", "x", tmp_path, timeout_no_output=120,
                   cmd_override=[sys.executable, "-c", "import time; time.sleep(60)"])
    async def go():
        assert await r.start()
        await asyncio.sleep(1.0)
        await r.cancel()
        return await r.wait()
    status = run(go())
    check("取消→aborted", status == "aborted")


def test_run_start_failure(tmp_path):
    print("[启动失败]")
    r = RunProcess("t5", "x", tmp_path, timeout_no_output=60,
                   cmd_override=[os.path.join(tmp_path, "no_such_exe_12345.exe")])
    async def go():
        return await r.start()
    ok = run(go())
    check("找不到可执行返回 False", ok is False)
    check("error 有原因", "启动失败" in r.error)


def run_all(tmp_path):
    test_build_run_command()
    test_extract_text()
    test_parse_run_event()
    test_run_done(tmp_path)
    test_run_failure_exit(tmp_path)
    test_run_no_output_timeout(tmp_path)
    test_run_cancel(tmp_path)
    test_run_start_failure(tmp_path)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp_path:
        run_all(tmp_path)
    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    sys.exit(1 if FAIL else 0)