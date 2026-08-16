from pathlib import Path
from engines import registry
from utils.logger import info, warning


_DETECT_CACHE: dict[str, list[tuple[object, int, list[str]]]] = {}


def detect_engine(path: Path) -> object | None:
    """扫描路径并返回匹配的引擎实例。"""
    info(f"正在检测游戏引擎: {path}")

    # 延迟导入，让引擎注册自己
    _ensure_engines_loaded()

    candidates = detect_engine_candidates(path)
    engine = candidates[0][0] if candidates else None
    if engine:
        info(f"检测到引擎: {engine.label}")
    else:
        warning("未能识别游戏引擎，将使用通用模式")
        engine = registry.detect_generic()
    return engine


def detect_engine_candidates(path: Path) -> list[tuple[object, int, list[str]]]:
    """Return ranked engine candidates for diagnostics and selection."""
    info(f"正在检测游戏引擎: {path}")
    _ensure_engines_loaded()
    cache_key = _detect_cache_key(path)
    if cache_key in _DETECT_CACHE:
        return list(_DETECT_CACHE[cache_key])
    candidates = registry.detect_candidates(path)
    if not candidates:
        generic = registry.detect_generic()
        return [(generic, 1, ["通用兜底扫描"])] if generic else []
    _DETECT_CACHE[cache_key] = list(candidates)
    return candidates


def clear_detect_cache() -> None:
    _DETECT_CACHE.clear()


def _detect_cache_key(path: Path) -> str:
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path.absolute()
    return str(resolved).casefold()


_engines_loaded = False


def _ensure_engines_loaded():
    global _engines_loaded
    if _engines_loaded:
        return
    import engines.renpy          # noqa: F401
    import engines.rpgmaker       # noqa: F401
    import engines.godot_frida    # noqa: F401 — 必须在 godot 之前，加密 PCK 优先匹配
    import engines.godot_pck      # noqa: F401 — 必须在 godot 之前，PCK 游戏优先匹配
    import engines.godot          # noqa: F401
    import engines.gamemaker      # noqa: F401
    import engines.unity_arch000_lua  # noqa: F401 — 通用 Unity @ARCH000 Lua 归档族
    import engines.xunity         # noqa: F401 — 通用 Unity 运行时注入（BepInEx + XUnity.AutoTranslator）
    import engines.unity          # noqa: F401 — UnityPy 提取回填（回退方案）
    import engines.wolf           # noqa: F401
    import engines.tyrano         # noqa: F401 - must precede broad KiriKiri .ks detection
    import engines.kirikiri       # noqa: F401
    import engines.unreal         # noqa: F401
    import engines.bgi            # noqa: F401
    import engines.generic        # noqa: F401
    _engines_loaded = True


def detect_with_luna_hints(path: Path) -> str | None:
    """Use LunaTranslator engine signatures as fallback detection.

    Returns engine class name if matched, else None.
    This is a supplementary detection method when built-in engines don't match.
    """
    try:
        from core.luna_engine_hints import LUNA_ENGINE_HINTS
    except ImportError:
        return None

    if not path.is_dir():
        path = path.parent

    for engine_name, hint in LUNA_ENGINE_HINTS.items():
        check_by = hint["check_by"]
        patterns = hint["patterns"]

        if check_by == "FILE":
            # Single file must exist
            if (path / patterns[0]).exists():
                return engine_name

        elif check_by == "FILE_ANY":
            # At least one pattern must match
            for pattern in patterns:
                if list(path.glob(pattern)):
                    return engine_name

        elif check_by == "FILE_ALL":
            # All patterns must match
            if all(list(path.glob(p)) for p in patterns):
                return engine_name

        elif check_by == "RESOURCE_STR":
            # Skip: requires PE parsing, too complex for now
            continue

    return None
