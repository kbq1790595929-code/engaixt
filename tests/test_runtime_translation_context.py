from core.runtime_translation_context import RollingTranslationContext


def test_rolling_context_keeps_latest_three_distinct_translations() -> None:
    context = RollingTranslationContext(limit=3, max_chars=600)

    for index in range(4):
        assert context.add(f"source-{index}", f"translated-{index}", f"speaker-{index}")

    snapshot = context.snapshot()
    assert [entry["source"] for entry in snapshot] == ["source-1", "source-2", "source-3"]
    assert snapshot[-1]["speaker"] == "speaker-3"


def test_rolling_context_updates_last_source_without_duplicate() -> None:
    context = RollingTranslationContext()

    assert context.add("same", "first")
    assert context.add("same", "second")
    assert not context.add("same", "second")

    assert context.snapshot() == [{
        "source": "same",
        "translated": "second",
        "speaker": "",
    }]


def test_rolling_context_rejects_empty_and_same_as_source() -> None:
    context = RollingTranslationContext()

    assert not context.add("", "translated")
    assert not context.add("source", "")
    assert not context.add("same", "same")
    assert context.snapshot() == []
