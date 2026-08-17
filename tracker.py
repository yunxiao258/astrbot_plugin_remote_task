"""任务状态机与持久化：任务生命周期、tasks.json 存储、启动恢复。

任务状态:
- pending: 已创建，等待执行器接管
- running: 执行中
- done: 完成
- failed: 执行失败或进程异常终止
- aborted: 管理员取消

启动时会把遗留的 pending/running 任务标记为 failed（插件重启/会话中断），
保证状态机在任何时刻都能正确闭合。
"""

import asyncio
import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Callable

from astrbot.api import logger

STATUSES = ("pending", "running", "done", "failed", "aborted")


def now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class Task:
    """一个远程任务"""

    id: str
    desc: str
    creator_umo: str
    creator_self_id: str = ""
    creator_user_id: str = ""  # 下发者用户 ID（用于完成/失败时 @ 提醒）
    mode: str = ""  # serve / run
    status: str = "pending"
    created_at: str = field(default_factory=now_iso)
    finished_at: str = ""
    summary: str = ""
    error: str = ""
    session_id: str = ""  # serve 模式的 opencode 会话 ID
    pid: int = 0  # run 模式的进程 PID
    retry_count: int = 0  # 已自动重试次数
    callback_target: str = ""  # 结果回调推送目标（解析后的 UMO 列表，逗号分隔；空则不推送）
    items: list = field(default_factory=list)  # 进度节点记录

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        return cls(
            id=d.get("id", ""),
            desc=d.get("desc", ""),
            creator_umo=d.get("creator_umo", ""),
            creator_self_id=d.get("creator_self_id", ""),
            creator_user_id=d.get("creator_user_id", ""),
            mode=d.get("mode", ""),
            status=d.get("status", "pending"),
            created_at=d.get("created_at", ""),
            finished_at=d.get("finished_at", ""),
            summary=d.get("summary", ""),
            error=d.get("error", ""),
            session_id=d.get("session_id", ""),
            pid=d.get("pid", 0),
            retry_count=int(d.get("retry_count", 0) or 0),
            callback_target=d.get("callback_target", ""),
            items=list(d.get("items") or []),
        )


# ---------------- 格式化 ----------------

_STATUS_LABEL = {
    "pending": "排队中",
    "running": "执行中",
    "done": "已完成",
    "failed": "失败",
    "aborted": "已取消",
}


def status_label(status: str) -> str:
    return _STATUS_LABEL.get(status, status)


def format_task(t: Task) -> str:
    lines = [
        f"任务 #{t.id} [{status_label(t.status)}]",
        f"命令: {t.desc}",
    ]
    if t.creator_umo:
        lines.append(f"发起人: {t.creator_umo}")
    if t.mode:
        lines.append(f"模式: {'serve API' if t.mode == 'serve' else '本地 run'}")
    lines.append(f"创建: {t.created_at}")
    if t.finished_at:
        lines.append(f"结束: {t.finished_at}")
    if t.summary:
        lines.append(f"结果: {t.summary}")
    if t.error:
        lines.append(f"错误: {t.error}")
    return "\n".join(lines)


def format_task_list(tasks: list[Task], limit: int = 10) -> str:
    rows = tasks[-limit:]
    if not rows:
        return "暂无任务记录"
    return "\n".join(
        f"#{t.id} [{status_label(t.status)}] {t.desc[:60]} @{t.created_at}"
        for t in reversed(rows)
    )


# ---------------- 存储 ----------------

class TaskStore:
    """任务持久化存储

    - save 时直接写 tasks.json（任务量小，无需原子写）
    - 超限裁剪最旧任务（裁剪前写入 archive_file 归档）
    - 进度节点 items 有去重与限流能力（记录最近 N 条，避免刷屏存储）
    """

    def __init__(
        self,
        data_file: str,
        max_tasks: int = 50,
        max_items_per_task: int = 20,
        archive_file: str | None = None,
        archive_max: int = 200,
    ):
        self.data_file = data_file
        self.max_tasks = max(1, int(max_tasks))
        self.max_items = max(1, int(max_items_per_task))
        self.archive_file = archive_file
        self.archive_max = max(1, int(archive_max))
        self._tasks: list[Task] = []

    def _ensure_dir(self):
        parent = os.path.dirname(self.data_file)
        if parent:
            os.makedirs(parent, exist_ok=True)

    def load(self):
        """读取 tasks.json；无法读取则视为空仓库。
        running/pending 任务改为 failed（进程中断）。"""
        self._tasks = []
        if not os.path.exists(self.data_file):
            return
        try:
            with open(self.data_file, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            lst = raw if isinstance(raw, list) else raw.get("tasks", [])
            for d in lst:
                t = Task.from_dict(d)
                if t.status in ("pending", "running"):
                    t.status = "failed"
                    t.error = "插件重启/会话中断，任务未完成"
                    t.finished_at = now_iso()
                self._tasks.append(t)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"读取任务历史失败: {e}")
        self.prune()

    def save(self):
        self._ensure_dir()
        try:
            with open(self.data_file, "w", encoding="utf-8") as fh:
                json.dump([t.to_dict() for t in self._tasks], fh, ensure_ascii=False, indent=2)
        except OSError as e:
            logger.warning(f"保存任务历史失败: {e}")

    def _load_archived(self) -> list[Task]:
        """读取归档文件（不存在或损坏时返回空列表）"""
        if not self.archive_file or not os.path.exists(self.archive_file):
            return []
        try:
            with open(self.archive_file, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            lst = raw if isinstance(raw, list) else raw.get("archived", [])
            return [Task.from_dict(d) for d in lst]
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"读取任务归档失败: {e}")
            return []

    def _save_archived(self, tasks: list[Task]):
        """覆写归档文件（仅保留最近 archive_max 条）"""
        if not self.archive_file:
            return
        self._ensure_dir()
        try:
            with open(self.archive_file, "w", encoding="utf-8") as fh:
                json.dump(
                    [t.to_dict() for t in tasks],
                    fh,
                    ensure_ascii=False,
                    indent=2,
                )
        except OSError as e:
            logger.warning(f"保存任务归档失败: {e}")

    def prune(self, archive_max: int = 0):
        if len(self._tasks) <= self.max_tasks:
            return
        dropped = self._tasks[: len(self._tasks) - self.max_tasks]
        self._tasks = self._tasks[len(self._tasks) - self.max_tasks :]
        if dropped and self.archive_file:
            archived = self._load_archived()
            archived.extend(dropped)
            self._save_archived(archived[- (archive_max or self.archive_max):])

    def list_archived(self, limit: int = 10) -> list[Task]:
        """最近归档的任务（新 → 旧）"""
        archived = self._load_archived()
        return list(archived[-limit:])[::-1]

    def get_archived(self, task_id: str) -> Task | None:
        for t in self._load_archived():
            if t.id == task_id:
                return t
        return None

    def add(self, t: Task) -> Task:
        self._tasks.append(t)
        self.prune()
        self.save()
        return t

    def get(self, task_id: str) -> Task | None:
        return next((t for t in self._tasks if t.id == task_id), None)

    def update(self, task_id: str, save: bool = True, **fields):
        """就地更新任务字段并按需保存（事件高频更新时可传 save=False 延迟落盘）"""
        t = self.get(task_id)
        if t is None:
            return None
        for k, v in fields.items():
            if hasattr(t, k):
                setattr(t, k, v)
        if save:
            self.save()
        return t

    def add_item(self, task_id: str, item: dict):
        """追加一条进度节点（带长度裁剪，不立刻落盘）"""
        t = self.get(task_id)
        if t is None:
            return
        t.items.append(item)
        del t.items[: -self.max_items]

    def running_tasks(self) -> list[Task]:
        return [t for t in self._tasks if t.status == "running"]

    def list_recent(self, limit: int = 10) -> list[Task]:
        return list(self._tasks[-limit:])

    def next_id(self) -> str:
        """唯一任务 id（时间戳 + 序号，避免同秒冲突）"""
        self._seq = (getattr(self, "_seq", 0) + 1) % 1000
        return f"{int(time.time())}{self._seq:03d}"


# ---------------- 事件分发 ----------------

class ProgressHub:
    """任务进度事件的限流与广播分发。

    - 同一会话同一工具连续相同事件去重
    - 相同类型事件设置最小间隔（毫秒），防刷屏
    - callback 通过 asyncio 外部循环安全投递
    """

    def __init__(self, callback: Callable | None = None, min_interval_ms: int = 2000):
        self.callback = callback
        self.min_interval = min_interval_ms / 1000.0
        self._last_sent: dict[str, float] = {}
        self._last_event: dict[str, str] = {}

    def set_callback(self, callback: Callable | None):
        self.callback = callback

    def should_emit(self, dedup_key: str, event_text: str) -> bool:
        now = time.time()
        last_t = self._last_sent.get(dedup_key, 0)
        last_e = self._last_event.get(dedup_key, "")
        # 相同事件在最小间隔（至少 0.05s 窗口）内不重复推送
        if now - last_t < max(self.min_interval, 0.05) and event_text == last_e:
            return False
        self._last_sent[dedup_key] = now
        self._last_event[dedup_key] = event_text
        return True

    async def emit(self, task_id: str, kind: str, text: str):
        key = f"{task_id}:{kind}"
        if not self.should_emit(key, text):
            return
        if self.callback:
            res = self.callback(task_id, kind, text)
            if asyncio.iscoroutine(res):
                await res