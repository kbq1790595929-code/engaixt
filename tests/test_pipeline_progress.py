from core.pipeline import Pipeline
from core.pipeline_detect_stage import record_final_extraction
from engines.base import TextItem


def test_item_progress_callback_receives_numeric_counts():
    calls = []
    metas = []
    pipe = Pipeline(
        item_progress_callback=lambda current, total: calls.append((current, total)),
        meta_callback=lambda key, value: metas.append((key, value)),
    )

    pipe._update_progress("translate")
    pipe._update_item_progress(10, 100)

    assert calls == [(10, 100)]
    assert metas[-1] == ("translate_progress", {"current": 10, "total": 100})


def test_item_progress_ignores_regressive_retry_events():
    calls = []
    metas = []
    pipe = Pipeline(
        item_progress_callback=lambda current, total: calls.append((current, total)),
        meta_callback=lambda key, value: metas.append((key, value)),
    )

    pipe._update_progress("translate")
    pipe._update_item_progress(3000, 10000)
    pipe._update_item_progress(200, 500)

    assert calls == [(3000, 10000)]
    assert metas == [("translate_progress", {"current": 3000, "total": 10000})]


def test_item_progress_resets_when_new_translate_stage_starts():
    calls = []
    pipe = Pipeline(item_progress_callback=lambda current, total: calls.append((current, total)))

    pipe._update_progress("translate")
    pipe._update_item_progress(3000, 10000)
    pipe._update_progress("translate")
    pipe._update_item_progress(200, 500)

    assert calls == [(3000, 10000), (200, 500)]


def test_final_extraction_publishes_counts_for_cost_preflight():
    metas = []
    pipe = Pipeline(meta_callback=lambda key, value: metas.append((key, value)))
    record_final_extraction(pipe, type("Engine", (), {"name": "bgi"})(), [
        TextItem(file="a.ks", original="こんにちは"),
        TextItem(file="a.ks", original="さようなら"),
        TextItem(file="b.ks", original="短文"),
    ])

    assert metas[-1] == (
        "extraction_stats",
        {"text_count": 3, "source_chars": 12, "file_count": 2, "preflight_reused": False},
    )
