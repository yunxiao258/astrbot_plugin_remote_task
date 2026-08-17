"""标准 5 段 cron 表达式解析器（纯标准库实现，无第三方依赖）。

字段格式（分 时 日 月 周）:
- *          任意值
- 1,2,3      枚举
- 1-5        区间
- */15       步进（从字段下限起）
- 1-30/5     区间内步进
- 5/15       从 5 起每 15（常见实现的兼容写法）

取值范围: 分 0-59、时 0-23、日 1-31、月 1-12、周 0-7（0 与 7 均表示周日）。

语义:
- 日/周双限定时采用标准 cron 的 OR 语义（任一命中即触发）
- 解析失败返回 None，保证调用方（任务日历/校验）面对脏表达式不崩溃
"""

from __future__ import annotations

from datetime import datetime, timedelta

# 各字段合法取值范围（与字段顺序对应）
_FIELD_RANGES = (
    (0, 59),   # 分
    (0, 23),   # 时
    (1, 31),   # 日
    (1, 12),   # 月
    (0, 7),    # 周（0 与 7 均为周日）
)
_FIELD_NAMES = ("分", "时", "日", "月", "周")

# 防御上限：单次扫描最多 366 天（避免脏表达式导致死循环）
_MAX_SCAN_MINUTES = 366 * 24 * 60


def _parse_field(field: str, low: int, high: int) -> set[int] | None:
    """解析单个字段，返回命中的值集合；语法或取值范围错误返回 None"""
    field = field.strip()
    if field == "*":
        return set(range(low, high + 1))
    vals: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            return None
        if "/" in part:
            base, _, step_s = part.partition("/")
            if not step_s.isdigit() or int(step_s) < 1:
                return None
            step = int(step_s)
            if base == "*":
                start, end = low, high
            elif "-" in base:
                a, _, b = base.partition("-")
                if not a.isdigit() or not b.isdigit():
                    return None
                start, end = int(a), int(b)
                if not (low <= start <= high and low <= end <= high and start <= end):
                    return None
            elif base.isdigit():
                start = int(base)
                if not (low <= start <= high):
                    return None
                end = high
            else:
                return None
            vals.update(range(start, end + 1, step))
        elif "-" in part:
            a, _, b = part.partition("-")
            if not a.isdigit() or not b.isdigit():
                return None
            a, b = int(a), int(b)
            if not (low <= a <= high and low <= b <= high and a <= b):
                return None
            vals.update(range(a, b + 1))
        elif part.isdigit():
            v = int(part)
            if not (low <= v <= high):
                return None
            vals.add(v)
        else:
            return None
    return vals


class CronExpr:
    """解析后的 cron 表达式（5 段标准格式）"""

    def __init__(self, minute, hour, day, month, week):
        self.minute = minute
        self.hour = hour
        self.day = day
        self.month = month
        self.week = week  # 已归一化 0-6（7 → 0，周日）
        # 是否全集：用于日/周 OR 语义判断
        self.day_full = len(day) >= 31
        self.week_full = len(week) >= 7

    @classmethod
    def parse(cls, expr: str) -> "CronExpr" | None:
        """解析 5 段 cron 表达式；失败返回 None（不抛异常）"""
        parts = (expr or "").strip().split()
        if len(parts) != 5:
            return None
        fields = []
        for i, part in enumerate(parts):
            low, high = _FIELD_RANGES[i]
            vals = _parse_field(part, low, high)
            if vals is None:
                return None
            fields.append(vals)
        minute, hour, day, month, week = fields
        # 周字段归一化：7 → 0（周日）
        week = {0 if v == 7 else v for v in week}
        return cls(minute, hour, day, month, week)

    def matches(self, dt: datetime) -> bool:
        """dt 是否命中该表达式（忽略秒）"""
        if dt.minute not in self.minute:
            return False
        if dt.hour not in self.hour:
            return False
        if dt.month not in self.month:
            return False
        day_ok = dt.day in self.day
        # cron 惯例周字段: 0/7 周日、1 周一 ... 6 周六；Python weekday() 周一=0，需偏移
        cron_weekday = (dt.weekday() + 1) % 7
        week_ok = cron_weekday in self.week
        if self.day_full and self.week_full:
            return True
        if self.week_full:
            return day_ok
        if self.day_full:
            return week_ok
        # 日/周双限定：标准 cron OR 语义
        return day_ok or week_ok

    def next_after(self, dt: datetime) -> datetime | None:
        """返回 dt 之后（严格大于 dt，分钟粒度）的下一次触发时间；一年内无命中返回 None"""
        cur = dt.replace(second=0, microsecond=0) + timedelta(minutes=1)
        for _ in range(_MAX_SCAN_MINUTES):
            if self.matches(cur):
                return cur
            cur += timedelta(minutes=1)
        return None

    def next_runs(self, start: datetime, days: int) -> list[datetime]:
        """返回从 start 所在分钟起 days 天内的所有触发时间（升序）。

        days 越界时钳制到 1-366；start 所在分钟本身也会被检查（若触发则包含）。
        """
        days = max(1, min(int(days), 366))
        end = start.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=days)
        cur = start.replace(second=0, microsecond=0)
        out: list[datetime] = []
        for _ in range(days * 24 * 60 + 5):
            if cur >= end:
                break
            if self.matches(cur):
                out.append(cur)
            cur += timedelta(minutes=1)
        return out


def validate_cron(expr: str) -> bool:
    """兼容式校验：返回表达式是否合法（供外部简单校验使用）"""
    return CronExpr.parse(expr) is not None
