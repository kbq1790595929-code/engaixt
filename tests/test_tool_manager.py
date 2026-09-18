from pathlib import Path
import json

import pytest

import config as config_mod
from config import Config
from core import tool_manager


def test_krkrextract_spec_tracks_split_release_assets():
    spec = tool_manager.get_tool_spec("krkrextract")

    assert spec is not None
    assert spec.install_all_release_assets is True
    assert "KrkrExtract.Lite.exe" in spec.executable_names
    assert "KrkrExtract.Core.dll" in spec.companion_names
    assert "KrkrExtract.UI.Lite.dll" in spec.companion_names


def test_tool_install_complete_requires_companion_files(tmp_path: Path):
    spec = tool_manager.get_tool_spec("krkrextract")
    assert spec is not None

    (tmp_path / "KrkrExtract.Lite.exe").write_bytes(b"MZ")
    assert tool_manager._tool_install_complete(tmp_path, spec) is False

    (tmp_path / "KrkrExtract.Core.dll").write_bytes(b"dll")
    assert tool_manager._tool_install_complete(tmp_path, spec) is False

    (tmp_path / "KrkrExtract.UI.Lite.dll").write_bytes(b"dll")
    assert tool_manager._tool_install_complete(tmp_path, spec) is True


def test_find_in_root_does_not_match_version_json_for_version_dll(tmp_path: Path):
    (tmp_path / "version.json").write_text("{}", encoding="utf-8")
    assert tool_manager._find_in_root(tmp_path, ("version.dll",)) is None

    dll = tmp_path / "version.dll"
    dll.write_bytes(b"dll")
    assert tool_manager._find_in_root(tmp_path, ("version.dll",)) == dll


def test_update_skips_missing_optional_tool(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(tool_manager, "get_tools_dir", lambda: tmp_path / "tools")
    monkeypatch.setattr(tool_manager, "find_tool", lambda name: None)
    called = []
    monkeypatch.setattr(
        tool_manager,
        "_resolve_tool_downloads",
        lambda spec: called.append(spec.name) or ([("tool.zip", "https://example.test/tool.zip")], {"tag_name": "v1"}),
    )

    spec = tool_manager.get_tool_spec("garbro_console")
    assert spec is not None
    result = tool_manager._update_tool_package(spec, force=True)

    assert result["status"] == "skipped"
    assert result["reason"] == "not_installed"
    assert called == []


def test_update_skips_external_or_bundled_tool(tmp_path: Path, monkeypatch):
    external = tmp_path / "external" / "GARbro.Console.exe"
    external.parent.mkdir()
    external.write_bytes(b"MZ")
    monkeypatch.setattr(tool_manager, "get_tools_dir", lambda: tmp_path / "managed")
    monkeypatch.setattr(tool_manager, "find_tool", lambda name: external)

    spec = tool_manager.get_tool_spec("garbro_console")
    assert spec is not None
    result = tool_manager._update_tool_package(spec, force=True)

    assert result["status"] == "skipped"
    assert result["reason"] == "external_or_bundled_tool"


def test_tool_update_default_interval_is_five_days():
    assert Config().tool_update_interval_hours == 120


def test_old_one_day_tool_update_interval_migrates_to_five_days(tmp_path: Path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"tool_update_interval_hours": 24}), encoding="utf-8")
    monkeypatch.setattr(config_mod, "CONFIG_PATH", cfg_path)

    assert Config.load().tool_update_interval_hours == 120


def test_maybe_auto_update_records_manifest_check(tmp_path: Path, monkeypatch):
    tools_dir = tmp_path / "tools"
    monkeypatch.setattr(tool_manager, "get_tools_dir", lambda: tools_dir)
    monkeypatch.setattr(
        config_mod,
        "get_config",
        lambda: Config(auto_download_tools=True, auto_update_tools=True, tool_update_interval_hours=24),
    )
    monkeypatch.setattr(
        tool_manager,
        "update_recommended_tools",
        lambda names, force=False, progress_callback=None: {
            "garbro_console": {"status": "skipped", "reason": "not_installed"}
        },
    )

    result = tool_manager.maybe_auto_update_tools(["garbro_console"])

    manifest = tool_manager._load_manifest()
    assert result["garbro_console"]["status"] == "skipped"
    assert manifest["_tool_update"]["summary"]["skipped"] == 1
    assert manifest["_tool_update"]["tool_names"] == ["garbro_console"]


def test_update_recommended_tools_emits_progress_events(monkeypatch):
    events = []
    spec = tool_manager.get_tool_spec("garbro_console")
    assert spec is not None

    monkeypatch.setattr(tool_manager, "_update_tool_package", lambda *args, **kwargs: {"status": "current"})

    result = tool_manager.update_recommended_tools(
        ["garbro_console"],
        progress_callback=events.append,
    )

    assert result["garbro_console"]["status"] == "current"
    assert [event["phase"] for event in events] == [
        "update_begin",
        "tool_check",
        "tool_done",
        "update_done",
    ]
    assert events[1]["tool"] == "garbro_console"
