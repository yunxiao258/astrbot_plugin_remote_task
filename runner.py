"""模式 B：本地 opencode run 进程管理。

以 `opencode run "<任务>" --format json` 异步子进程方式执行任务。
- 逐行解析 stdout 的 JSON 事件（message.updated / tool / permission.request 等）
- 无新输出超时视为卡住（常见于非交互模式权限卡在 ask），自动终止并标记失败
- 取消时 Windows 用 taskkill 杀进程树，Linux 杀进程组

严格权限策略：默认不加 --auto，任务在 opencode 现有 permission 配置规则下运行；
权限卡住时超时兜底并在群内提示。
"""

import asyncio
import json
import os
import shutil
import signal
import sys
import time
from typing import Callable, Coroutine

from astrbot.api import logger

DEFAULT_EXE = "opencode"


# ---------------- 命令构造 ----------------

def build_run_command(
    desc: str,
    auto_approve: bool = False,
    opencode_exe: str | None = None,
) -> list[str]:
    """构造 opencode run 命令参数"""
    exe = opencode_exe or shutil.which("opencode") or DEFAULT_EXE
    cmd = [exe, "run", desc, "--format", "json"]
    if auto_approve:
        cmd.append("--auto")
    return cmd


def permission_rules_to_env(rules: dict | None) -> dict:
    """把 permission 规则注入 run 进程环境（OPENCODE_CONFIG_CONTENT 内联配置）。

    headless `opencode run` 遇到解析为 ask 的权限会永久挂起（无 TTY 无法审批），
    通过内联配置把需要放行的权限类改为 allow，从源头避免挂起。
    无效输入返回空 dict（不注入，保持既有行为）。
    """
    if not rules or not isinstance(rules, dict):
        return {}
    return {
        "OPENCODE_CONFIG_CONTENT": json.dumps(
            {"permission": rules}, ensure_ascii=False
        )
    }


# ---------------- 事件解析 ----------------

def _extract_text(payload, depth: int = 0) -> str:
    """从事件 payload 中宽松提取文本内容（递归找 text/content 字符串）"""
    if depth > 6:
        return ""
    if isinstance(payload, str):
        return payload
    if isinstance(payload, list):
        return "\n".join(_extract_text(x, depth + 1) for x in payload)
    if isinstance(payload, dict):
        for key in ("items", "parts", "messages"):
            v = payload.get(key)
            if isinstance(v, list):
                r = _extract_text(v, depth + 1)
                if r:
                    return r
        for key in ("text", "content", "message", "output", "description", "title"):
            v = payload.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
            if isinstance(v, (dict, list)):
                r = _extract_text(v, depth + 1)
                if r:
                    return r
    return ""


def parse_run_event(line: str) -> tuple[str, str] | None:
    """解析 opencode run --format json 的一行事件，返回 (kind, text)。

    kind: text=模型输出 / tool=工具调用 / permission=权限请求 / done=结束
    无法解析返回 None。
    """
    line = line.strip()
    if not line:
        return None
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    etype = str(data.get("type") or data.get("event") or "")
    if "message.updated" in etype or "message" in etype and "tool" not in etype:
        text = _extract_text(data)
        if text:
            return ("text", text)
        return None
    if "tool" in etype or "toolCall" in etype or data.get("tool"):
        tool = data.get("toolName") or data.get("name") or data.get("tool") or "工具"
        args = data.get("toolInput") or data.get("input") or data.get("args") or {}
        if isinstance(args, dict):
            arg_text = " ".join(f"{k}={v}" for k, v in args.items() if k in ("command", "file_path", "path", "pattern", "query"))
        else:
            arg_text = str(args)[:120]
        return ("tool", f"{tool} {arg_text}".strip())
    if "permission" in etype or etype == "permission.request":
        detail = _extract_text(data) or ""
        return ("permission", f"请求权限: {detail}" if detail else "请求权限")
    if "finished" in etype or "complete" in etype or "session.updated" in etype:
        return ("done", _extract_text(data))
    return None


# ---------------- 进程管理 ----------------

class RunProcess:
    """本地 opencode run 子进程"""

    def __init__(
        self,
        task_id: str,
        desc: str,
        work_dir: str,
        timeout_no_output: int = 120,
        auto_approve: bool = False,
        on_event: Callable[[str, str, str], Coroutine] | None = None,
        opencode_exe: str | None = None,
        env_extra: dict | None = None,
        cmd_override: list[str] | None = None,
    ):
        self.task_id = task_id
        self.desc = desc
        self.work_dir = work_dir
        self.timeout_no_output = max(30, int(timeout_no_output))
        self.auto_approve = auto_approve
        self.on_event = on_event
        self.env_extra = env_extra or {}
        self.cmd = (
            list(cmd_override)
            if cmd_override
            else build_run_command(desc, auto_approve, opencode_exe)
        )
        self.proc: asyncio.subprocess.Process | None = None
        self.cancelled = False
        self.last_output_at = 0.0
        self.error = ""

    @property
    def pid(self) -> int:
        return self.proc.pid if self.proc else 0

    async def start(self) -> bool:
        """启动进程，返回是否成功（找不到 opencode 可执行文件等返回 False）"""
        try:
            env = dict(os.environ)
            env.update(self.env_extra)
            self.proc = await asyncio.create_subprocess_exec(
                *self.cmd,
                cwd=self.work_dir,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=sys.platform != "win32",
            )
            self.last_output_at = time.monotonic()
            return True
        except (OSError, ValueError) as e:
            self.error = f"启动失败: {e}"
            logger.error(f"任务 {self.task_id} {self.error}")
            return False

    async def _emit(self, kind: str, text: str):
        if self.on_event:
            try:
                await self.on_event(self.task_id, kind, text)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"任务 {self.task_id} 事件回调异常: {e}")

    async def wait(self) -> str:
        """等待进程结束，返回终态 done / failed / aborted"""
        if not self.proc:
            return "failed"
        if self.cancelled:
            return "aborted"
        if self.proc.returncode is not None:
            # 进程已结束（如取消时刚好终止）
            rc = self.proc.returncode
            if rc != 0 and not self.error:
                self.error = f"退出码 {rc}"
            return "done" if rc == 0 else "failed"
        stderr_lines: list[str] = []
        while True:
            if self.cancelled:
                break
            timeout = self.timeout_no_output - (time.monotonic() - self.last_output_at)
            if timeout <= 0:
                self.error = (
                    f"无新输出超过 {self.timeout_no_output} 秒，已终止"
                    "（可能权限卡在 ask，请放行后重跑）"
                )
                logger.warning(f"任务 {self.task_id} {self.error}")
                await self.cancel()
                return "failed"
            try:
                line = await asyncio.wait_for(
                    self.proc.stdout.readline(), timeout=timeout
                )
            except asyncio.TimeoutError:
                continue
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip("\n")
            self.last_output_at = time.monotonic()
            if not text:
                continue
            ev = parse_run_event(text)
            if ev:
                await self._emit(*ev)
        rc = await self.proc.wait()
        err = await self.proc.stderr.read()
        if err:
            self.error = err.decode("utf-8", errors="replace").strip()[-300:]
        if self.cancelled:
            return "aborted"
        if rc == 0:
            return "done"
        self.error = self.error or f"退出码 {rc}"
        return "failed"

    async def cancel(self):
        """终止进程（Windows 杀进程树，Linux 杀进程组）"""
        self.cancelled = True
        proc = self.proc
        if not proc or proc.returncode is not None:
            return
        try:
            if sys.platform == "win32":
                sub = await asyncio.create_subprocess_exec(
                    "taskkill", "/PID", str(proc.pid), "/T", "/F",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await sub.wait()
            else:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except ProcessLookupError:
                    proc.kill()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"任务 {self.task_id} 终止失败: {e}")
            try:
                proc.kill()
            except ProcessLookupError:
                pass