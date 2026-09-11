from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HyMt2ModelSpec:
    name: str
    label: str
    filename: str
    size: int
    sha256: str
    source_url: str
    license_url: str
    description: str


_MODELSCOPE_ROOT = "https://www.modelscope.cn/models/Tencent-Hunyuan"

# 自训模型与底模不在同一个 ModelScope 账号下，单独给出根地址，
# 避免把它拼到上面那个已含账号名的常量后面。
_ENGAIXT_MODEL_ROOT = "https://www.modelscope.cn/models/ENGAOXT/EngAixt-7B-GGUF/resolve/master"

MODEL_SPECS: dict[str, HyMt2ModelSpec] = {
    "Hy-MT2-1.8B-Q4_K_M": HyMt2ModelSpec(
        name="Hy-MT2-1.8B-Q4_K_M",
        label="腾讯混元 Hy-MT2 1.8B（轻量）",
        filename="Hy-MT2-1.8B-Q4_K_M.gguf",
        size=1_133_080_448,
        sha256="dc5f44fcf1fa496ee7ad725982c0c8c553a4de00259b53af84c4b89fb0c06699",
        source_url=(
            f"{_MODELSCOPE_ROOT}/Hy-MT2-1.8B-GGUF/resolve/master/"
            "Hy-MT2-1.8B-Q4_K_M.gguf"
        ),
        license_url=f"{_MODELSCOPE_ROOT}/Hy-MT2-1.8B-GGUF/resolve/master/LICENSE.txt",
        description="约 1.1 GB，显存和内存占用低，适合实时翻译与轻量设备。",
    ),
    "HY-MT2-7B-Q4_K_M": HyMt2ModelSpec(
        name="HY-MT2-7B-Q4_K_M",
        label="腾讯混元 Hy-MT2 7B（Q4 速度版）",
        filename="Hy-MT2-7B-Q4_K_M.gguf",
        size=4_624_648_896,
        sha256="9f96256500f3fc1ab4d64336b58f52a949a95ad7516b0c229476eef782f9f77b",
        source_url=(
            f"{_MODELSCOPE_ROOT}/Hy-MT2-7B-GGUF/resolve/master/"
            "Hy-MT2-7B-Q4_K_M.gguf"
        ),
        license_url=f"{_MODELSCOPE_ROOT}/Hy-MT2-7B-GGUF/resolve/master/LICENSE.txt",
        description="约 4.3 GB，7B 质量与速度的平衡版本，推荐日常翻译使用。",
    ),
    "HY-MT2-7B-Q8_0": HyMt2ModelSpec(
        name="HY-MT2-7B-Q8_0",
        label="腾讯混元 Hy-MT2 7B（高质量）",
        filename="HY-MT2-7B-Q8_0.gguf",
        size=7_981_928_896,
        sha256="58b3ad55dd6f6fa08c695cddc34fb5f8f708a844f78ae10508071914b0ed67c0",
        source_url=(
            f"{_MODELSCOPE_ROOT}/Hy-MT2-7B-GGUF/resolve/master/"
            "HY-MT2-7B-Q8_0.gguf"
        ),
        license_url=f"{_MODELSCOPE_ROOT}/Hy-MT2-7B-GGUF/resolve/master/LICENSE.txt",
        description="约 7.4 GB，译文质量更高；建议使用至少 12 GB 显存的 NVIDIA GPU。",
    ),
    "EngAixt-7B-Q4_K_M": HyMt2ModelSpec(
        name="EngAixt-7B-Q4_K_M",
        label="EngAixt 7B（自训 · 控制符强化版）",
        filename="EngAixt-7B-Q4_K_M.gguf",
        size=4_624_648_800,
        sha256="4f8aeb8e0a41e6c0e8438686863fd9f7af390d4c8c2a1c8cb1e38b45a659c551",
        source_url=_ENGAIXT_MODEL_ROOT + "/EngAixt-7B-Q4_K_M.gguf",
        license_url=_ENGAIXT_MODEL_ROOT + "/EngAixt-7B-LICENSE.txt",
        description=(
            "约 4.3 GB，基于腾讯混元 Hy-MT2-7B（Apache 2.0）自训的日译中文本模型，"
            "控制符保护与 EngAixt 生产链路同格式。"
            "真实游戏文本全量 4,239 行实测（batch 20）：整批 JSON 契约 212/212 正确，"
            "控制符保留 99.71%（语义类 99.17%），空译文 0，日文残留 38 条。"
            "仅训练日译中；建议 8 GB 以上显存。"
        ),
    ),
}

DEFAULT_MODEL_NAME = "Hy-MT2-1.8B-Q4_K_M"


def resolve_model_name(value: str | None) -> str:
    candidate = str(value or "").strip()
    return candidate if candidate in MODEL_SPECS else DEFAULT_MODEL_NAME


def model_spec(value: str | None = None) -> HyMt2ModelSpec:
    return MODEL_SPECS[resolve_model_name(value)]


def model_choices() -> tuple[HyMt2ModelSpec, ...]:
    return tuple(MODEL_SPECS.values())
