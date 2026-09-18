import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.pipeline_stages import stage, stage_keys, stage_plan


def test_pipeline_stage_order_and_progress_are_stable():
    keys = stage_keys()
    assert keys == [
        "archive",
        "detect",
        "extract",
        "language",
        "checkpoint_load",
        "checkpoint_export",
        "translate",
        "validate",
        "backup",
        "repack",
        "fonts",
        "copy_back",
        "launch",
        "complete",
    ]
    progress = [stage(key).progress for key in keys]
    assert progress == sorted(progress)


def test_stage_plan_is_json_ready():
    plan = stage_plan(["detect", "extract", "translate"])
    assert plan == [
        {"key": "detect", "label": "引擎检测", "progress": 10},
        {"key": "extract", "label": "解包文本", "progress": 20},
        {"key": "translate", "label": "AI 翻译", "progress": 40},
    ]


def test_unknown_stage_is_rejected():
    try:
        stage("not-a-stage")
    except ValueError as exc:
        assert "unknown pipeline stage" in str(exc)
    else:
        raise AssertionError("unknown stage should fail")
