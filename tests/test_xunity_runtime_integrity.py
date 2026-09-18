from __future__ import annotations

import zipfile
from pathlib import Path

from engines.base import TextItem
from engines.xunity.constants import XUNITY_DOWNLOAD_URLS
from engines.xunity.engine import XUnityRealtimeEngine
from engines.xunity.runtime import XUnityPluginDeployer
from engines.xunity.translation_cache import TranslationCacheGenerator


def _write_xunity_zip(path: Path, arch: str = "il2cpp", complete: bool = True) -> None:
    bridge = (
        "XUnity.AutoTranslator.Plugin.BepInEx-IL2CPP.dll"
        if arch == "il2cpp"
        else "XUnity.AutoTranslator.Plugin.BepInEx.dll"
    )
    entries = {
        "BepInEx/core/XUnity.Common.dll": b"common",
        "BepInEx/plugins/XUnity.AutoTranslator/XUnity.AutoTranslator.Plugin.Core.dll": b"core",
        f"BepInEx/plugins/XUnity.AutoTranslator/{bridge}": b"bridge",
        "BepInEx/plugins/XUnity.ResourceRedirector/XUnity.ResourceRedirector.dll": b"redirector",
    }
    if not complete:
        entries.pop("BepInEx/plugins/XUnity.AutoTranslator/XUnity.AutoTranslator.Plugin.Core.dll")
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)


def test_xunity_partial_leftover_is_not_deployed(tmp_path: Path) -> None:
    leftover = (
        tmp_path
        / "BepInEx/plugins/XUnity.AutoTranslator/XUnity.AutoTranslator.Plugin.ExtProtocol.dll"
    )
    leftover.parent.mkdir(parents=True)
    leftover.write_bytes(b"leftover")

    assert not XUnityPluginDeployer(tmp_path, arch="il2cpp").is_deployed()


def test_xunity_deploy_validates_before_replacing_existing_files(
    tmp_path: Path, monkeypatch
) -> None:
    sentinel = tmp_path / "BepInEx/plugins/XUnity.AutoTranslator/sentinel.dll"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_bytes(b"keep-me")
    archive = tmp_path / "incomplete.zip"
    _write_xunity_zip(archive, complete=False)

    deployer = XUnityPluginDeployer(tmp_path, arch="il2cpp")
    monkeypatch.setattr(deployer, "_get_xunity_zip", lambda: archive)

    assert not deployer.deploy()
    assert sentinel.read_bytes() == b"keep-me"


def test_xunity_complete_archive_deploys_required_bridge(tmp_path: Path, monkeypatch) -> None:
    archive = tmp_path / "complete.zip"
    _write_xunity_zip(archive)
    deployer = XUnityPluginDeployer(tmp_path, arch="il2cpp")
    monkeypatch.setattr(deployer, "_get_xunity_zip", lambda: archive)

    assert deployer.deploy()
    assert deployer.is_deployed()


def test_xunity_il2cpp_download_is_pinned_to_tested_version() -> None:
    assert "v5.6.1/" in XUNITY_DOWNLOAD_URLS["il2cpp"]


def test_xunity_cache_uses_configured_text_directory_and_escapes_multiline(
    tmp_path: Path,
) -> None:
    source = "一行目\n二行目=値//注釈"
    translated = "第一行\n第二行=值//注释"
    generator = TranslationCacheGenerator(tmp_path)

    cache_file = generator.generate(
        [TextItem(file="fixture", original=source, translated=translated)]
    )

    assert cache_file == (
        tmp_path / "BepInEx/Translation/zh/Text/Translation_zh.txt"
    )
    physical_lines = cache_file.read_text(encoding="utf-8-sig").splitlines()
    assert len(physical_lines) == 1
    assert generator._decode_line(physical_lines[0]) == (source, translated)


def test_xunity_cache_migrates_legacy_location(tmp_path: Path) -> None:
    legacy = tmp_path / "BepInEx/Translation/zh/Translation_zh.txt"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("はい=好的。", encoding="utf-8-sig")
    generator = TranslationCacheGenerator(tmp_path)

    generator.generate([])

    current = generator.cache_file.read_text(encoding="utf-8-sig")
    assert "はい=好的。" in current


def test_xunity_proxy_failure_is_recorded_and_rejects_launch(
    tmp_path: Path, monkeypatch
) -> None:
    import core.launcher as launcher
    import engines.xunity.engine as engine_module

    engine = XUnityRealtimeEngine()
    engine._game_dir = tmp_path
    monkeypatch.setattr(engine_module, "start_server", lambda **kwargs: None)
    monkeypatch.setattr(
        launcher,
        "launch_game",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("game launched")),
    )

    assert not engine.ensure_translation_proxy()
    assert not launcher._launch_with_xunity(tmp_path / "Game.exe", engine)
    events = (tmp_path / "_translation_meta/xunity_runtime.jsonl").read_text(
        encoding="utf-8"
    )
    assert "proxy_start_failed" in events


def test_xunity_proxy_healthcheck_accepts_live_proxy(tmp_path: Path, monkeypatch) -> None:
    import engines.xunity.engine as engine_module

    class Proxy:
        def stop(self) -> None:
            raise AssertionError("healthy proxy should not be stopped")

    engine = XUnityRealtimeEngine()
    engine._game_dir = tmp_path
    proxy = Proxy()
    monkeypatch.setattr(engine_module, "start_server", lambda **kwargs: proxy)
    monkeypatch.setattr(engine, "_proxy_is_listening", lambda port: True)

    assert engine.ensure_translation_proxy(provider="deepseek")
    assert engine._translation_proxy is proxy
    events = (tmp_path / "_translation_meta/xunity_runtime.jsonl").read_text(
        encoding="utf-8"
    )
    assert "proxy_ready" in events
