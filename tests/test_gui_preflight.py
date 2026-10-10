from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from app import Api
from core.gui_preflight import is_realtime_only_engine, preflight_extract


def test_only_unity_engines_skip_static_preflight():
    assert is_realtime_only_engine("kirikiri") is False
    assert is_realtime_only_engine("unity") is True
    assert is_realtime_only_engine("bgi") is False


def test_preflight_reuses_existing_checkpoint_without_pipeline(tmp_path, monkeypatch):
    checkpoint = tmp_path / "_translation_meta" / "translation_checkpoint.json"
    checkpoint.parent.mkdir()
    checkpoint.write_text(json.dumps({
        "items": [
            {"file": "scene.ks", "original": "こんにちは"},
            {"file": "scene.ks", "original": "さようなら"},
        ]
    }, ensure_ascii=False), encoding="utf-8")

    def fail_pipeline(*_args, **_kwargs):
        raise AssertionError("existing checkpoint should be reused")

    monkeypatch.setattr("core.gui_preflight.Pipeline", fail_pipeline)
    metas = []
    progress = []

    assert preflight_extract(
        str(tmp_path),
        engine_name="bgi",
        progress_callback=lambda step, pct: progress.append((step, pct)),
        meta_callback=lambda key, value: metas.append((key, value)),
    ) is True
    assert metas[0][0] == "extraction_stats"
    assert metas[0][1]["text_count"] == 2
    assert metas[0][1]["preflight_reused"] is True
    assert progress[-1] == ("preflight_complete", 100)


def test_preflight_keeps_extraction_stats_from_temporary_archive_workspace(tmp_path, monkeypatch):
    class FakePipeline:
        def __init__(self, progress_callback=None, meta_callback=None):
            self.meta_callback = meta_callback

        def run_with_checkpoint(self, *_args, **_kwargs):
            self.meta_callback("extraction_stats", {
                "text_count": 17,
                "source_chars": 340,
                "file_count": 3,
            })
            return True

    monkeypatch.setattr("core.gui_preflight.Pipeline", FakePipeline)
    monkeypatch.setattr(
        "core.gui_preflight.game_text_stats",
        lambda _path: {"available": False},
    )
    metas = []

    assert preflight_extract(
        str(tmp_path),
        engine_name="bgi",
        meta_callback=lambda key, value: metas.append((key, value)),
    ) is True

    result = next(value for key, value in metas if key == "preflight_result")
    assert result["stats"]["text_count"] == 17
    assert result["stats"]["source_chars"] == 340


def test_api_preflight_is_rejected_when_setting_is_disabled():
    api = Api()
    submitted = []
    api._tasks.submit = lambda *args, **kwargs: submitted.append((args, kwargs))

    with patch(
        "core.gui_preflight_api.get_config",
        return_value=SimpleNamespace(selection_preflight_enabled=False),
    ):
        assert api.preflight_extract("C:/game") == {"started": False, "reason": "disabled"}

    assert submitted == []


def test_api_preflight_marks_all_progress_as_preflight(tmp_path):
    api = Api()
    api.resolve_path = lambda _path: str(tmp_path)
    emitted = []
    api._js = emitted.append
    submitted = []

    def submit(*args, **kwargs):
        submitted.append((args, kwargs))

    api._tasks.submit = submit

    def fake_preflight(_path, **kwargs):
        kwargs["progress_callback"]("提取文本", 100)
        kwargs["meta_callback"]("extraction_stats", {"text_count": 4})
        return True

    with (
        patch(
            "core.gui_preflight_api.get_config",
            return_value=SimpleNamespace(selection_preflight_enabled=True),
        ),
        patch("core.gui_preflight_api.preflight_extract", side_effect=fake_preflight),
    ):
        result = api.preflight_extract(str(tmp_path), "bgi")
        assert result["started"] is True
        runner = submitted[0][0][1]
        assert runner(object()) is True

    assert any('"preflight": true' in code for code in emitted)
    assert any('on_meta("extraction_stats"' in code for code in emitted)
