from scripts.repair_runtime_control_tokens import repair_runtime_map


def test_repairs_cjk_speaker_tag_and_injected_empty_braces():
    source = "\\n<\u30eb\u30eb\u30a5>\u300c\u304d\u306e\3053\u3068\u91ce\u83dc\u304c\u3042\u308b\uff01\u300d"
    mapping = {source: "{}<\u9732\u9732>\u300c\u6709\u8611\u83c7\u548c\u91ce\u83dc{}\uff01\u300d"}

    repaired, stats = repair_runtime_map(mapping)

    assert repaired[source] == "\\n<\u30eb\u30eb\u30a5>\u300c\u6709\u8611\u83c7\u548c\u91ce\u83dc\uff01\u300d"
    assert stats == {
        "checked": 1,
        "speaker_restored": 1,
        "extra_braces_removed": 1,
        "changed": 1,
    }


def test_preserves_authored_empty_braces():
    source = "{}<\u30eb\u30eb\u30a5>\u300c\u304a\u306f\u3088\u3046\u300d"
    mapping = {source: "{}<\u30eb\u30eb\u30a5>\u300c\u65e9\u4e0a\u597d\u300d"}

    repaired, stats = repair_runtime_map(mapping)

    assert repaired == mapping
    assert stats["changed"] == 0
