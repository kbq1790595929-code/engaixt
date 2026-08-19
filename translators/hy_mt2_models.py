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
}

DEFAULT_MODEL_NAME = "Hy-MT2-1.8B-Q4_K_M"


def resolve_model_name(value: str | None) -> str:
    candidate = str(value or "").strip()
    return candidate if candidate in MODEL_SPECS else DEFAULT_MODEL_NAME


def model_spec(value: str | None = None) -> HyMt2ModelSpec:
    return MODEL_SPECS[resolve_model_name(value)]


def model_choices() -> tuple[HyMt2ModelSpec, ...]:
    return tuple(MODEL_SPECS.values())
