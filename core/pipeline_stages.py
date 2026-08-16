from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class PipelineStage:
    key: str
    label: str
    progress: float


STAGES: tuple[PipelineStage, ...] = (
    PipelineStage("archive", "解压检查", 5),
    PipelineStage("detect", "引擎检测", 10),
    PipelineStage("extract", "解包文本", 20),
    PipelineStage("language", "语言检测", 30),
    PipelineStage("checkpoint_load", "检查点加载", 35),
    PipelineStage("checkpoint_export", "导出检查点", 37),
    PipelineStage("translate", "AI 翻译", 40),
    PipelineStage("validate", "安全校验", 55),
    PipelineStage("backup", "安全备份", 65),
    PipelineStage("repack", "回填文本", 70),
    PipelineStage("fonts", "字体替换", 75),
    PipelineStage("copy_back", "复制回游戏", 85),
    PipelineStage("launch", "启动游戏", 100),
    PipelineStage("complete", "完成", 100),
)

_BY_KEY = {stage.key: stage for stage in STAGES}


def stage(key: str) -> PipelineStage:
    try:
        return _BY_KEY[key]
    except KeyError as exc:
        raise ValueError(f"unknown pipeline stage: {key}") from exc


def stage_keys() -> list[str]:
    return [s.key for s in STAGES]


def stage_plan(keys: Iterable[str] | None = None) -> list[dict]:
    selected = STAGES if keys is None else [stage(key) for key in keys]
    return [
        {"key": s.key, "label": s.label, "progress": s.progress}
        for s in selected
    ]
