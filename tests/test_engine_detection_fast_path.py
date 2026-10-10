from __future__ import annotations

from pathlib import Path

from engines.base import EngineBase, EngineRegistry
from engines.godot_pck import GodotPckEngine
from engines.wolf import WolfEngine

ROOT = Path(__file__).parent.parent


class _ProbeEngine(EngineBase):
    name = "probe"
    label = "Probe"

    def __init__(self, name: str, score: int, calls: list[str]):
        self.name = name
        self._score = score
        self._calls = calls

    def detect(self, path: Path) -> bool:
        return self._score > 0

    def detect_confidence(self, path: Path) -> tuple[int, list[str]]:
        self._calls.append(self.name)
        return self._score, [self.name] if self._score > 0 else []

    def unpack(self, path: Path, workspace: Path):
        return []

    def repack(self, items, workspace: Path) -> None:
        return None


def test_engine_registry_stops_after_high_confidence_match(tmp_path):
    calls: list[str] = []
    registry = EngineRegistry()
    registry.register(_ProbeEngine("low", 10, calls))
    registry.register(_ProbeEngine("high", 97, calls))
    registry.register(_ProbeEngine("slow_after_high", 80, calls))

    candidates = registry.detect_candidates(tmp_path)

    assert calls == ["low", "high"]
    assert [engine.name for engine, _score, _evidence in candidates] == ["high", "low"]


def test_gui_engine_info_uses_candidates_without_second_detection():
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    method_source = source.split("def get_engine_info", 1)[1].split("def find_cover", 1)[0]

    assert "detect_engine_candidates" in method_source
    assert "best = candidates[0][0] if candidates else None" in method_source
    assert "detect_engine(Path(path))" not in method_source


def test_godot_pck_detection_does_not_read_whole_exe(tmp_path, monkeypatch):
    game = tmp_path / "game"
    game.mkdir()
    exe = game / "game.exe"
    exe.write_bytes(b"MZ" + b"\0" * 1024)

    def fail_full_parse(_data):
        raise AssertionError("detect() must not run full embedded PCK parsing")

    monkeypatch.setattr("engines.godot_pck.pck._find_embedded_pck_range", fail_full_parse)

    assert GodotPckEngine().detect(game) is False


def test_wolf_detection_no_longer_uses_unsupported_case_insensitive_glob(tmp_path):
    game = tmp_path / "game"
    data = game / "Data"
    data.mkdir(parents=True)
    (data / "Archive.wolf").write_bytes(b"wolf")

    assert WolfEngine().detect(game) is True
