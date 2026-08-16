from __future__ import annotations

import threading


class RollingTranslationContext:
    def __init__(self, *, limit: int = 3, max_chars: int = 600):
        self.limit = max(1, int(limit))
        self.max_chars = max(80, int(max_chars))
        self._entries: list[dict[str, str]] = []
        self._lock = threading.Lock()

    def add(self, source: str, translated: str, speaker: str = "") -> bool:
        source = str(source or "").strip()
        translated = str(translated or "").strip()
        speaker = str(speaker or "").strip()
        if not source or not translated or source == translated:
            return False
        entry = {
            "source": source[:300],
            "translated": translated[:300],
            "speaker": speaker[:80],
        }
        with self._lock:
            if self._entries and self._entries[-1]["source"] == entry["source"]:
                if self._entries[-1] == entry:
                    return False
                self._entries[-1] = entry
            else:
                self._entries.append(entry)
            self._trim_locked()
        return True

    def snapshot(self) -> list[dict[str, str]]:
        with self._lock:
            return [dict(entry) for entry in self._entries]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def _trim_locked(self) -> None:
        while len(self._entries) > self.limit:
            self._entries.pop(0)
        while len(self._entries) > 1 and sum(
            len(entry["source"]) + len(entry["translated"])
            for entry in self._entries
        ) > self.max_chars:
            self._entries.pop(0)
