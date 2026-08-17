"""astrbot_plugin_remote_task 任务模板库测试：模板数量、查找、参数渲染。

运行：python test_templates.py
纯标准库，不依赖 AstrBot。
"""

import os
import sys

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_PLUGIN_DIR))

from astrbot_plugin_remote_task.templates import TEMPLATES, find_template  # noqa: E402

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


def test_template_count():
    print("[模板数量与字段]")
    check("内置模板 ≥ 6 个", len(TEMPLATES) >= 6)
    names = [t.name for t in TEMPLATES]
    check("英文名唯一", len(names) == len(set(names)))
    for tpl in TEMPLATES:
        check(f"字段齐全: {tpl.name}", bool(tpl.title and tpl.description and tpl.cron and tpl.command))
        from astrbot_plugin_remote_task.cron import CronExpr
        check(f"默认 cron 合法: {tpl.cron}", CronExpr.parse(tpl.cron) is not None)
    check("含每日备份", any(t.name == "daily_backup" for t in TEMPLATES))
    check("含每周统计日报", any(t.name == "weekly_report" for t in TEMPLATES))
    check("含临时文件清理", any(t.name == "temp_clean" for t in TEMPLATES))
    check("含每日新闻推送", any(t.name == "daily_news" for t in TEMPLATES))
    check("含健康检查", any(t.name == "health_check" for t in TEMPLATES))
    check("含每周报告生成", any(t.name == "weekly_summary" for t in TEMPLATES))


def test_find():
    print("[模板查找]")
    check("英文名查找", find_template("daily_backup") is not None)
    check("中文标题查找", find_template("每日备份") is not None)
    check("大小写不敏感", find_template("DAILY_BACKUP") is not None)
    check("空名返回 None", find_template("") is None)
    check("未知名返回 None", find_template("no_such_template") is None)
    check("None 安全", find_template(None) is None)


def test_render():
    print("[参数渲染]")
    tpl = find_template("daily_backup")
    cron, cmd, err = tpl.render({"backup_dir": "D:\\data"})
    check("渲染成功无错误", err == "")
    check("cron 透传", cron == "0 2 * * *")
    check("占位符替换", cmd == "备份目录 D:\\data 到 ./backup，完成后清理 7 天前的旧备份")
    check("默认参数生效", "./backup" in cmd)

    cron, cmd, err = tpl.render({"backup_dir": "D:\\x", "backup_root": "E:\\bak"})
    check("多参数渲染", cmd == "备份目录 D:\\x 到 E:\\bak，完成后清理 7 天前的旧备份")

    cron, cmd, err = tpl.render({})
    check("缺必填参数报错", cron is None and "缺少参数" in err and "backup_dir" in err)

    cron, cmd, err = tpl.render(None)
    check("None 参数缺必填报错", cron is None and "缺少参数" in err)

    # 无参数模板
    health = find_template("health_check")
    cron, cmd, err = health.render({})
    check("无参数模板渲染成功", cron == "*/30 * * * *" and err == "")

    # 脏参数值（空白）视为未提供
    cron, cmd, err = tpl.render({"backup_dir": "  ", "backup_root": "x"})
    check("空白参数视为缺失", cron is None and "backup_dir" in err)

    # 未知参数忽略
    cron, cmd, err = tpl.render({"backup_dir": "D:\\d", "unknown_key": "zzz"})
    check("未知参数忽略", err == "" and "unknown_key" not in cmd)


def test_display():
    print("[展示文本]")
    tpl = find_template("temp_clean")
    lines = tpl.to_lines()
    check("列表行含标题", "定时清理临时文件" in lines[0])
    check("列表行含 cron", "30 3 * * *" in lines[-1])
    detail = "\n".join(tpl.detail_lines())
    check("详情含说明", "清理" in detail)
    check("详情含命令", "temp_dir" in detail)
    check("详情含创建用法", "创建" in detail)


def run_all():
    test_template_count()
    test_find()
    test_render()
    test_display()


if __name__ == "__main__":
    run_all()
    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    sys.exit(1 if FAIL else 0)
