from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.pipeline import Pipeline
from core.pipeline_stage_runtime import run_extract_stage
from engines.base import TextItem


class _StubKirikiriEngine:
    """最小 KiriKiri 引擎替身：只暴露受保护 XP3 判定所需的属性。"""

    name = "kirikiri"
    label = "KiriKiri"
    support_level = "beta"

    def __init__(self, items: list[TextItem]) -> None:
        self._items = list(items)
        self._protected_archives = ["data.xp3"]
        self._protected_formats: set[str] = set()
        self._protected_script_count = 169
        self._static_external_decrypt_succeeded = False
        self._runtime_dump_count = 0

    def unpack(self, path, workspace):  # noqa: ARG002 - 替身不做真实提取
        return list(self._items)


def _runtime_items(count: int) -> list[TextItem]:
    return [
        TextItem(
            file="__kirikiri_runtime_capture__.jsonl",
            key=f"runtime:{i}",
            original=f"常夜灯の暗さにだんだんと目が慣れていく{i}",
            context="message",
            line=i + 1,
            meta={
                "engine": "kirikiri",
                "runtime_capture": True,
                "source": "internal_wide",
            },
        )
        for i in range(count)
    ]


def _static_items(count: int) -> list[TextItem]:
    return [
        TextItem(
            file="scenario/noise.ks",
            key=f"static:{i}",
            original=f"噪声候选{i}",
            context="message",
            line=i + 1,
            meta={"engine": "kirikiri"},
        )
        for i in range(count)
    ]


def _make_pipeline(tmp_path: Path) -> Pipeline:
    game_dir = tmp_path / "game"
    game_dir.mkdir()
    pipe = Pipeline()
    pipe._begin_run_context(str(game_dir), game_dir, {"checkpoint": False})
    return pipe


def test_protected_xp3_keeps_runtime_captures(tmp_path):
    """回归：受保护 XP3 的噪声判定不得连带清空运行时捕获的真实文本。

    实测案例：某游戏 ``data.xp3`` 的内容由 XP3 解密插件保护，静态提取为 0 条，
    但运行时 hook 已经捕获到 24 条真实文本。旧逻辑用
    ``len(items) < max(200, protected_count * 2)`` 判断噪声，把 runtime 捕获
    也算进 ``items``，于是 24 条真实文本被整体清空，管线随后报
    「KRKR 的 XP3 封包可以读取，但没有提取出可解析的脚本」。
    """
    pipe = _make_pipeline(tmp_path)
    captured = _runtime_items(24)
    engine = _StubKirikiriEngine(captured)

    _, items, extracted_count = run_extract_stage(pipe, tmp_path / "game", engine)

    assert extracted_count == 24
    assert items == captured

    noise = pipe.diagnostics.data["kirikiri_protected_static_noise"]
    assert noise["runtime_items_kept"] == 24
    assert noise["static_items"] == 0


def test_protected_xp3_keeps_runtime_captures_among_static_noise(tmp_path):
    """静态噪声与运行时捕获混在一起时，只丢弃静态候选。"""
    pipe = _make_pipeline(tmp_path)
    captured = _runtime_items(24)
    engine = _StubKirikiriEngine(_static_items(9) + captured)

    _, items, extracted_count = run_extract_stage(pipe, tmp_path / "game", engine)

    assert extracted_count == 24
    assert items == captured

    noise = pipe.diagnostics.data["kirikiri_protected_static_noise"]
    assert noise["static_items"] == 9
    assert noise["runtime_items_kept"] == 24


def test_protected_xp3_still_drops_pure_static_noise(tmp_path):
    """回归保护：没有运行时捕获时，受保护脚本的噪声候选仍必须被丢弃。"""
    pipe = _make_pipeline(tmp_path)
    engine = _StubKirikiriEngine(_static_items(12))

    _, items, extracted_count = run_extract_stage(pipe, tmp_path / "game", engine)

    assert items == []
    assert extracted_count == 0
    assert pipe.diagnostics.data["static_decrypt_required"] is True
    noise = pipe.diagnostics.data["kirikiri_protected_static_noise"]
    assert noise["runtime_items_kept"] == 0
    assert noise["protected_script_count"] == 169
