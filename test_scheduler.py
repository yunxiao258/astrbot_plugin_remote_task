"""astrbot_plugin_remote_task 定时任务测试：cron 校验、持久化、调度注册。

运行：python test_scheduler.py
不依赖 AstrBot 运行实例与真实调度器（用假 cron_manager）。
"""

import os
import sys
import tempfile

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_PLUGIN_DIR))

from astrbot_plugin_remote_task.scheduler import (  # noqa: E402
    ScheduleEntry,
    ScheduleManager,
    ScheduleStore,
    validate_cron,
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


def test_validate_cron():
    print("[cron 校验]")
    good = ["0 9 * * *", "* * * * *", "*/5 * * * *", "0,30 9-18 * * 1-5", "15 3 * * 0"]
    bad = ["0 9 * *", "a b c d e", "0 9 * * * *", "", "60 9 * * *", "0 24 * * *", "0 9 * * * 6", "*/0 * * * *"]
    for expr in good:
        check(f"合法: {expr}", validate_cron(expr))
    for expr in bad:
        check(f"非法: {expr}", not validate_cron(expr))


def test_entry_roundtrip(tmp_path):
    print("[条目持久化]")
    f = os.path.join(tmp_path, "schedule.json")
    store = ScheduleStore(f)
    store.load()
    e = ScheduleEntry(
        entry_id="s1",
        cron="0 9 * * *",
        desc="汇总日报",
        creator_umo="g:1",
        creator_self_id="self1",
    )
    store.add(e)
    check("add 后可在", store.get("s1") is not None)

    store2 = ScheduleStore(f)
    store2.load()
    e2 = store2.get("s1")
    check("重载保留", e2 is not None and e2.cron == "0 9 * * *")
    check("重载保留发起人", e2.creator_umo == "g:1" and e2.creator_self_id == "self1")
    check("默认启用", e2.enabled is True)
    check("from_dict 空安全", ScheduleEntry.from_dict({}).id == "")


def test_store_remove(tmp_path):
    print("[删除条目]")
    f = os.path.join(tmp_path, "schedule.json")
    store = ScheduleStore(f)
    store.add(ScheduleEntry("s1", "0 9 * * *", "a", "u"))
    store.add(ScheduleEntry("s2", "0 10 * * *", "b", "u"))
    check("删除成功", store.remove("s1") is True)
    check("重复删除失败", store.remove("s1") is False)
    check("剩余 1 条", len(store.all()) == 1)
    check("next_id 避开占用", store.next_id({"s2"}) == "s1")


def test_manager_register():
    print("[调度注册]")

    class FakeCron:
        def __init__(self):
            self.jobs = []

        def add_basic_job(self, **kwargs):
            self.jobs.append(kwargs)

        def delete_job(self, name):
            self.jobs = [j for j in self.jobs if j["name"] != name]

    cron = FakeCron()
    store = ScheduleStore(os.path.join(tempfile.mkdtemp(), "s.json"))
    store.add(ScheduleEntry("s1", "0 9 * * *", "a", "u"))
    store.add(ScheduleEntry("s2", "bad expr", "b", "u"))  # 非法表达式跳过
    store.add(ScheduleEntry("s3", "*/10 * * * *", "c", "u", enabled=False))  # 停用跳过

    submitted = []
    mgr = ScheduleManager(store, lambda desc, umo, sid: submitted.append((desc, umo, sid)))
    n = mgr.register_all(cron)
    check("仅注册合法启用条目", n == 1)
    check("job 名称生成", len(cron.jobs) == 1 and "remote_task_sched" in cron.jobs[0]["name"])
    check("cron 表达式透传", cron.jobs[0]["cron_expression"] == "0 9 * * *")

    # 触发 handler：模拟调度器回调
    cron.jobs[0]["handler"]()
    check("handler 触发提交", len(submitted) == 1 and submitted[0][0] == "a")

    # 删除注册
    mgr.remove_job(cron, "s1")
    check("注销后 job 移除", len(cron.jobs) == 0)
    mgr.remove_job(cron, "s1")
    check("重复注销安全", len(cron.jobs) == 0)

    mgr2 = ScheduleManager(store, lambda *a: None)
    check("无 cron_manager 注册 0", mgr2.register_all(None) == 0)


def run_all(tmp_path):
    test_validate_cron()
    test_entry_roundtrip(tmp_path)
    test_store_remove(tmp_path)
    test_manager_register()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp_path:
        run_all(tmp_path)
    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    sys.exit(1 if FAIL else 0)