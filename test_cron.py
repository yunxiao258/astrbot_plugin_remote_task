"""astrbot_plugin_remote_task cron 解析器测试：标准 5 段语法、触发时间计算。

运行：python test_cron.py
纯标准库，不依赖 AstrBot。
"""

import os
import sys
from datetime import datetime

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_PLUGIN_DIR))

from astrbot_plugin_remote_task.cron import CronExpr, validate_cron  # noqa: E402

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


def test_parse_valid():
    print("[合法表达式解析]")
    good = [
        "0 9 * * *",          # 每天 9:00
        "* * * * *",          # 每分钟
        "*/5 * * * *",        # 每 5 分钟
        "0,30 9-18 * * 1-5",  # 工作日 9-18 点整点和半点
        "15 3 * * 0",         # 周日 3:15
        "0 0 1 * *",          # 每月 1 号 0 点
        "30 2 * * 7",         # 周日（7）2:30
        "1-5 * * * *",        # 每分钟 1-5 分
        "0 9 * * *",          # 重复
    ]
    for expr in good:
        check(f"解析成功: {expr}", CronExpr.parse(expr) is not None)
    check("validate_cron 兼容", validate_cron("*/15 * * * *") is True)


def test_parse_invalid():
    print("[非法表达式拒绝]")
    bad = [
        "0 9 * *",        # 4 段
        "0 9 * * * *",    # 6 段
        "a b c d e",      # 非数字
        "",               # 空
        "60 9 * * *",     # 分越界
        "0 24 * * *",     # 时越界
        "0 9 32 * *",     # 日越界
        "0 9 * 13 *",     # 月越界
        "0 9 * * 8",      # 周越界
        "*/0 * * * *",    # 步进 0
        "5-1 * * * *",    # 区间倒置
        "0 9 * * 1-",     # 残区间
        "0 9 * * 1,",     # 尾逗号
        "0 9 * * */x",    # 步进非数字
    ]
    for expr in bad:
        check(f"拒绝: {expr!r}", CronExpr.parse(expr) is None)
    check("validate_cron 非法", validate_cron("bad") is False)


def test_matches():
    print("[命中判定]")
    daily = CronExpr.parse("0 9 * * *")
    check("每天 9:00 命中", daily.matches(datetime(2026, 8, 17, 9, 0)))
    check("9:01 不命中", not daily.matches(datetime(2026, 8, 17, 9, 1)))
    check("10:00 不命中", not daily.matches(datetime(2026, 8, 17, 10, 0)))

    every5 = CronExpr.parse("*/5 * * * *")
    check("每 5 分钟命中", every5.matches(datetime(2026, 8, 17, 10, 5)))
    check("非 5 倍数不命中", not every5.matches(datetime(2026, 8, 17, 10, 6)))

    work = CronExpr.parse("0,30 9-18 * * 1-5")
    # 2026-08-17 是周一
    check("工作日 09:30 命中", work.matches(datetime(2026, 8, 17, 9, 30)))
    check("工作日 09:15 不命中", not work.matches(datetime(2026, 8, 17, 9, 15)))
    check("工作日 08:30 不命中", not work.matches(datetime(2026, 8, 17, 8, 30)))
    # 2026-08-15 是周六
    check("周六 09:00 不命中", not work.matches(datetime(2026, 8, 15, 9, 0)))

    sunday = CronExpr.parse("15 3 * * 0")
    # 2026-08-16 是周日
    check("周日 03:15 命中", sunday.matches(datetime(2026, 8, 16, 3, 15)))
    check("周一 03:15 不命中", not sunday.matches(datetime(2026, 8, 17, 3, 15)))
    # 周字段 7 等价于 0
    sunday7 = CronExpr.parse("15 3 * * 7")
    check("周 7 同周日", sunday7.matches(datetime(2026, 8, 16, 3, 15)))

    monthly = CronExpr.parse("0 0 1 * *")
    check("每月 1 号命中", monthly.matches(datetime(2026, 8, 1, 0, 0)))
    check("2 号不命中", not monthly.matches(datetime(2026, 8, 2, 0, 0)))


def test_or_semantics():
    print("[日/周双限定 OR 语义]")
    # 每月 15 号 或 周一，00:00
    expr = CronExpr.parse("0 0 15 * 1")
    # 2026-08-15 是周六（15 号命中）
    check("15 号命中", expr.matches(datetime(2026, 8, 15, 0, 0)))
    # 2026-08-17 是周一（周命中）
    check("周一命中", expr.matches(datetime(2026, 8, 17, 0, 0)))
    # 2026-08-14 是周五（都不命中）
    check("都不命中", not expr.matches(datetime(2026, 8, 14, 0, 0)))


def test_next_after():
    print("[下次触发时间]")
    daily = CronExpr.parse("0 9 * * *")
    nxt = daily.next_after(datetime(2026, 8, 17, 10, 30))
    check("次日 9:00", nxt == datetime(2026, 8, 18, 9, 0))
    nxt = daily.next_after(datetime(2026, 8, 17, 8, 30))
    check("当天 9:00", nxt == datetime(2026, 8, 17, 9, 0))

    every5 = CronExpr.parse("*/5 * * * *")
    nxt = every5.next_after(datetime(2026, 8, 17, 10, 6))
    check("10:10", nxt == datetime(2026, 8, 17, 10, 10))

    sunday = CronExpr.parse("15 3 * * 0")
    nxt = sunday.next_after(datetime(2026, 8, 17, 0, 0))
    check("下周日 3:15", nxt == datetime(2026, 8, 23, 3, 15))

    work = CronExpr.parse("0 9 * * 1-5")
    nxt = work.next_after(datetime(2026, 8, 14, 18, 0))  # 周五晚
    check("下周一 9:00", nxt == datetime(2026, 8, 17, 9, 0))


def test_next_runs():
    print("[未来 N 天排期]")
    daily = CronExpr.parse("0 9 * * *")
    runs = daily.next_runs(datetime(2026, 8, 17, 0, 0), 3)
    check("每天 9 点共 3 次", len(runs) == 3)
    check("升序且首日为 17 日", runs[0] == datetime(2026, 8, 17, 9, 0))
    check("末日为 19 日", runs[-1] == datetime(2026, 8, 19, 9, 0))

    every30 = CronExpr.parse("*/30 * * * *")
    runs = every30.next_runs(datetime(2026, 8, 17, 0, 0), 1)
    check("每半小时一天 48 次", len(runs) == 48)

    monthly = CronExpr.parse("0 0 1 * *")
    runs = monthly.next_runs(datetime(2026, 8, 1, 0, 0), 31)
    check("31 天内仅 1 号触发", len(runs) == 1)

    # 起始时间已过当次触发 → 不包含过去时间；days 按自然日（当天起算）
    runs = daily.next_runs(datetime(2026, 8, 17, 10, 0), 2)
    check("起始后 2 个自然日排期", len(runs) == 1 and runs[0] == datetime(2026, 8, 18, 9, 0))

    # 无排期：周一起 1 天内的周日任务
    sunday = CronExpr.parse("15 3 * * 0")
    runs = sunday.next_runs(datetime(2026, 8, 17, 0, 0), 1)
    check("1 天内无周日排期", len(runs) == 0)

    # days 钳制
    runs = daily.next_runs(datetime(2026, 8, 17, 0, 0), 0)
    check("days=0 钳制为 1 天", len(runs) == 1)
    runs = daily.next_runs(datetime(2026, 8, 17, 0, 0), 9999)
    check("days 超大钳制 366 天", len(runs) == 366)


def test_defensive():
    print("[防御性]")
    check("None 安全", CronExpr.parse(None) is None)
    check("空白安全", CronExpr.parse("   ") is None)
    check("超长字段拒绝", CronExpr.parse("0 9 * * 1,2,3,4,5,6,7,8") is None)


def run_all():
    test_parse_valid()
    test_parse_invalid()
    test_matches()
    test_or_semantics()
    test_next_after()
    test_next_runs()
    test_defensive()


if __name__ == "__main__":
    run_all()
    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    sys.exit(1 if FAIL else 0)
