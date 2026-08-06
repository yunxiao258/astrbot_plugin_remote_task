"""astrbot_plugin_remote_task 单元测试：任务状态机、持久化、限流。

运行：python test_tracker.py
不依赖 AstrBot 运行实例与 opencode。
"""

import json
import os
import sys
import tempfile
import time

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_PLUGIN_DIR))

from astrbot_plugin_remote_task.tracker import (
    ProgressHub,
    Task,
    TaskStore,
    format_task,
    format_task_list,
    now_iso,
    status_label,
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


def test_task_dataclass():
    print("[Task 数据类]")
    t = Task(id="1", desc="列出文件", creator_umo="g:1")
    check("默认状态 pending", t.status == "pending")
    check("默认 summary 为空", t.summary == "")
    check("默认 pid 为 0", t.pid == 0)
    d = t.to_dict()
    check("to_dict 含关键字段", d["id"] == "1" and d["mode"] == "" and d["items"] == [])
    t2 = Task.from_dict(d)
    check("from_dict 往返", t2.desc == "列出文件" and t2.creator_umo == "g:1")
    t3 = Task.from_dict({})
    check("空 dict 安全", t3.id == "" and t3.status == "pending" and t3.items == [])


def test_status_label():
    print("[状态标签]")
    check("done→已完成", status_label("done") == "已完成")
    check("未知状态原样", status_label("weird") == "weird")


def test_store_basic(tmp_path):
    print("[存储基础]")
    path = os.path.join(tmp_path, "tasks.json")
    store = TaskStore(path, max_tasks=50)
    store.load()
    check("空仓库加载", len(store.list_recent()) == 0)

    t = store.add(Task(id="t1", desc="任务一", creator_umo="u1", mode="run"))
    check("add 后可在仓库中", store.get("t1") is not None)
    check("next_id 递增", len(store.next_id()) >= 10)
    check("os.path.exists 落盘", os.path.exists(path))

    store2 = TaskStore(path, max_tasks=50)
    store2.load()
    check("重载保留任务", store2.get("t1").desc == "任务一")
    check("重载保留模式", store2.get("t1").mode == "run")


def test_store_prune(tmp_path):
    print("[超限裁剪]")
    path = os.path.join(tmp_path, "tasks.json")
    store = TaskStore(path, max_tasks=3)
    store.load()
    for i in range(5):
        store.add(Task(id=f"p{i}", desc=f"任务{i}", creator_umo="u"))
    lst = store.list_recent()
    check("只保留最近 3 条", len(lst) == 3)
    check("最旧的被丢弃", store.get("p0") is None)
    check("最新保留", store.get("p4") is not None)


def test_store_recover_interrupted(tmp_path):
    print("[重启恢复 running→failed]")
    path = os.path.join(tmp_path, "tasks.json")
    store = TaskStore(path, max_tasks=50)
    store.add(Task(id="r1", desc="进行中", creator_umo="u", status="running"))
    store.add(Task(id="r2", desc="排队中", creator_umo="u", status="pending"))
    store.add(Task(id="r3", desc="已完成", creator_umo="u", status="done"))

    store2 = TaskStore(path, max_tasks=50)
    store2.load()
    check("running 恢复为 failed", store2.get("r1").status == "failed")
    check("failed 有中断原因", "重启" in store2.get("r1").error)
    check("pending 恢复为 failed", store2.get("r2").status == "failed")
    check("done 不受影响", store2.get("r3").status == "done")
    check("running_tasks 为空", len(store2.running_tasks()) == 0)


def test_store_update_and_items(tmp_path):
    print("[更新与进度节点]")
    path = os.path.join(tmp_path, "tasks.json")
    store = TaskStore(path, max_tasks=50, max_items_per_task=3)
    t = store.add(Task(id="u1", desc="x", creator_umo="u"))
    store.update("u1", status="running", pid=99)
    check("update 状态", store.get("u1").status == "running")
    check("update pid", store.get("u1").pid == 99)

    for i in range(5):
        store.add_item("u1", {"t": str(i), "kind": "tool", "text": f"e{i}"})
    items = store.get("u1").items
    check("进度节点裁剪到 3 条", len(items) == 3)
    check("保留最新节点", items[-1]["text"] == "e4")

    store.save()
    store3 = TaskStore(path, max_tasks=50, max_items_per_task=3)
    store3.load()
    check("落盘后节点保留", len(store3.get("u1").items) == 3)


def test_store_damaged_file(tmp_path):
    print("[损坏文件容错]")
    path = os.path.join(tmp_path, "tasks.json")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{ 这不是 json")
    store = TaskStore(path, max_tasks=50)
    store.load()
    check("损坏文件读取为空", len(store.list_recent()) == 0)


def test_format():
    print("[格式化]")
    t = Task(id="f1", desc="hello", creator_umo="g:1", mode="serve", status="done",
             summary="结果", created_at="2026-08-05 10:00:00", finished_at="2026-08-05 10:01:00")
    txt = format_task(t)
    check("状态含已完成", "已完成" in txt)
    check("含模式", "serve API" in txt)
    check("含结果", "结果" in txt)

    lst = format_task_list([])
    check("空列表提示", "暂无" in lst)
    lst2 = format_task_list([t])
    check("列表含描述", "hello" in lst2)

    t2 = Task(id="f2", desc="", creator_umo="")
    txt2 = format_task(t2)
    check("空发起人不占行", "发起人" not in txt2)


def test_progress_hub():
    print("[进度限流]")
    seen = []
    hub = ProgressHub(lambda tid, kind, text: seen.append((tid, kind, text)),
                      min_interval_ms=0)
    hub.emit("t", "tool", "bash git push")
    hub.emit("t", "tool", "bash git push")
    hub.emit("t", "tool", "bash git pull")
    check("相同事件连续去重", len(seen) == 2)
    check("不同事件放行", seen[-1][2] == "bash git pull")

    hub2 = ProgressHub(lambda *a: None)
    check("无指令集调用安全", hub2.should_emit("t:tool", "x") is True)
    check("同文本短间隔去重", hub2.should_emit("t:tool", "x") is False)


def test_now_iso():
    print("[时间格式]")
    check("ISO 格式", len(now_iso()) == 19 and " " in now_iso())


def run_all(tmp_path):
    test_task_dataclass()
    test_status_label()
    test_store_basic(tmp_path)
    test_store_prune(tmp_path)
    test_store_recover_interrupted(tmp_path)
    test_store_update_and_items(tmp_path)
    test_store_damaged_file(tmp_path)
    test_format()
    test_progress_hub()
    test_now_iso()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp_path:
        run_all(tmp_path)
    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    sys.exit(1 if FAIL else 0)