from types import SimpleNamespace

import pytest

from core import pipeline_runtime_stage as stage
from core.launcher import _write_native_kirikiri_launcher, create_kirikiri_static_launcher


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
