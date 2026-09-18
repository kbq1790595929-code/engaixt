from pathlib import Path

from engines.xunity.config_gen import (
    DEFAULT_TMP_SYSTEM_FONT,
    generate_xunity_config,
)
import engines.xunity.fonts as xunity_fonts
from engines.xunity.fonts import FontDeployer
from engines.xunity.runtime import TranslationCacheGenerator as RuntimeCacheGenerator
from engines.xunity.translation_cache import TranslationCacheGenerator
from engines.base import TextItem


def test_xunity_uses_system_tmp_fallback_when_no_bundle_is_deployed(tmp_path: Path):
    config_path = generate_xunity_config(tmp_path)
    content = config_path.read_text(encoding="utf-8")

    assert f"FallbackFontTextMeshPro={DEFAULT_TMP_SYSTEM_FONT}" in content
    assert "OverrideFontTextMeshPro=\n" in content
    assert "FallbackFontTextMeshPro=Unity 6000" not in content


def test_xunity_uses_actual_tmp_bundle_reference_when_one_is_deployed(tmp_path: Path):
    config_path = generate_xunity_config(
        tmp_path,
        tmp_font_reference="EngAixt_SourceHanSansCN_TMP.bundle",
    )
    content = config_path.read_text(encoding="utf-8")

    assert "FallbackFontTextMeshPro=EngAixt_SourceHanSansCN_TMP.bundle" in content
    assert "TMP fallback: asset bundle" in content


def test_xunity_unity6000_plugin_leaves_xunity_tmp_fallback_blank(tmp_path: Path):
    config_path = generate_xunity_config(tmp_path, use_engaixt_tmp_fallback=True)
    content = config_path.read_text(encoding="utf-8")

    assert "TMP fallback: EngAixt asynchronous fallback plugin" in content
    assert "OverrideFontTextMeshPro=\n" in content
    assert "FallbackFontTextMeshPro=\n" in content


def test_xunity_unity6000_fallback_assets_deploy_to_game_root(tmp_path: Path):
    assert FontDeployer(tmp_path).deploy_tmp_font_bundle() is True
    assert (tmp_path / "NotoSansSC_sdf32_optimized_12k_lz4_2020").is_file()
    assert (tmp_path / "BepInEx" / "plugins" / "EngAixt.XUnityTmpFontFallback.dll").is_file()


def test_xunity_runtime_keeps_public_exports_after_module_split(tmp_path: Path, monkeypatch):
    assert RuntimeCacheGenerator is TranslationCacheGenerator
    monkeypatch.setattr(xunity_fonts, "TOOLS_CACHE", tmp_path / "missing_tools")
    monkeypatch.setattr(xunity_fonts, "resource_path", lambda *parts: tmp_path.joinpath(*parts))
    assert FontDeployer(tmp_path).deploy_tmp_font_bundle() is False

    cache_file = TranslationCacheGenerator(tmp_path).generate([
        TextItem(file="asset", original="Hello", translated="你好"),
    ])

    assert cache_file.read_text(encoding="utf-8-sig") == "Hello=你好"
