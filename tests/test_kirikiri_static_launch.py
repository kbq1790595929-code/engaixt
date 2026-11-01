from types import SimpleNamespace

import base64
import json
import re

import pytest

from core import pipeline_runtime_stage as stage
from core.launcher import _write_native_kirikiri_launcher, create_kirikiri_static_launcher


def _ps_payload(launcher) -> str:
    content = launcher.read_text(encoding="utf-8-sig")
    match = re.search(r"-EncodedCommand\s+(\S+)", content)
    assert match is not None
    return base64.b64decode(match.group(1)).decode("utf-16le")


def test_protected_archive_static_launcher_uses_native_embed_hook(tmp_path, monkeypatch):
    """自定义签名/加密索引的封包不认 patch.xp3，静态启动器必须走 native 内嵌。"""
    exe = tmp_path / "Game.exe"
    exe.write_bytes(b"MZ")
    meta = tmp_path / "_translation_meta"
    meta.mkdir()
    (meta / "extract_diagnostics.json").write_text(json.dumps({
        "protected_archives": ["data.xp3"],
        "index_anomaly": "unknown_chunk",
        "static_external_extractor": "msg_tool",
    }), encoding="utf-8")
    (meta / "translation_checkpoint.json").write_text(json.dumps({"items": []}), encoding="utf-8")
    native = meta / "kirikiri_native_launcher.exe"
    native.write_bytes(b"MZ")
    monkeypatch.setattr("core.launcher._prepare_native_kirikiri_runtime", lambda *a, **k: native)
    monkeypatch.setattr("core.launcher._prepare_kirikiri_standalone_launcher", lambda *a, **k: None)

    launcher = create_kirikiri_static_launcher(exe, SimpleNamespace(name="kirikiri"))

    assert launcher is not None
    payload = _ps_payload(launcher)
    assert "kirikiri_native_launcher.exe" in payload
    assert "KIRIKIRI_NATIVE_HOOK_PROFILE" in payload
    assert "KIRIKIRI_ENABLE_EMBED_TEXT_REPLACE" in payload
    assert "KIRIKIRI_CAPTURE_HOOKS" in payload
    assert "KIRIKIRI_EXPERIMENTAL_TEXT_HOOKS" in payload
    assert "'display'" in payload
    assert "Game.exe" in payload


def test_protected_archive_embed_launcher_uses_state_checkpoint(tmp_path, monkeypatch):
    """工作区检查点缺失时，应用状态目录里的检查点也必须能驱动内嵌启动器。"""
    exe = tmp_path / "Game.exe"
    exe.write_bytes(b"MZ")
    meta = tmp_path / "_translation_meta"
    meta.mkdir()
    (meta / "extract_diagnostics.json").write_text(json.dumps({
        "protected_archives": ["data.xp3"],
        "index_anomaly": "unknown_chunk",
        "static_external_extractor": "msg_tool",
    }), encoding="utf-8")
    state_checkpoint = tmp_path / "state" / "translation_checkpoint.json"
    state_checkpoint.parent.mkdir()
    state_checkpoint.write_text(json.dumps({"items": []}), encoding="utf-8")
    monkeypatch.setattr("core.checkpoint_store.persistent_checkpoint_path", lambda *a, **k: state_checkpoint)
    native = meta / "kirikiri_native_launcher.exe"
    native.write_bytes(b"MZ")
    monkeypatch.setattr("core.launcher._prepare_native_kirikiri_runtime", lambda *a, **k: native)
    monkeypatch.setattr("core.launcher._prepare_kirikiri_standalone_launcher", lambda *a, **k: None)

    launcher = create_kirikiri_static_launcher(exe, SimpleNamespace(name="kirikiri"))

    assert launcher is not None
    payload = _ps_payload(launcher)
    assert "KIRIKIRI_ENABLE_EMBED_TEXT_REPLACE" in payload
    assert "kirikiri_native_launcher.exe" in payload


def test_protected_archive_prefers_standalone_exe_and_removes_batch(tmp_path, monkeypatch):
    """受保护封包只暴露一个入口：独立 EXE，旧批处理要被清掉。"""
    exe = tmp_path / "Game.exe"
    exe.write_bytes(b"MZ")
    meta = tmp_path / "_translation_meta"
    meta.mkdir()
    (meta / "extract_diagnostics.json").write_text(json.dumps({
        "protected_archives": ["data.xp3"],
        "index_anomaly": "unknown_chunk",
        "static_external_extractor": "msg_tool",
    }), encoding="utf-8")
    (meta / "translation_checkpoint.json").write_text(json.dumps({"items": []}), encoding="utf-8")
    native = meta / "kirikiri_native_launcher.exe"
    native.write_bytes(b"MZ")
    standalone = tmp_path / "启动汉化版.exe"
    standalone.write_bytes(b"MZ")
    legacy_batch = tmp_path / "启动汉化版.bat"
    legacy_batch.write_text("old entry", encoding="ascii")
    monkeypatch.setattr("core.launcher._prepare_native_kirikiri_runtime", lambda *a, **k: native)
    monkeypatch.setattr("core.launcher._prepare_kirikiri_standalone_launcher", lambda *a, **k: standalone)

    launcher = create_kirikiri_static_launcher(exe, SimpleNamespace(name="kirikiri"))

    assert launcher == standalone
    assert not legacy_batch.exists()


def test_plain_archive_static_launcher_still_starts_game_directly(tmp_path):
    exe = tmp_path / "Game.exe"
    exe.write_bytes(b"MZ")

    launcher = create_kirikiri_static_launcher(exe, SimpleNamespace(name="kirikiri"))

    assert launcher is not None
    payload = _ps_payload(launcher)
    assert "$psi.FileName = $Exe" in payload
    assert "kirikiri_native_launcher.exe" not in payload


@pytest.mark.parametrize("injector", [None, "", "frida", "xunity"])
@pytest.mark.parametrize("launcher_kind", ["missing", "legacy_realtime", "static"])
def test_finished_krkr_static_run_never_enters_runtime(tmp_path, monkeypatch, injector, launcher_kind):
    exe = tmp_path / "Game.exe"
    exe.write_bytes(b"MZ")
    engine = SimpleNamespace(name="kirikiri")
    if launcher_kind == "legacy_realtime":
        runtime = _write_native_kirikiri_launcher(
            tmp_path, exe.name, tmp_path / "_translation_meta" / "kirikiri_native_launcher.exe"
        )
        runtime.rename(tmp_path / "启动汉化版.bat")
    elif launcher_kind == "static":
        create_kirikiri_static_launcher(exe, engine)

    calls = []
    monkeypatch.setattr(stage, "launch_game", lambda *args: calls.append(("game", args)) or True)
    monkeypatch.setattr(
        stage, "launch_translated_launcher",
        lambda *args, **kwargs: calls.append(("static", args)),
    )

    def forbid_runtime(*_args, **_kwargs):
        raise AssertionError("KRKR static completion must never inject a realtime hook")

    monkeypatch.setattr(stage, "launch_with_injector", forbid_runtime)
    assert stage._launch_finished_game(tmp_path, engine, injector, None) is True
    assert len(calls) == 1
    assert calls[0][0] == ("static" if launcher_kind == "static" else "game")


def test_failed_static_launcher_falls_back_to_plain_exe(tmp_path, monkeypatch):
    exe = tmp_path / "Game.exe"
    exe.write_bytes(b"MZ")
    engine = SimpleNamespace(name="kirikiri")
    create_kirikiri_static_launcher(exe, engine)

    def fail_start(*_args, **_kwargs):
        raise OSError("launch failed")

    calls = []
    monkeypatch.setattr(stage, "launch_translated_launcher", fail_start)
    monkeypatch.setattr(stage, "launch_game", lambda *args: calls.append(args) or True)
    assert stage._launch_finished_game(tmp_path, engine, "frida", None) is True
    assert calls == [(tmp_path, engine)]


def test_static_pipeline_records_preserved_realtime_launcher_for_uninstall(tmp_path):
    from core.manifest import GameManifest

    exe = tmp_path / "Game.exe"
    exe.write_bytes(b"MZ")
    realtime = _write_native_kirikiri_launcher(
        tmp_path, exe.name, tmp_path / "_translation_meta" / "kirikiri_native_launcher.exe"
    )
    realtime.rename(tmp_path / "启动汉化版.bat")
    pipeline = SimpleNamespace(manifest=GameManifest.for_game(tmp_path), diagnostics=None)
    engine = SimpleNamespace(name="kirikiri")
    stage.create_runtime_launchers(pipeline, exe, engine, None)

    records = {entry["rel"]: entry for entry in pipeline.manifest.data["created_files"]}
    assert "启动汉化版.bat" in records
    assert records["启动实时汉化版.bat"]["kind"] == "realtime_launcher"
    assert not (tmp_path / "_translation_meta").exists()
    result = pipeline.manifest.restore_and_uninstall()
    assert result["errors"] == 0
    assert exe.exists()
    assert not (tmp_path / "启动汉化版.bat").exists()
    assert not (tmp_path / "启动实时汉化版.bat").exists()
