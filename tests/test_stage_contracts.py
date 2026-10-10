"""阶段合同测试：冻结 detect/extract/translate/patch/runtime/diagnostics 的跨阶段禁令。

这里锁的不是某个功能是否正确，而是"哪个阶段绝不允许做哪件事"：

- detect 只能轻量识别：不解包、不在游戏目录留痕、对陌生目录不抛异常
- engines（提取/回填/部署层）不得调用 AI 翻译器，不得反向依赖管线编排
- translators 只做翻译/缓存/费用：只许认识 engines.base 的数据结构，
  不得回填、不得部署、不得启动
- diagnostics 只记录事实：不 import 任何业务模块，不能反过来影响业务

合同全貌见 docs/stage-contracts.md。改动如果撞上这些测试，
先回答"这个职责真的属于这个阶段吗"，而不是改测试放行。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.detector import _ensure_engines_loaded
from engines import registry


def _py_sources(rel: str) -> dict[str, str]:
    base = ROOT / rel
    return {
        p.relative_to(ROOT).as_posix(): p.read_text(encoding="utf-8", errors="replace")
        for p in sorted(base.rglob("*.py"))
        if "__pycache__" not in p.parts
    }


def _imports_of(source: str, module_pattern: str) -> list[str]:
    """返回 source 中 import 指定模块的行（含函数内延迟 import）。"""
    pattern = re.compile(
        rf"^\s*(?:from\s+{module_pattern}\b|import\s+{module_pattern}\b).*$",
        re.MULTILINE,
    )
    return pattern.findall(source)


# ---------------------------------------------------------------------------
# extract/repack 层（engines）合同
# ---------------------------------------------------------------------------


def test_engines_never_call_ai_translators():
    """提取/回填层禁止调用 AI：translators 包对 engines 不可见。"""
    offenders = {
        path: lines
        for path, src in _py_sources("engines").items()
        if (lines := _imports_of(src, r"translators"))
    }
    assert not offenders, f"engines 不得 import translators（AI 属 translate 阶段）: {offenders}"


def test_engines_never_import_pipeline_orchestration():
    """引擎不得反向依赖管线编排，防止阶段边界从下往上塌。"""
    offenders = {
        path: lines
        for path, src in _py_sources("engines").items()
        if (lines := _imports_of(src, r"core\.pipeline"))
    }
    assert not offenders, f"engines 不得 import core.pipeline*: {offenders}"


def test_engines_translation_proxy_allowlist():
    """本地翻译代理属于运行时部署职责，只允许 xunity 实时引擎接线。

    新引擎要用实时代理，先在 docs/stage-contracts.md 说明理由再进白名单。
    """
    allowed = {
        "engines/xunity/engine.py",
        "engines/xunity/runtime.py",
        "engines/xunity/translation_cache.py",
    }
    offenders = {
        path: lines
        for path, src in _py_sources("engines").items()
        if path not in allowed
        and (lines := _imports_of(src, r"utils\.translation_server"))
    }
    assert not offenders, f"translation_server 引用超出白名单: {offenders}"


def test_engines_never_use_diagnostics_module():
    """引擎自身的诊断落盘走引擎内部方法；core.diagnostics 归编排层持有。"""
    offenders = {
        path: lines
        for path, src in _py_sources("engines").items()
        if (lines := _imports_of(src, r"core\.diagnostics"))
    }
    assert not offenders, f"engines 不得直接使用 core.diagnostics: {offenders}"


# ---------------------------------------------------------------------------
# translate 层（translators）合同
# ---------------------------------------------------------------------------


def test_translators_only_know_engine_base_types():
    """翻译层只认识 TextItem 等数据结构，不认识任何具体引擎。"""
    offenders: dict[str, list[str]] = {}
    for path, src in _py_sources("translators").items():
        bad = [
            line
            for line in _imports_of(src, r"engines")
            if "engines.base" not in line
        ]
        if bad:
            offenders[path] = bad
    assert not offenders, f"translators 只许 import engines.base: {offenders}"


def test_translators_never_patch_deploy_or_launch():
    """翻译层不得回填、部署、启动：这些模块对 translators 永久禁区。
    """
    forbidden = (
        r"core\.pipeline",
        r"core\.launcher",
        r"core\.rpgmaker_runtime",
        r"core\.manifest",
        r"core\.frida_injector",
    )
    offenders: dict[str, list[str]] = {}
    for path, src in _py_sources("translators").items():
        bad: list[str] = []
        for module in forbidden:
            bad.extend(_imports_of(src, module))
        if bad:
            offenders[path] = bad
    assert not offenders, f"translators 触碰回填/部署/启动禁区: {offenders}"


# ---------------------------------------------------------------------------
# diagnostics 合同
# ---------------------------------------------------------------------------


def test_diagnostics_is_fact_recorder_only():
    """diagnostics 只记录事实：import 面锁死为 stdlib + utils.logger。

    一旦 diagnostics 开始 import 业务模块，就有了"反过来影响业务"的通道。
    """
    src = (ROOT / "core" / "diagnostics.py").read_text(encoding="utf-8")
    import_lines = re.findall(r"^(?:from|import)\s+\S+.*$", src, re.MULTILINE)
    allowed = re.compile(
        r"^(?:from|import)\s+"
        r"(?:__future__|json|platform|sys|time|traceback|os|re|dataclasses|datetime|"
        r"pathlib|typing|utils\.logger)\b"
    )
    offenders = [line for line in import_lines if not allowed.match(line)]
    assert not offenders, f"diagnostics 只许依赖 stdlib 与 logger: {offenders}"


def test_business_modules_never_read_diagnostics_back():
    """diagnostics 是单向黑匣子：引擎与翻译层不得读取诊断数据做决策。"""
    for rel in ("engines", "translators"):
        offenders = {
            path: lines
            for path, src in _py_sources(rel).items()
            if (lines := _imports_of(src, r"core\.diagnostics"))
        }
        assert not offenders, f"{rel} 不得读取 diagnostics: {offenders}"


# ---------------------------------------------------------------------------
# KRKR 路线合同：默认静态优先，失败后由用户确认实时 hook
# ---------------------------------------------------------------------------


def test_kirikiri_defaults_are_static_first():
    """KRKR starts with static preflight and keeps realtime as explicit fallback."""
    from config import Config

    defaults = Config()
    assert defaults.kirikiri_enable_static_patch is True, "KRKR 默认先走静态方案"
    assert defaults.kirikiri_use_external_krkrdump is False, "第三方 KrkrDumpLoader 默认关闭"
    assert defaults.kirikiri_auto_launch_dump is False, "自动拉起游戏 dump 默认关闭"


def test_gui_allows_kirikiri_static_route():
    """KRKR must enter the static route before offering realtime fallback."""
    import app as app_mod

    assert "kirikiri" not in app_mod._REALTIME_ONLY_ENGINE_NAMES


# ---------------------------------------------------------------------------
# detect 阶段行为合同（对所有已注册引擎生效）
# ---------------------------------------------------------------------------


@pytest.fixture()
def sample_game_dir(tmp_path: Path) -> Path:
    game = tmp_path / "game"
    game.mkdir()
    (game / "game.exe").write_bytes(b"MZ" + b"\0" * 4096)
    (game / "readme.txt").write_text("sample", encoding="utf-8")
    (game / "data").mkdir()
    (game / "data" / "misc.bin").write_bytes(b"\x00\x01\x02\x03" * 16)
    return game


def test_detect_never_unpacks(sample_game_dir: Path, monkeypatch):
    """detect/detect_confidence 不得触发 unpack——检测阶段禁止解包。"""
    _ensure_engines_loaded()

    def _boom(*_args, **_kwargs):
        raise AssertionError("detect 阶段不得调用 unpack")

    for engine in registry.list_engines():
        monkeypatch.setattr(engine, "unpack", _boom)
        engine.detect(sample_game_dir)
        engine.detect_confidence(sample_game_dir)


def test_detect_leaves_game_dir_untouched(sample_game_dir: Path):
    """检测不留痕：跑完所有引擎的检测后游戏目录必须一字未动。"""
    _ensure_engines_loaded()

    def _snapshot() -> dict[str, bytes]:
        return {
            p.relative_to(sample_game_dir).as_posix(): p.read_bytes()
            for p in sorted(sample_game_dir.rglob("*"))
            if p.is_file()
        }

    before = _snapshot()
    for engine in registry.list_engines():
        engine.detect(sample_game_dir)
        engine.detect_confidence(sample_game_dir)
    assert _snapshot() == before, "有引擎在 detect 阶段修改了游戏目录"
