import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.engine_capabilities import can_extract, can_repack, engine_support_summary
from core.detector import _ensure_engines_loaded
from engines import registry


def _engine(name: str):
    _ensure_engines_loaded()
    for engine in registry.list_engines():
        if engine.name == name:
            return engine
    raise AssertionError(f"engine not registered: {name}")


def test_all_registered_engines_publish_capabilities():
    _ensure_engines_loaded()
    engines = [engine for engine in registry.list_engines() if engine.name != "base"]
    assert engines

    for engine in engines:
        summary = engine.support_summary()
        caps = summary["capabilities"]
        assert summary["name"] == engine.name
        assert summary["label"]
        assert summary["support_level"] in {"stable", "beta", "experimental", "partial", "planned"}
        for key in [
            "extract",
            "repack",
            "static_patch",
            "runtime_patch",
            "creates_launcher",
            "portable_after_patch",
            "requires_python",
            "requires_frida",
            "needs_external_tool",
        ]:
            assert isinstance(caps[key], bool), (engine.name, key)
        assert isinstance(caps["notes"], list)


def test_bgi_declares_native_portable_launcher():
    caps = _engine("bgi").get_capabilities()
    assert caps.extract
    assert caps.repack
    assert caps.static_patch
    assert caps.runtime_patch
    assert caps.creates_launcher
    assert caps.portable_after_patch
    assert not caps.requires_python
    assert not caps.requires_frida


def test_xunity_declares_runtime_patch_not_static_patch():
    caps = _engine("xunity_realtime").get_capabilities()
    assert caps.extract
    assert caps.repack
    assert not caps.static_patch
    assert caps.runtime_patch
    assert caps.portable_after_patch
    assert not caps.requires_frida


def test_godot_frida_declares_runtime_dependency():
    caps = _engine("godot_frida").get_capabilities()
    assert caps.extract
    assert caps.repack
    assert caps.runtime_patch
    assert caps.requires_python
    assert caps.requires_frida
    assert not caps.portable_after_patch


def test_wolf_declares_beta_uberwolf_static_pipeline():
    engine = _engine("wolf")
    caps = _engine("wolf").get_capabilities()
    assert engine.support_level == "beta"
    assert caps.extract
    assert caps.repack
    assert caps.static_patch
    assert not caps.runtime_patch
    assert caps.portable_after_patch
    assert caps.needs_external_tool


def test_kirikiri_declares_beta_static_patch():
    engine = _engine("kirikiri")
    caps = engine.get_capabilities()
    assert engine.support_level == "beta"
    assert caps.extract
    assert caps.repack
    assert caps.static_patch
    assert caps.runtime_patch
    assert caps.portable_after_patch
    assert caps.creates_launcher
    assert caps.needs_external_tool
    assert not caps.requires_python
    assert not caps.requires_frida


def test_pipeline_helpers_read_engine_capabilities():
    bgi = _engine("bgi")
    wolf = _engine("wolf")

    assert can_extract(bgi)
    assert can_repack(bgi)
    assert can_extract(wolf)
    assert can_repack(wolf)

    summary = engine_support_summary(bgi)
    assert summary["name"] == "bgi"
    assert summary["capabilities"]["creates_launcher"]
    assert summary["capabilities"]["portable_after_patch"]
