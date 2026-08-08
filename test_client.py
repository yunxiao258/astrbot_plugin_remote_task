"""astrbot_plugin_remote_task 单元测试：serve API 客户端、SSE 解析、serve 进程托管。

运行：python test_client.py
用本地 ThreadingHTTPServer 模拟 opencode serve，用 python 子进程模拟 serve 后台进程。
"""

import asyncio
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_PLUGIN_DIR))

from astrbot_plugin_remote_task.client import (
    ServeClient,
    ServeManager,
    parse_sse_payload,
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


class FakeHandler(BaseHTTPRequestHandler):
    """模拟 opencode serve 的部分 API"""

    sessions = {}
    prompts = []
    aborts = []
    permission_responses = []
    permission_bodies = []
    last_body = ""

    def log_message(self, *a):
        pass

    def _send_json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/global/health":
            self._send_json(200, {"status": "ok"})
        elif self.path.endswith("/message"):
            self._send_json(200, {"items": [
                {"role": "assistant", "parts": [{"type": "text", "text": "任务结果内容"}]},
            ]})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        ln = int(self.headers.get("Content-Length", 0) or 0)
        self.last_body = self.rfile.read(ln).decode() if ln else ""
        if self.path == "/session":
            self._send_json(200, {"id": "sess-abc-123"})
        elif self.path.endswith("/prompt_async"):
            self.prompts.append(self.last_body)
            self._send_json(200, {})
        elif self.path.endswith("/abort"):
            self.aborts.append(1)
            self._send_json(200, {"ok": True})
        elif "/permissions/" in self.path:
            self.permission_responses.append(self.path)
            self.permission_bodies.append(self.last_body)
            self._send_json(200, True)
        else:
            self._send_json(404, {"error": "not found"})


def start_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeHandler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


def test_parse_sse_payload():
    print("[SSE 事件解析]")
    ev = parse_sse_payload("message.updated",
                           json.dumps({"type": "message.updated", "sessionID": "s1",
                                       "message": {"text": "你好"}}))
    check("message→(text,sid,内容)", ev is not None and ev[0] == "text" and ev[1] == "s1" and ev[2] == "你好")
    check("message→meta 空 dict", ev is not None and isinstance(ev[3], dict) and not ev[3])
    ev = parse_sse_payload("tool",
                           json.dumps({"type": "tool", "sessionID": "s2",
                                       "toolName": "edit", "toolInput": {"file_path": "a.py"}}))
    check("tool 事件", ev is not None and ev[0] == "tool" and "a.py" in ev[2])
    ev = parse_sse_payload("permission.request",
                           json.dumps({"type": "permission.request", "sessionID": "s3",
                                       "permission": {"pattern": "bash *"}}))
    check("permission 事件（旧格式）", ev is not None and ev[0] == "permission" and "bash" in ev[2])
    # opencode 1.18 真实格式：permission.asked + properties
    ev = parse_sse_payload("",
                           json.dumps({"type": "permission.asked", "sessionID": "s5",
                                       "properties": {"id": "per_123", "permission": "external_directory",
                                                      "patterns": ["C:\\\\Windows\\\\*"],
                                                      "always": ["C:\\\\Windows\\\\*"],
                                                      "metadata": {"filepath": "C:\\\\Windows\\\\win.ini"}}}))
    check("permission.asked→permission", ev is not None and ev[0] == "permission")
    check("permission.asked 提取 ID", ev is not None and ev[3].get("permission_id") == "per_123")
    check("permission.asked 提取路径", ev is not None and "win.ini" in ev[2])
    check("permission.asked 提取 always", ev is not None and ev[3].get("always") == ["C:\\\\Windows\\\\*"])
    # message.part.updated 的 tool part（真实格式）
    ev = parse_sse_payload("",
                           json.dumps({"type": "message.part.updated", "sessionID": "s6",
                                       "properties": {"part": {"type": "tool", "tool": "bash",
                                                               "state": {"input": {"command": "git push"}}}}}))
    check("part.updated tool→tool", ev is not None and ev[0] == "tool" and "git push" in ev[2])
    # message.part.updated 的 text part
    ev = parse_sse_payload("",
                           json.dumps({"type": "message.part.updated", "sessionID": "s7",
                                       "properties": {"part": {"type": "text", "text": "部分输出"}}}))
    check("part.updated text→text", ev is not None and ev[0] == "text" and ev[2] == "部分输出")
    # sync 包装解包
    ev = parse_sse_payload("",
                           json.dumps({"type": "sync",
                                       "syncEvent": {"type": "message.part.updated.1", "seq": 1,
                                                     "data": {"type": "message.part.updated", "sessionID": "s8",
                                                              "properties": {"part": {"type": "tool", "tool": "edit",
                                                                                      "state": {"input": {"file_path": "b.js"}}}}}}}))
    check("sync 包装内 tool 事件", ev is not None and ev[0] == "tool" and "b.js" in ev[2])
    # message.updated（sessionID 在 properties 内层的真实格式）
    ev = parse_sse_payload("",
                           json.dumps({"type": "message.part.updated",
                                       "properties": {"sessionID": "s9",
                                                      "part": {"type": "text", "text": "内层会话"}}}))
    check("properties.sessionID 提取", ev is not None and ev[1] == "s9" and ev[2] == "内层会话")
    # message.part.delta（真实流式事件）
    ev = parse_sse_payload("",
                           json.dumps({"type": "message.part.delta",
                                       "properties": {"sessionID": "s10", "messageID": "m1",
                                                      "partID": "p1", "field": "text", "delta": ""}}))
    check("part.delta 忽略（无文本）", ev is None)
    ev = parse_sse_payload("session.updated",
                           json.dumps({"type": "finished", "sessionID": "s4"}))
    check("finished→done", ev is not None and ev[0] == "done")
    check("无 sessionID 忽略", parse_sse_payload("x", json.dumps({"type": "y"})) is None)
    check("非法 JSON 忽略", parse_sse_payload("x", "not json") is None)


def test_serve_api():
    print("[serve API]")
    srv, url = start_server()
    try:
        c = ServeClient(url, "user", "pass")
        check("probe 成功", c.probe() is True)
        sid = c.create_session("测试任务")
        check("创建会话返回 ID", sid == "sess-abc-123")
        check("prompt 下发成功", c.send_prompt(sid, "跑一下") is True)
        body = json.loads(FakeHandler.prompts[-1])
        check("prompt body 含 parts", body["parts"][0]["text"] == "跑一下")
        check("拉取消息", "任务结果" in c.get_message_text(sid))
        check("abort 成功", c.abort(sid) is True)
        check("abort 被调用", len(FakeHandler.aborts) == 1)

        FakeHandler.permission_responses.clear()
        FakeHandler.permission_bodies.clear()
        ok = c.respond_permission(sid, "per_1", "always")
        check("审批接口成功", ok is True)
        check("审批路径含 permission ID", len(FakeHandler.permission_responses) == 1
              and "per_1" in FakeHandler.permission_responses[0])
        body = json.loads(FakeHandler.permission_bodies[-1])
        check("审批 body response", body.get("response") == "always")

        c2 = ServeClient("http://127.0.0.1:1")
        check("不可达 probe False", c2.probe() is False)
        check("不可达 create_session None", c2.create_session() is None)
        check("不可达 send_prompt False", c2.send_prompt("x", "y") is False)
        check("不可达 respond_permission False", c2.respond_permission("x", "p", "once") is False)
        check("不可达 get_message_text 空", c2.get_message_text("x") == "")
    finally:
        srv.shutdown()


def test_serve_api_fallback_on_400(tmp_path):
    print("[400 时回退空 body]")
    class H(FakeHandler):
        def do_POST(self):
            ln = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(ln).decode() if ln else ""
            if self.path == "/session":
                if body.strip() and body.strip() != "{}":
                    self._send_json(400, {"error": "bad title"})
                else:
                    self._send_json(200, {"id": "fallback-id"})
            else:
                self._send_json(404, {})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = ServeClient(f"http://127.0.0.1:{srv.server_port}")
        check("带标题 400 后回退成功", c.create_session("t") == "fallback-id")
    finally:
        srv.shutdown()


def test_serve_manager(tmp_path):
    print("[serve 进程托管]")
    data_file = os.path.join(tmp_path, "serve.json")
    fake = ["sleep", "300"]
    long_sleep = [sys.executable, "-c", "import time; time.sleep(300)"]
    mgr = ServeManager(data_file, opencode_exe=sys.executable)
    pid, msg = mgr.spawn(4099, "127.0.0.1", cmd_override=long_sleep)
    check("spawn 返回 pid", pid > 0)
    check("spawn 消息", "已启动" in msg)
    check("状态文件记录", os.path.exists(data_file))
    check("进程存活判定", mgr._pid_running(pid) is True)
    st = mgr.status()
    check("状态文本含 PID", f"PID {pid}" in st)
    # 重复 spawn 不重复拉起
    pid2, msg2 = mgr.spawn(4099, "127.0.0.1", cmd_override=long_sleep)
    check("重复 spawn 复用", pid2 == pid and "已在运行" in msg2)
    r = mgr.stop()
    check("stop 成功", "已停止" in r)
    check("停止后文件清空", mgr.status() == "插件未托管 opencode serve 进程")
    # 停止后再 spawn 全新进程
    pid3, msg3 = mgr.spawn(4099, "127.0.0.1", cmd_override=long_sleep)
    check("停止后可再次启动", pid3 > 0 and pid3 != pid)
    r3 = mgr.stop()
    check("再次停止", "已停止" in r3)
    # 不存在文件时状态
    mgr2 = ServeManager(os.path.join(tmp_path, "no.json"))
    check("无记录状态", "未托管" in mgr2.status())


def run_all(tmp_path):
    test_parse_sse_payload()
    test_serve_api()
    test_serve_api_fallback_on_400(tmp_path)
    test_serve_manager(tmp_path)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp_path:
        run_all(tmp_path)
    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    sys.exit(1 if FAIL else 0)