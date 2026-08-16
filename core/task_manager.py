"""统一后台任务管理器。

GUI(app.py) 的所有长任务线程统一经由 TaskManager.submit 派发，共享同一套
状态 / 进度归属 / 取消 / 错误 / 结果模型：

- 状态流转：pending → running → success / failed / cancelled
- 取消是协作式的：cancel() 只设置 cancel_event，任务函数自行检查后退出
- counted=True 的任务参与"活动任务计数"（对应旧版 Api._begin/_end_active_task
  的计数集合）；全部活动任务结束（计数归零）时触发 on_active_drained 回调
- begin_external/end_external 是旧版计数器的兼容入口，供尚未收编成
  submit 形式的调用方（或外部隐性依赖）使用，与 counted 任务共享同一本账

本模块是纯基础设施：只依赖 stdlib 与 utils.logger，
不 import app、不 import pipeline、不 import engines。
"""

from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from utils.logger import get_logger


@dataclass
class Task:
    """一次后台长任务的完整台账。"""

    id: str                 # 短 id，进程内自增，如 "t1"、"t2"
    name: str               # 人类可读名："翻译"、"安装工具"、"实时翻译(kirikiri)"...
    kind: str = "misc"      # translate / realtime / tools / app_update / uninstall / cache / misc
    status: str = "pending"  # pending / running / success / failed / cancelled
    counted: bool = True    # 是否计入活动任务计数
    error: str | None = None
    result: Any = None
    started_at: float | None = None
    ended_at: float | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    cancel_requested: bool = False
    # detached=True：任务函数声明"本任务不落账收尾"（进程即将退出等场景），
    # 函数返回后状态保持 running，不减计数、不触发 drained 回调。
    detached: bool = False
    # never_ends=True：submit 时静态声明正常返回后保持 running（等价于函数
    # 返回前自动 detach）。update_app 的"已是最新版本"等早退路径也是正常返回
    # 且必须正常收尾，所以 update_app 不用本标志，而是在成功启动更新器后由
    # 任务函数自己调用 task.detach()——两种机制二选一，按路径是否可静态判定选择。
    never_ends: bool = False

    def request_cancel(self):
        """协作式取消：设置事件并留痕，任务函数自行检查退出。"""
        self.cancel_requested = True
        self.cancel_event.set()

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def detach(self):
        """任务函数内调用：本任务不再由管理器收尾（保持 running，不减计数）。"""
        self.detached = True

    def to_dict(self) -> dict:
        """GUI 序列化视图（result 仅透出 JSON 安全的原始类型）。"""
        result = self.result
        if not isinstance(result, (str, int, float, bool, type(None))):
            result = str(result)
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "status": self.status,
            "counted": self.counted,
            "error": self.error,
            "result": result,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "cancel_requested": self.cancel_requested,
        }


class TaskManager:
    """收编 GUI 全部长任务线程的统一管理器。"""

    def __init__(self, on_active_drained: Callable[[], None] | None = None):
        self._lock = threading.Lock()
        self._tasks: dict[str, Task] = {}
        self._order: list[str] = []
        self._ids = itertools.count(1)
        self._external = 0  # 兼容旧 begin/end 计数器的外部计数
        self._on_active_drained = on_active_drained

    # ── 提交与执行 ──

    def submit(self, name: str, fn: Callable[[Task], Any], *,
               kind: str = "misc", counted: bool = True,
               never_ends: bool = False) -> Task:
        """派发 daemon 线程执行 fn(task)，返回任务台账。

        计数在 submit 时同步生效（等价于旧版在起线程前 _begin_active_task），
        异常被捕获进 task.error 并标 failed，同时 logger.warning 留痕，不外抛。
        """
        with self._lock:
            task_id = f"t{next(self._ids)}"
            task = Task(id=task_id, name=str(name), kind=str(kind),
                        counted=bool(counted), never_ends=bool(never_ends))
            task.status = "running"
            task.started_at = time.time()
            self._tasks[task_id] = task
            self._order.append(task_id)
        thread = threading.Thread(
            target=self._run_task, args=(task, fn),
            daemon=True, name=f"task-{task.id}-{task.kind}",
        )
        thread.start()
        return task

    def _run_task(self, task: Task, fn: Callable[[Task], Any]):
        error: BaseException | None = None
        try:
            task.result = fn(task)
        except BaseException as exc:  # 不吞：落账 + 日志，不让线程静默死亡
            error = exc
            try:
                get_logger().warning("任务 %s(%s) 异常: %s", task.name, task.id, exc)
            except Exception:
                pass
        self._finish(task, error)

    def _finish(self, task: Task, error: BaseException | None):
        fire = False
        with self._lock:
            if task.detached or (task.never_ends and error is None):
                # 进程即将退出（update_app 成功路径）等：保持 running，
                # 不减计数、不触发 drained 回调。
                return
            if error is not None:
                task.status = "failed"
                task.error = str(error)
            elif task.cancel_requested:
                task.status = "cancelled"
            else:
                task.status = "success"
            task.ended_at = time.time()
            fire = task.counted and self._active_count_locked() == 0
        if fire:
            self._notify_drained()

    # ── 取消与查询 ──

    def cancel(self, task_id: str) -> bool:
        """协作式取消：设 cancel_event 并标记 cancel_requested。"""
        task = self.get(task_id)
        if task is None or task.status not in {"pending", "running"}:
            return False
        task.request_cancel()
        return True

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            return self._tasks.get(str(task_id))

    def list(self) -> list[dict]:
        with self._lock:
            return [self._tasks[tid].to_dict() for tid in self._order]

    # ── 活动任务计数（旧 _begin/_end_active_task 语义） ──

    def _active_count_locked(self) -> int:
        running = sum(
            1 for t in self._tasks.values() if t.counted and t.status == "running"
        )
        return running + self._external

    def active_count(self) -> int:
        """counted 且 running 的任务数 + 外部兼容计数。"""
        with self._lock:
            return self._active_count_locked()

    def begin_external(self):
        """兼容旧 Api._begin_active_task：外部计数 +1。"""
        with self._lock:
            self._external += 1

    def end_external(self):
        """兼容旧 Api._end_active_task：外部计数 -1（下限 0），归零时触发回调。

        与旧实现一致：即使计数已为 0，再次 end 仍会触发一次 drained 回调。
        """
        fire = False
        with self._lock:
            self._external = max(0, self._external - 1)
            fire = self._active_count_locked() == 0
        if fire:
            self._notify_drained()

    def _notify_drained(self):
        if not self._on_active_drained:
            return
        try:
            self._on_active_drained()
        except Exception as exc:
            try:
                get_logger().warning("on_active_drained 回调异常: %s", exc)
            except Exception:
                pass
