from __future__ import annotations

from typing import Any

from engines.base import EngineCapabilities


def capabilities_for(engine: object | None) -> EngineCapabilities:
    if engine is None:
        return EngineCapabilities(extract=False, repack=False, static_patch=False)
    getter = getattr(engine, "get_capabilities", None)
    if callable(getter):
        return getter()
    return EngineCapabilities(
        extract=bool(getattr(engine, "supports_extract", True)),
        repack=bool(getattr(engine, "supports_repack", True)),
        static_patch=bool(getattr(engine, "supports_repack", True)),
    )


def can_extract(engine: object | None) -> bool:
    return capabilities_for(engine).extract


def can_repack(engine: object | None) -> bool:
    return capabilities_for(engine).repack


def engine_support_summary(engine: object | None) -> dict[str, Any]:
    if engine is None:
        return {}
    summary = getattr(engine, "support_summary", None)
    if callable(summary):
        return summary()
    caps = capabilities_for(engine)
    return {
        "name": getattr(engine, "name", ""),
        "label": getattr(engine, "label", ""),
        "support_level": getattr(engine, "support_level", "unknown"),
        "supports_extract": caps.extract,
        "supports_repack": caps.repack,
        "capabilities": caps.to_dict(),
        "limitations": list(getattr(engine, "limitations", [])),
    }
