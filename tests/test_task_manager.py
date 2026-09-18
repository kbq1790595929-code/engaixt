"""core/task_manager.py 单元测试 + Api 集成冒烟。

覆盖点：
1. 生命周期：submit → running → success，result/started_at/ended_at 落账
2. 异常捕获：fn 抛错 → failed + error 字符串，不向外抛
3. 协作式取消：cancel 设事件，fn 检查后退出 → cancelled
4. 活动计数：counted 并发任务 active_count 正确，归零时 on_active_drained 恰好一次；
   counted=False 不计数不触发
5. never_ends / detach：正常返回后保持 running，不减计数不触发回调
6. 兼容垫片：begin_external/end_external 与旧 _begin/_end_active_task 语义一致
7. Api 集成：无窗口实例化 Api，任务经 list_tasks 可见、cancel_task 生效
"""

from __future__ import annotations

import threading
import time

from core.task_manager import TaskManager


def _wait_until(cond, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_submit_lifecycle_success_records_result_and_times():
    mgr = TaskManager()
    release = threading.Event()

    def fn(task):
        release.wait(5)
        return 42

    task = mgr.submit("测试任务", fn, kind="misc")
    assert task.id == "t1"
    assert task.status == "running"
    assert task.started_at is not None
    assert task.ended_at is None

    release.set()
    assert _wait_until(lambda: task.status == "success")
    assert task.result == 42
    assert task.error is None
    assert task.ended_at is not None
    assert task.ended_at >= task.started_at


def test_submit_captures_exception_as_failed_without_raising():
    mgr = TaskManager()

    def fn(task):
        raise RuntimeError("boom")

    task = mgr.submit("会失败的任务", fn)
    assert _wait_until(lambda: task.status == "failed")
    assert task.error == "boom"
    assert task.result is None


def test_cooperative_cancel_marks_cancelled():
    mgr = TaskManager()
    started = threading.Event()

    def fn(task):
        started.set()
        while not task.cancel_event.is_set():
            time.sleep(0.01)

    task = mgr.submit("可取消任务", fn, kind="realtime")
    assert started.wait(5)
    assert mgr.cancel(task.id) is True
    assert task.cancel_requested is True
    assert task.cancelled is True
    assert _wait_until(lambda: task.status == "cancelled")
    # 已结束的任务再取消返回 False
    assert mgr.cancel(task.id) is False
    # 不存在的任务
    assert mgr.cancel("t999") is False


def test_counted_tasks_drive_active_count_and_drained_fires_once():
    drained = []
    mgr = TaskManager(on_active_drained=lambda: drained.append(1))
    gate1, gate2 = threading.Event(), threading.Event()

    t1 = mgr.submit("任务一", lambda task: gate1.wait(5), counted=True)
    t2 = mgr.submit("任务二", lambda task: gate2.wait(5), counted=True)
    assert mgr.active_count() == 2
    assert drained == []

    gate1.set()
    assert _wait_until(lambda: t1.status == "success")
    assert mgr.active_count() == 1
    assert drained == []

    gate2.set()
    assert _wait_until(lambda: t2.status == "success")
    assert mgr.active_count() == 0
    assert _wait_until(lambda: len(drained) == 1)
    assert drained == [1]


def test_uncounted_tasks_do_not_count_or_fire_drained():
    drained = []
    mgr = TaskManager(on_active_drained=lambda: drained.append(1))
    gate = threading.Event()

    task = mgr.submit("后台任务", lambda task: gate.wait(5), counted=False)
    assert mgr.active_count() == 0

    gate.set()
    assert _wait_until(lambda: task.status == "success")
    assert drained == []


def test_never_ends_task_stays_running_and_keeps_count():
    drained = []
    mgr = TaskManager(on_active_drained=lambda: drained.append(1))

    task = mgr.submit("退出前任务", lambda task: None, counted=True, never_ends=True)
    # fn 已正常返回，任务仍保持 running（进程即将退出的语义）
    time.sleep(0.2)
    assert task.status == "running"
    assert task.ended_at is None
    assert mgr.active_count() == 1
    assert drained == []


def test_never_ends_task_failure_still_ends_normally():
    drained = []
    mgr = TaskManager(on_active_drained=lambda: drained.append(1))

    def fn(task):
        raise RuntimeError("update failed")

    task = mgr.submit("更新失败", fn, counted=True, never_ends=True)
    assert _wait_until(lambda: task.status == "failed")
    assert task.error == "update failed"
    assert mgr.active_count() == 0
    assert _wait_until(lambda: len(drained) == 1)


def test_detach_inside_fn_keeps_task_running():
    drained = []
    mgr = TaskManager(on_active_drained=lambda: drained.append(1))

    def fn(task):
        task.detach()

    task = mgr.submit("detach 任务", fn, counted=True)
    time.sleep(0.2)
    assert task.status == "running"
    assert mgr.active_count() == 1
    assert drained == []


def test_list_returns_serializable_snapshot_in_submit_order():
    mgr = TaskManager()
    gate = threading.Event()
    t1 = mgr.submit("先提交", lambda task: gate.wait(5), kind="translate")
    t2 = mgr.submit("后提交", lambda task: object(), kind="tools", counted=False)
    assert _wait_until(lambda: t2.status == "success")

    items = mgr.list()
    assert [item["id"] for item in items] == [t1.id, t2.id]
    assert items[0]["name"] == "先提交"
    assert items[0]["kind"] == "translate"
    assert items[0]["status"] == "running"
    assert items[0]["counted"] is True
    # result 非 JSON 原始类型时序列化为字符串
    assert isinstance(items[1]["result"], str)
    import json
    json.dumps(items, ensure_ascii=False)
    gate.set()
    assert _wait_until(lambda: t1.status == "success")


def test_external_begin_end_compat_semantics():
    drained = []
    mgr = TaskManager(on_active_drained=lambda: drained.append(1))

    mgr.begin_external()
    assert mgr.active_count() == 1
    assert drained == []
    mgr.end_external()
    assert mgr.active_count() == 0
    assert drained == [1]
    # 与旧实现一致：计数已为 0 再 end 仍触发一次回调
    mgr.end_external()
    assert mgr.active_count() == 0
    assert drained == [1, 1]


def test_api_integration_list_tasks_and_cancel_task():
    from app import Api

    api = Api()
    js_calls = []
    api._js = lambda code: js_calls.append(code)

    gate = threading.Event()
    started = threading.Event()

    def fake(task):
        started.set()
        gate.wait(5)

    task = api._tasks.submit("假任务", fake, kind="misc", counted=True)
    assert started.wait(5)
    assert api._is_active_task_running() is True

    items = api.list_tasks()
    assert any(item["id"] == task.id and item["name"] == "假任务" for item in items)

    assert api.cancel_task(task.id) == {"ok": True}
    gate.set()
    assert _wait_until(lambda: task.status == "cancelled")
    assert api._is_active_task_running() is False
    assert not any("refreshLicenseStatus" in code for code in js_calls)
    assert api.cancel_task("t999") == {"ok": False}
