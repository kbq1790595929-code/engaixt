"""Real-time translation pipeline.

Architecture (capture-then-translate, no third-party source in the path):
- Single consumer thread per translator instance
- PriorityQueue: callback requests (priority=1) ahead of auto (priority=0)
- "Newest wins": if queue has newer request, abandon current (raise Interrupted)
- Cache: only successful translations cached (failures never poison cache)
- Dedup: cache hit → skip API call entirely
"""
from __future__ import annotations

import hashlib
import heapq
import threading
from typing import Callable

from translators.cache import get_cache
from utils.logger import debug, info, warning


class Interrupted(Exception):
    pass


class _PriorityQueue:
    def __init__(self):
        self._heap: list = []
        self._sema = threading.Semaphore(0)
        self._idx = 0

    def put(self, item, priority: int = 0):
        heapq.heappush(self._heap, (-priority, self._idx, item))
        self._idx += 1
        self._sema.release()

    def get(self):
        self._sema.acquire()
        return heapq.heappop(self._heap)[-1]

    def empty(self) -> bool:
        return len(self._heap) == 0


class _WorkerThread(threading.Thread):
    def __init__(self, fn: Callable):
        super().__init__(daemon=True)
        self._fn = fn
        self.result = None
        self.exception: Exception | None = None
        self.done = False

    def run(self):
        try:
            self.result = self._fn()
        except Exception as e:
            self.exception = e
        finally:
            self.done = True

    def wait(self, keep_going: Callable[[], bool]):
        """Block until done OR keep_going() returns False."""
        while not self.done:
            if not keep_going():
                raise Interrupted()
            self.join(0.1)
        if self.exception:
            raise self.exception
        return self.result


class RealtimeTranslator:
    """Wraps any translate_one callable with a queue + cache."""

    def __init__(
        self,
        translate_fn: Callable[[str], str],
        source_lang: str = "ja",
        target_lang: str = "zh-CN",
    ):
        """
        Args:
            translate_fn: Synchronous function text→text. Must raise on failure.
            source_lang / target_lang: Used as cache key.
        """
        self._fn = translate_fn
        self._src = source_lang
        self._tgt = target_lang
        self._cache = get_cache()
        self._queue: _PriorityQueue = _PriorityQueue()
        self._thread = threading.Thread(target=self._consumer, daemon=True)
        self._thread.start()

    # ── public ──────────────────────────────────────────────────────────────

    def submit(
        self,
        text: str,
        on_result: Callable[[str, str], None],
        priority: int = 0,
    ):
        """Enqueue a translation request.

        Args:
            text: Source text.
            on_result: Called with (original, translated) on success.
            priority: 1 = high (inline/embed), 0 = normal auto.
        """
        self._queue.put((text, on_result), priority=priority)

    def translate_blocking(self, text: str) -> str | None:
        """Synchronous translation with cache. Returns None on failure."""
        cached = self._cache.get(text, self._src, self._tgt)
        if cached:
            return cached
        try:
            result = self._fn(text)
            if result and result != text:
                self._cache.set(text, result, self._src, self._tgt)
            return result
        except Exception as e:
            warning(f"[realtime] translate error: {e}")
            return None

    # ── internal ─────────────────────────────────────────────────────────────

    def _consumer(self):
        while True:
            text, on_result = self._queue.get()

            # Cache hit — skip API entirely
            cached = self._cache.get(text, self._src, self._tgt)
            if cached:
                try:
                    on_result(text, cached)
                except Exception:
                    pass
                continue

            # "Newest wins": keep going only if queue is still empty
            keep_going = lambda: self._queue.empty()

            worker = _WorkerThread(lambda t=text: self._fn(t))
            worker.start()
            try:
                result = worker.wait(keep_going)
                if result and result != text:
                    self._cache.set(text, result, self._src, self._tgt)
                    try:
                        on_result(text, result)
                    except Exception:
                        pass
            except Interrupted:
                # A newer request arrived — abandon this result (but worker still
                # runs to completion in background and its result is cached above
                # on next cache check). Actually we need to wait and cache async.
                threading.Thread(target=self._cache_async, args=(text, worker), daemon=True).start()
            except Exception as e:
                warning(f"[realtime] translate failed (not cached): {e}")

    def _cache_async(self, text: str, worker: _WorkerThread):
        """Wait for an abandoned worker and cache its result silently."""
        try:
            worker.join()
            if worker.result and worker.result != text:
                self._cache.set(text, worker.result, self._src, self._tgt)
                debug(f"[realtime] cached abandoned result for: {text[:40]}")
        except Exception:
            pass
