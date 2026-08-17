"""定时任务调度：基于 AstrBot 内置 cron_manager 的周期性任务。

- schedule.json 持久化定时任务定义（cron 表达式 + 描述 + 发起人）
- 插件加载时注册到 context.cron_manager；无 cron_manager 的老版本环境安全降级
- 支持「定时 列表 / 定时 删除」管理命令
"""

import asyncio
import json
import os
import re
import time
from typing import Callable

from astrbot.api import logger

# 5 段 crontab：分 时 日 月 周（每段支持 *、数字、, - 组合）
_CRON_FIELD = re.compile(r"^(\*|(\d+(-\d+)?)(,(\d+(-\d+)?))*)$")
_CRON_PART = ("分", "时", "日", "月", "周")
# 各字段合法取值范围（cron 惯例：周 0-7，0 与 7 都表示周日）
_CRON_RANGE = (("minute", 0, 59), ("hour", 0, 23), ("day", 1, 31), ("month", 1, 12), ("week", 0, 7))


class ScheduleEntry:
    """一条定时任务定义"""

    def __init__(
        self,
        entry_id: str,
        cron: str,
        desc: str,
        creator_umo: str,
        creator_self_id: str = "",
        enabled: bool = True,
        created_at: str = "",
    ):
        self.id = entry_id
        self.cron = cron
        self.desc = desc
        self.creator_umo = creator_umo
        self.creator_self_id = creator_self_id
        self.enabled = enabled
        self.created_at = created_at

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "cron": self.cron,
            "desc": self.desc,
            "creator_umo": self.creator_umo,
            "creator_self_id": self.creator_self_id,
            "enabled": self.enabled,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ScheduleEntry":
        return cls(
            entry_id=d.get("id", ""),
            cron=d.get("cron", ""),
            desc=d.get("desc", ""),
            creator_umo=d.get("creator_umo", ""),
            creator_self_id=d.get("creator_self_id", ""),
            enabled=bool(d.get("enabled", True)),
            created_at=d.get("created_at", ""),
        )


def _in_range(values: list[str], low: int, high: int) -> bool:
    """校验数值（含区间 1-5）是否在合法范围内"""
    for v in values:
        if "-" in v:
            a, b = v.split("-", 1)
            if not a.isdigit() or not b.isdigit():
                return False
            if not (low <= int(a) <= high and low <= int(b) <= high and int(a) <= int(b)):
                return False
        elif not v.isdigit() or not (low <= int(v) <= high):
            return False
    return True


def validate_cron(expr: str) -> bool:
    """校验 5 段 crontab 表达式格式与取值范围"""
    parts = expr.strip().split()
    if len(parts) != 5:
        return False
    for i, p in enumerate(parts):
        low, high = _CRON_RANGE[i][1], _CRON_RANGE[i][2]
        p = p.strip()
        if p == "*":
            continue
        if "/" in p:
            base, step = p.split("/", 1)
            if not step.isdigit() or int(step) < 1:
                return False
            if base == "*":
                continue
            if not _CRON_FIELD.match(base):
                return False
            if not _in_range(base.split(","), low, high):
                return False
        elif _CRON_FIELD.match(p):
            if not _in_range(p.split(","), low, high):
                return False
        else:
            return False
    return True


class ScheduleStore:
    """定时任务定义的持久化存储"""

    def __init__(self, data_file: str):
        self.data_file = data_file
        self._entries: list[ScheduleEntry] = []

    def _ensure_dir(self):
        parent = os.path.dirname(self.data_file)
        if parent:
            os.makedirs(parent, exist_ok=True)

    def load(self):
        self._entries = []
        if not os.path.exists(self.data_file):
            return
        try:
            with open(self.data_file, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            lst = raw if isinstance(raw, list) else raw.get("schedule", [])
            self._entries = [ScheduleEntry.from_dict(d) for d in lst]
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"读取定时任务失败: {e}")

    def save(self):
        self._ensure_dir()
        try:
            with open(self.data_file, "w", encoding="utf-8") as fh:
                json.dump(
                    [e.to_dict() for e in self._entries],
                    fh,
                    ensure_ascii=False,
                    indent=2,
                )
        except OSError as e:
            logger.warning(f"保存定时任务失败: {e}")

    def add(self, entry: ScheduleEntry) -> ScheduleEntry:
        self._entries.append(entry)
        self.save()
        return entry

    def get(self, entry_id: str) -> ScheduleEntry | None:
        return next((e for e in self._entries if e.id == entry_id), None)

    def remove(self, entry_id: str) -> bool:
        for i, e in enumerate(self._entries):
            if e.id == entry_id:
                del self._entries[i]
                self.save()
                return True
        return False

    def all(self) -> list[ScheduleEntry]:
        return list(self._entries)

    def next_id(self, used: set[str]) -> str:
        n = 1
        while f"s{n}" in used:
            n += 1
        return f"s{n}"


class ScheduleManager:
    """定时任务调度管理：注册/注销 cron job 到 AstrBot 内置调度器"""

    def __init__(self, store: ScheduleStore, submit: Callable):
        self.store = store
        self.submit = submit  # 回调: async (desc, creator_umo, creator_self_id) -> str
        self._registered: dict[str, str] = {}  # entry_id -> cron job 名称
        self._pending_tasks: set = set()  # 持有 fire-and-forget 任务引用防 GC
        self._seq = 0

    def _job_name(self) -> str:
        self._seq += 1
        return f"remote_task_sched_{int(time.time())}_{self._seq}"

    async def register_all(self, cron_manager) -> int:
        """把启用的定时任务注册进调度器，返回成功注册数（无 cron_manager 返回 0）"""
        if cron_manager is None:
            return 0
        n = 0
        for e in self.store.all():
            if not e.enabled or not validate_cron(e.cron):
                continue
            try:
                name = self._job_name()
                await cron_manager.add_basic_job(
                    name=name,
                    cron_expression=e.cron,
                    handler=self._make_handler(e),
                    description=f"remote_task 定时任务 {e.id}: {e.desc[:50]}",
                    payload={"entry_id": e.id},
                )
                self._registered[e.id] = name
                n += 1
            except Exception as ex:  # noqa: BLE001
                logger.warning(f"注册定时任务 {e.id} 失败: {ex}")
        return n

    def _make_handler(self, entry: ScheduleEntry):
        def handler(**kwargs):
            import asyncio

            try:
                res = self.submit(
                    entry.desc, entry.creator_umo, entry.creator_self_id
                )
                if asyncio.iscoroutine(res):
                    # 调度器可能在非插件事件循环触发，用 create_task 投递
                    try:
                        asyncio.get_running_loop()
                    except RuntimeError:
                        return
                    task = asyncio.create_task(res)
                    # 保留引用防止 GC，并记录未捕获异常
                    self._pending_tasks.add(task)
                    task.add_done_callback(self._pending_tasks.discard)
            except Exception as ex:  # noqa: BLE001
                logger.warning(f"定时任务 {entry.id} 触发失败: {ex}")

        return handler

    async def remove_job(self, cron_manager, entry_id: str):
        """删除调度器中的注册（若存在）"""
        name = self._registered.pop(entry_id, None)
        if name and cron_manager is not None:
            try:
                await cron_manager.delete_job(name)
            except Exception as ex:  # noqa: BLE001
                logger.warning(f"删除定时任务 {entry_id} 注册失败: {ex}")

    async def clear_all(self, cron_manager):
        for eid in list(self._registered):
            await self.remove_job(cron_manager, eid)
