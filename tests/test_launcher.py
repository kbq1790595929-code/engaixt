"""Launcher selection regression tests."""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import Config
from core.exe_selector import find_main_exe
from core.launcher import (
    _copy_or_reuse_locked_runtime,
    _find_latest_checkpoint_for_game,
    _is_valid_native_bgi_runtime,
    _kirikiri_native_hook_profile,
    _resolve_bgi_preload,
    _resolve_checkpoint_for_overlay,
    _launch_with_godot_display_hook,
    _launch_with_kirikiri_frida_capture,
    _select_bgi_launch_exe,
    _select_launch_exe,
    _write_bgi_native_map,
    _write_kirikiri_native_map,
    _write_native_kirikiri_launcher,
    create_bgi_hook_launcher,
    create_godot_display_hook_launcher,
    create_kirikiri_native_launcher,
    launch_kirikiri_native_runtime,
    launch_translated_launcher,
)
from core.pipeline import Pipeline, _is_runtime_frida_mode
from engines.base import TextItem
from engines.godot_pck import GodotPckEngine


def _load_bgi_preload_map_fn():
    script = Path(__file__).parent.parent / "frida" / "run_bgi_realtime.py"
    spec = importlib.util.spec_from_file_location("run_bgi_realtime_for_test", script)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module._load_preload_map


def _load_kirikiri_preload_map_fn():
    script = Path(__file__).parent.parent / "frida" / "run_kirikiri_realtime.py"
    spec = importlib.util.spec_from_file_location("run_kirikiri_realtime_for_test", script)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module._load_preload_map


def test_locked_kirikiri_runtime_reuses_only_identical_file(tmp_path, monkeypatch):
    source = tmp_path / "source.dll"
    target = tmp_path / "target.dll"
    source.write_bytes(b"current-runtime")
    target.write_bytes(b"current-runtime")
    monkeypatch.setattr("core.launcher.shutil.copy2", lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError()))

    _copy_or_reuse_locked_runtime(source, target)


def test_locked_kirikiri_runtime_rejects_stale_file(tmp_path, monkeypatch):
    source = tmp_path / "source.dll"
    target = tmp_path / "target.dll"
    source.write_bytes(b"current-runtime")
    target.write_bytes(b"stale-runtime")
    monkeypatch.setattr("core.launcher.shutil.copy2", lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError()))
    monkeypatch.setattr("core.launcher.time.sleep", lambda _seconds: None)

    try:
        _copy_or_reuse_locked_runtime(source, target)
    except PermissionError as exc:
        assert "locked and outdated" in str(exc)
    else:
        raise AssertionError("stale locked runtime must not be reused")


def _load_godot_preload_map_fn():
    script = Path(__file__).parent.parent / "frida" / "run_godot_display_hook.py"
    spec = importlib.util.spec_from_file_location("run_godot_display_hook_for_test", script)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module._load_preload_map


def _decode_encoded_command_from_bat(content: str) -> str:
    encoded_line = next(line for line in content.splitlines() if "-EncodedCommand" in line)
    encoded = encoded_line.split("-EncodedCommand", 1)[1].strip()
    return base64.b64decode(encoded).decode("utf-16le")


def _exe(path: Path, size: int = 1024 * 1024):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MZ" + b"\0" * max(0, size - 2))


def _pe_x86(path: Path, size: int = 1024 * 1024):
    data = bytearray(b"\0" * size)
    data[:2] = b"MZ"
    data[0x3C:0x40] = (0x80).to_bytes(4, "little")
    data[0x80:0x84] = b"PE\0\0"
    data[0x84:0x86] = (0x14C).to_bytes(2, "little")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _pe_x64(path: Path, size: int = 1024 * 1024):
    data = bytearray(b"\0" * size)
    data[:2] = b"MZ"
    data[0x3C:0x40] = (0x80).to_bytes(4, "little")
    data[0x80:0x84] = b"PE\0\0"
    data[0x84:0x86] = (0x8664).to_bytes(2, "little")
    data[0x98:0x9A] = (0x20B).to_bytes(2, "little")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _use_fake_bgi_runtime(monkeypatch, runtime_dir: Path, arch: str = "x86"):
    expected = ("assets", "bgi_native_runtime", arch)

    def fake_resource_path(*parts):
        if tuple(str(part) for part in parts) == expected:
            return runtime_dir
        return Path(__file__).parent.parent.joinpath(*parts)

    monkeypatch.setattr("core.launcher.resource_path", fake_resource_path)
    monkeypatch.setattr("core.launcher._is_valid_native_bgi_runtime", lambda *_args: True)


def test_bgi_native_runtime_rejects_placeholder_assets(tmp_path):
    launcher = tmp_path / "bgi_native_launcher.exe"
    hook = tmp_path / "bgi_native_hook.dll"
    launcher.write_bytes(b"launcher")
    hook.write_bytes(b"hook")

    assert not _is_valid_native_bgi_runtime(launcher, hook, "x86")


def test_bgi_native_runtime_accepts_matching_pe_assets(tmp_path):
    launcher = tmp_path / "bgi_native_launcher.exe"
    hook = tmp_path / "bgi_native_hook.dll"
    _pe_x86(launcher)
    _pe_x86(hook)

    assert _is_valid_native_bgi_runtime(launcher, hook, "x86")


class _Engine:
    def __init__(self, exe: Path):
        self.exe = exe

    def find_exe(self, _path: Path) -> Path:
        return self.exe


class _NamedEngine:
    def __init__(self, name: str):
        self.name = name


def test_manual_exe_overrides_engine_and_auto_selection():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        manual = root / "ManualChoice.exe"
        auto = root / "AutoChoice.exe"
        _exe(manual)
        _exe(auto, 10 * 1024 * 1024)

        assert _select_launch_exe(manual, _Engine(auto)) == manual


def test_find_main_exe_ignores_mtool_and_integrity_checker():
    with tempfile.TemporaryDirectory() as tmp:
        game_dir = Path(tmp) / "\u604b\u611b\u3001\u306f\u3058\u3081\u307e\u3057\u3066"
        game_dir.mkdir()
        _exe(game_dir / "MTool_Game.exe", 5 * 1024 * 1024)
        _exe(game_dir / "\u30d5\u30a1\u30a4\u30eb\u7834\u640d\u30c1\u30a7\u30c3\u30af\u30c4\u30fc\u30eb.exe", 600 * 1024)
        real = game_dir / "\u604b\u611b\u3001\u306f\u3058\u3081\u307e\u3057\u3066.exe"
        _exe(real, 4 * 1024 * 1024)

        assert find_main_exe(game_dir) == real


def test_godot_pck_find_exe_prefers_main_exe_over_console_helper():
    with tempfile.TemporaryDirectory() as tmp:
        game_dir = Path(tmp) / "kick_the_demon_out"
        game_dir.mkdir()
        console = game_dir / "kick_the_demon_out.console.exe"
        main = game_dir / "kick_the_demon_out.exe"
        _exe(console, 180 * 1024)
        _exe(main, 80 * 1024 * 1024)
        (game_dir / "kick_the_demon_out.pck").write_bytes(b"GDPC")

        assert GodotPckEngine().find_exe(game_dir) == main


def test_bgi_realtime_preload_accepts_checkpoint_json():
    with tempfile.TemporaryDirectory() as tmp:
        checkpoint = Path(tmp) / "translation_checkpoint.json"
        checkpoint.write_text(json.dumps({
            "items": [
                {"original": "\u30d7\u30ed\u30ed\u30fc\u30b0", "translated": "\u5e8f\u7ae0"},
                {"original": "\u540c\u3058", "translated": "\u540c\u3058"},
                {"original": "\u672a\u7ffb\u8a33", "translated": ""},
            ],
        }), encoding="utf-8")

        assert _load_bgi_preload_map_fn()(checkpoint) == {"\u30d7\u30ed\u30ed\u30fc\u30b0": "\u5e8f\u7ae0"}


def test_kirikiri_realtime_preload_accepts_checkpoint_json():
    with tempfile.TemporaryDirectory() as tmp:
        checkpoint = Path(tmp) / "translation_checkpoint.json"
        checkpoint.write_text(json.dumps({
            "items": [
                {"original": "はじめから", "translated": "从头开始"},
                {"original": "同じ", "translated": "同じ"},
                {"original": "未翻訳", "translated": ""},
            ],
        }, ensure_ascii=False), encoding="utf-8")

        assert _load_kirikiri_preload_map_fn()(checkpoint) == {"はじめから": "从头开始"}


def test_bgi_auto_uses_native_launcher_without_overriding_manual_choice():
    with tempfile.TemporaryDirectory() as tmp:
        game_dir = Path(tmp)
        engine = _NamedEngine("bgi")
        pipeline = Pipeline()

        assert pipeline._auto_select_injector(None, engine, game_dir) is None
        assert pipeline._auto_select_injector("", engine, game_dir) is None
        assert pipeline._auto_select_injector("xunity", engine, game_dir) == "xunity"


def test_godot_display_preload_accepts_checkpoint_json_and_visible_variants():
    with tempfile.TemporaryDirectory() as tmp:
        checkpoint = Path(tmp) / "translation_checkpoint.json"
        checkpoint.write_text(json.dumps({
            "items": [
                {"original": "[i]The Bar opened...[/i]", "translated": "[i]酒吧开门了……[/i]"},
                {"original": "- Start", "translated": "- 开始"},
                {"original": "Same", "translated": "Same"},
                {"original": "Missing", "translated": ""},
            ],
        }, ensure_ascii=False), encoding="utf-8")

        preload = _load_godot_preload_map_fn()(checkpoint)

        assert preload["[i]The Bar opened...[/i]"] == "[i]酒吧开门了……[/i]"
        assert preload["The Bar opened..."] == "酒吧开门了……"
        assert preload["Start"] == "开始"
        assert "Same" not in preload


def test_godot_display_preload_adds_bbcode_visible_segments():
    with tempfile.TemporaryDirectory() as tmp:
        checkpoint = Path(tmp) / "translation_checkpoint.json"
        checkpoint.write_text(json.dumps({
            "items": [{
                "file": "Timelines/intro.dtl",
                "key": "text_1",
                "original": "[i]Opening [b]bold span[/b], trailing text.[/i]",
                "translated": "[i]Opening CN [b]bold CN[/b], trailing CN.[/i]",
            }],
        }, ensure_ascii=False), encoding="utf-8")

        preload = _load_godot_preload_map_fn()(checkpoint)

        assert preload["bold span"] == "bold CN"
        assert preload[", trailing text."] == ", trailing CN."


def test_godot_pck_auto_uses_static_patch():
    with tempfile.TemporaryDirectory() as tmp:
        game_dir = Path(tmp)
        engine = _NamedEngine("godot_pck")
        pipeline = Pipeline()

        # godot_pck now defaults to static PCK repack — no injector needed
        assert pipeline._auto_select_injector(None, engine, game_dir) is None
        assert pipeline._auto_select_injector("", engine, game_dir) is None
        # explicit injector is still honored
        assert pipeline._auto_select_injector("xunity", engine, game_dir) == "xunity"
        assert _is_runtime_frida_mode("frida", engine, game_dir)


def test_godot_frida_auto_uses_frida_hook():
    with tempfile.TemporaryDirectory() as tmp:
        game_dir = Path(tmp)
        engine = _NamedEngine("godot_frida")
        pipeline = Pipeline()

        assert pipeline._auto_select_injector(None, engine, game_dir) == "frida"
        assert pipeline._auto_select_injector("", engine, game_dir) == "frida"


def test_godot_runtime_restores_pre_tool_pck_before_hook():
    with tempfile.TemporaryDirectory() as tmp:
        game_dir = Path(tmp)
        pck = game_dir / "game.pck"
        backup = game_dir / "game.pck.pre_tool"
        pck.write_bytes(b"patched-broken")
        backup.write_bytes(b"original-ok")

        restored = Pipeline()._restore_godot_static_patch_artifacts_for_runtime(game_dir)

        assert restored == [str(pck)]
        assert pck.read_bytes() == b"original-ok"
        assert backup.exists()


def test_kirikiri_frida_is_runtime_overlay_mode():
    with tempfile.TemporaryDirectory() as tmp:
        game_dir = Path(tmp)
        assert _is_runtime_frida_mode("frida", _NamedEngine("kirikiri"), game_dir)
        assert not _is_runtime_frida_mode(None, _NamedEngine("kirikiri"), game_dir)


def test_kirikiri_frida_launcher_disables_live_api(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        game_dir = Path(tmp)
        exe = game_dir / "krkr.exe"
        _exe(exe)
        meta = game_dir / "_translation_meta"
        meta.mkdir()
        checkpoint = meta / "translation_checkpoint.json"
        checkpoint.write_text(json.dumps({
            "items": [{"original": "はじめから", "translated": "从头开始"}],
        }, ensure_ascii=False), encoding="utf-8")

        calls = []

        class DummyProc:
            returncode = 0

            def wait(self):
                return 0

        def fake_popen(cmd, cwd=None, **_kwargs):
            calls.append((cmd, cwd))
            return DummyProc()

        monkeypatch.setattr("core.launcher.subprocess.Popen", fake_popen)

        assert _launch_with_kirikiri_frida_capture(game_dir, _NamedEngine("kirikiri"), checkpoint)
        cmd = calls[0][0]
        assert "run_kirikiri_realtime.py" in " ".join(map(str, cmd))
        assert "--no-live-translate" in cmd
        assert "--live-translate" not in cmd
        assert "--capture" in cmd
        assert str(game_dir / "_translation_meta" / "kirikiri_runtime_capture.jsonl") in cmd


def test_translated_batch_launcher_uses_hidden_cmd_on_windows(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        game_dir = Path(tmp)
        launcher = game_dir / "启动汉化版.bat"
        launcher.write_text("@echo off\r\n", encoding="utf-8")
        calls = []

        class DummyProc:
            pass

        def fake_popen(cmd, cwd=None, **kwargs):
            calls.append((cmd, cwd, kwargs))
            return DummyProc()

        monkeypatch.setattr("core.launcher.sys.platform", "win32")
        monkeypatch.setenv("ComSpec", "cmd.exe")
        monkeypatch.setattr("core.launcher.subprocess.CREATE_NO_WINDOW", 0x08000000, raising=False)
        monkeypatch.setattr("core.launcher._hidden_startupinfo", lambda: "hidden-startup")
        monkeypatch.setattr("core.launcher.subprocess.Popen", fake_popen)

        proc = launch_translated_launcher(launcher, cwd=game_dir)

        assert isinstance(proc, DummyProc)
        assert calls == [
            (
                ["cmd.exe", "/d", "/c", "call", f'"{launcher}"'],
                str(game_dir),
                {"creationflags": 0x08000000, "startupinfo": "hidden-startup"},
            )
        ]


def test_bgi_preload_prefers_game_metadata_dir():
    with tempfile.TemporaryDirectory() as tmp:
        game_dir = Path(tmp)
        meta = game_dir / "_translation_meta"
        meta.mkdir()
        runtime_map = meta / "bgi_runtime_map_cached.json"
        runtime_map.write_text("{}", encoding="utf-8")

        assert _resolve_bgi_preload(game_dir, None) == runtime_map


def test_bgi_preload_uses_game_checkpoint_before_workspace_checkpoint():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        game_dir = root / "Game"
        meta = game_dir / "_translation_meta"
        workspace = root / "workspace"
        meta.mkdir(parents=True)
        workspace.mkdir()
        game_checkpoint = meta / "translation_checkpoint.json"
        workspace_checkpoint = workspace / "translation_checkpoint.json"
        game_checkpoint.write_text("{}", encoding="utf-8")
        workspace_checkpoint.write_text("{}", encoding="utf-8")

        assert _resolve_bgi_preload(game_dir, workspace_checkpoint) == game_checkpoint


def test_bgi_launcher_prefers_bgi_exe_over_helper_exes():
    with tempfile.TemporaryDirectory() as tmp:
        game_dir = Path(tmp)
        bgi = game_dir / "BGI.exe"
        helper = game_dir / "ESS.exe"
        _exe(bgi)
        _exe(helper, 4 * 1024 * 1024)

        assert _select_bgi_launch_exe(game_dir, _Engine(helper)) == bgi


def test_checkpoint_is_mirrored_to_game_metadata_dir():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        game_dir = root / "Game"
        game_dir.mkdir()
        checkpoint = root / "workspace" / "translation_checkpoint.json"
        checkpoint.parent.mkdir()
        item = TextItem(file="script.txt", key="1", original="Hello", translated="你好")

        Pipeline()._save_checkpoint_json([item], game_dir, "en", "zh-CN", checkpoint)

        game_checkpoint = game_dir / "_translation_meta" / "translation_checkpoint.json"
        assert checkpoint.exists()
        assert game_checkpoint.exists()
        assert game_checkpoint.read_text(encoding="utf-8") == checkpoint.read_text(encoding="utf-8")


def test_overlay_checkpoint_prefers_game_metadata_dir():
    with tempfile.TemporaryDirectory() as tmp:
        game_dir = Path(tmp)
        meta = game_dir / "_translation_meta"
        meta.mkdir()
        checkpoint = meta / "translation_checkpoint.json"
        checkpoint.write_text("{}", encoding="utf-8")

        assert _resolve_checkpoint_for_overlay(game_dir, None) == checkpoint


def test_bgi_hook_launcher_is_written_with_preload_and_sjis_ext():
    repo_root = Path(__file__).parent.parent
    runtime_dir = repo_root / "assets" / "bgi_native_runtime" / "x86"
    launcher_src = runtime_dir / "bgi_native_launcher.exe"
    hook_src = runtime_dir / "bgi_native_hook.dll"
    old_launcher = launcher_src.read_bytes() if launcher_src.exists() else None
    old_hook = hook_src.read_bytes() if hook_src.exists() else None
    launcher_src.unlink(missing_ok=True)
    hook_src.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        try:
            game_dir = Path(tmp) / "游戏"
            game_dir.mkdir()
            exe = game_dir / "Game.exe"
            _exe(exe)
            checkpoint = game_dir / "_translation_meta" / "translation_checkpoint.json"
            checkpoint.parent.mkdir()
            checkpoint.write_text("{}", encoding="utf-8")
            (game_dir / "sjis_ext.bin").write_bytes(b"\x00N")

            launcher = create_bgi_hook_launcher(game_dir, _Engine(exe), checkpoint)

            assert launcher == game_dir / "启动汉化版.bat"
            assert launcher.exists()
            assert (game_dir / "_translation_meta" / "bgi_hook" / "run_bgi_realtime.py").exists()
            assert (game_dir / "_translation_meta" / "bgi_hook" / "bgi_realtime_hook.js").exists()
            content = launcher.read_text(encoding="utf-8")
            assert "-EncodedCommand" in content
            import base64
            encoded_line = next(line for line in content.splitlines() if "-EncodedCommand" in line)
            encoded = encoded_line.split("-EncodedCommand", 1)[1].strip()
            script = base64.b64decode(encoded).decode("utf-16le")
            assert "BGI_GAME_DIR" in content
            assert "$GameDir" in script
            assert "_translation_meta" in script
            assert "bgi_hook" in script
            assert "run_bgi_realtime.py" in script
            assert "Game.exe" in script
            assert "sjis_ext.bin" in script
            assert str(exe) not in script
            assert str(checkpoint) not in script
            assert str(game_dir / "sjis_ext.bin") not in script
        finally:
            if old_launcher is not None:
                launcher_src.write_bytes(old_launcher)
            if old_hook is not None:
                hook_src.write_bytes(old_hook)


def test_godot_display_hook_launcher_is_written_with_preload():
    with tempfile.TemporaryDirectory() as tmp:
        game_dir = Path(tmp) / "Game"
        game_dir.mkdir()
        exe = game_dir / "GodotGame.exe"
        _exe(exe)
        checkpoint = game_dir / "_translation_meta" / "translation_checkpoint.json"
        checkpoint.parent.mkdir()
        checkpoint.write_text(json.dumps({
            "items": [{"original": "Hello", "translated": "你好"}],
        }, ensure_ascii=False), encoding="utf-8")

        launcher = create_godot_display_hook_launcher(game_dir, _Engine(exe), checkpoint)

        assert launcher == game_dir / "启动汉化版.bat"
        assert launcher.exists()
        assert (game_dir / "_translation_meta" / "godot_hook" / "run_godot_display_hook.py").exists()
        assert (game_dir / "_translation_meta" / "godot_hook" / "godot_display_hook.js").exists()
        content = launcher.read_text(encoding="utf-8")
        assert "-EncodedCommand" in content
        encoded_line = next(line for line in content.splitlines() if "-EncodedCommand" in line)
        encoded = encoded_line.split("-EncodedCommand", 1)[1].strip()
        script = base64.b64decode(encoded).decode("utf-16le")
        assert "GODOT_GAME_DIR" in content
        assert "godot_hook" in script
        assert "run_godot_display_hook.py" in script
        assert "GodotGame.exe" in script
        assert "translation_checkpoint.json" in script
        assert "godot_runtime_capture.jsonl" in script
        assert "--capture" in script
        assert "--no-live-translate" in script
        # --progressive replaces typewriter-in-progress text with a length-ratio-guessed
        # partial translation directly inside Godot's live text shaping call, which breaks
        # rendering (reported bug: translated text vanishes mid-typewriter). It must stay
        # off for this direct-replacement launcher; it is only safe for the isolated overlay path.
        assert "--progressive" not in script
        assert str(exe) not in script
        assert str(checkpoint) not in script


def test_bgi_native_map_is_base64_tsv():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        checkpoint = root / "translation_checkpoint.json"
        out = root / "bgi_native_map.tsv"
        checkpoint.write_text(json.dumps({
            "items": [
                {"original": "こんにちは", "translated": "你好"},
                {"original": "同じ", "translated": "同じ"},
                {"original": "未翻訳", "translated": ""},
            ],
        }, ensure_ascii=False), encoding="utf-8")

        count = _write_bgi_native_map(checkpoint, out)

        assert count == 1
        lines = out.read_text(encoding="ascii").splitlines()
        assert lines[0].startswith("#")
        assert len(lines) == 2
        import base64
        src, dst = lines[1].split("\t")
        assert base64.b64decode(src).decode("utf-8") == "こんにちは"
        assert base64.b64decode(dst).decode("utf-8") == "你好"

def test_kirikiri_native_map_is_base64_tsv():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        checkpoint = root / "translation_checkpoint.json"
        out = root / "kirikiri_native_map.tsv"
        checkpoint.write_text(json.dumps({
            "items": [
                {"original": "「はぁ、はぁ……ご、ごめんなさい……っ！」", "translated": "「哈、哈……对、对不起……！」"},
                {"original": "朝[r]昼", "translated": "早上[r]中午"},
                {"original": "@voice name='翼（幼少期）' word='今日はいい天気だ'", "translated": "今天天气真好"},
                {"original": "@jump storage=\"next.ks\"", "translated": "跳转到 next.ks"},
                {"original": "同じ", "translated": "同じ"},
            ],
        }, ensure_ascii=False), encoding="utf-8")

        count = _write_kirikiri_native_map(checkpoint, out)

        assert count == 6
        lines = out.read_text(encoding="ascii").splitlines()
        assert lines[0].startswith("#")
        pairs = {
            tuple(base64.b64decode(part).decode("utf-8") for part in line.split("\t"))
            for line in lines[1:]
        }
        assert ("「はぁ、はぁ……ご、ごめんなさい……っ！」", "「哈、哈……对、对不起……！」") in pairs
        assert ("朝[r]昼", "早上[r]中午") in pairs
        assert ("朝\n昼", "早上\n中午") in pairs
        assert ("朝昼", "早上中午") in pairs
        assert ("今日はいい天気だ", "今天天气真好") in pairs
        assert ("翼「今日はいい天気だ」", "翼「今天天气真好」") in pairs
        assert ("@jump storage=\"next.ks\"", "跳转到 next.ks") not in pairs


def test_bgi_launcher_prefers_native_runtime_when_built(tmp_path, monkeypatch):
    runtime_dir = tmp_path / "assets" / "bgi_native_runtime" / "x86"
    _use_fake_bgi_runtime(monkeypatch, runtime_dir)
    launcher_src = runtime_dir / "bgi_native_launcher.exe"
    hook_src = runtime_dir / "bgi_native_hook.dll"
    old_launcher = launcher_src.read_bytes() if launcher_src.exists() else None
    old_hook = hook_src.read_bytes() if hook_src.exists() else None
    try:
        runtime_dir.mkdir(parents=True, exist_ok=True)
        launcher_src.write_bytes(b"launcher")
        hook_src.write_bytes(b"hook")
        with tempfile.TemporaryDirectory() as tmp:
            game_dir = Path(tmp) / "Game"
            game_dir.mkdir()
            exe = game_dir / "BGI.exe"
            _exe(exe)
            meta = game_dir / "_translation_meta"
            meta.mkdir()
            checkpoint = meta / "translation_checkpoint.json"
            checkpoint.write_text(json.dumps({
                "items": [{"original": "選択肢", "translated": "选项"}],
            }, ensure_ascii=False), encoding="utf-8")

            launcher = create_bgi_hook_launcher(game_dir, _Engine(exe), checkpoint)

            assert launcher == game_dir / "启动汉化版.bat"
            assert (meta / "bgi_native_launcher.exe").read_bytes() == b"launcher"
            assert (meta / "bgi_native_hook.dll").read_bytes() == b"hook"
            assert (meta / "bgi_native_map.tsv").exists()
            content = launcher.read_text(encoding="ascii")
            script = _decode_encoded_command_from_bat(content)
            assert "bgi_native_launcher.exe" in script
            assert "BGI.exe" in script
            assert "-EncodedCommand" in content
            assert "python" not in script.lower()
            assert "frida" not in script.lower()
    finally:
        if old_launcher is None:
            launcher_src.unlink(missing_ok=True)
        else:
            launcher_src.write_bytes(old_launcher)
        if old_hook is None:
            hook_src.unlink(missing_ok=True)
        else:
            hook_src.write_bytes(old_hook)
        try:
            if runtime_dir.exists() and not any(runtime_dir.iterdir()):
                runtime_dir.rmdir()
        except OSError:
            pass


def test_bgi_native_launcher_keeps_non_ascii_exe_name_out_of_bat_codepage(tmp_path, monkeypatch):
    runtime_dir = tmp_path / "assets" / "bgi_native_runtime" / "x86"
    _use_fake_bgi_runtime(monkeypatch, runtime_dir)
    launcher_src = runtime_dir / "bgi_native_launcher.exe"
    hook_src = runtime_dir / "bgi_native_hook.dll"
    old_launcher = launcher_src.read_bytes() if launcher_src.exists() else None
    old_hook = hook_src.read_bytes() if hook_src.exists() else None
    try:
        runtime_dir.mkdir(parents=True, exist_ok=True)
        launcher_src.write_bytes(b"launcher")
        hook_src.write_bytes(b"hook")
        with tempfile.TemporaryDirectory() as tmp:
            game_dir = Path(tmp) / "Hooksoft"
            game_dir.mkdir()
            exe = game_dir / "放課後シンデレラ２_Crack.exe"
            _exe(exe)
            meta = game_dir / "_translation_meta"
            meta.mkdir()
            checkpoint = meta / "translation_checkpoint.json"
            checkpoint.write_text(json.dumps({"items": []}, ensure_ascii=False), encoding="utf-8")

            launcher = create_bgi_hook_launcher(exe, _Engine(exe), checkpoint)

            content = launcher.read_text(encoding="ascii")
            assert "放課後シンデレラ２_Crack.exe" not in content
            assert all(ord(ch) < 128 for ch in content)
            script = _decode_encoded_command_from_bat(content)
            assert "放課後シンデレラ２_Crack.exe" in script
            assert "bgi_native_launcher.exe" in script
    finally:
        if old_launcher is None:
            launcher_src.unlink(missing_ok=True)
        else:
            launcher_src.write_bytes(old_launcher)
        if old_hook is None:
            hook_src.unlink(missing_ok=True)
        else:
            hook_src.write_bytes(old_hook)
        try:
            if runtime_dir.exists() and not any(runtime_dir.iterdir()):
                runtime_dir.rmdir()
        except OSError:
            pass


def test_bgi_launcher_uses_x64_native_runtime_for_x64_game(tmp_path, monkeypatch):
    runtime_dir = tmp_path / "assets" / "bgi_native_runtime" / "x64"
    _use_fake_bgi_runtime(monkeypatch, runtime_dir, "x64")
    launcher_src = runtime_dir / "bgi_native_launcher.exe"
    hook_src = runtime_dir / "bgi_native_hook.dll"
    old_launcher = launcher_src.read_bytes() if launcher_src.exists() else None
    old_hook = hook_src.read_bytes() if hook_src.exists() else None
    try:
        runtime_dir.mkdir(parents=True, exist_ok=True)
        launcher_src.write_bytes(b"launcher64")
        hook_src.write_bytes(b"hook64")
        with tempfile.TemporaryDirectory() as tmp:
            game_dir = Path(tmp) / "Game"
            game_dir.mkdir()
            exe = game_dir / "BGI64.exe"
            _pe_x64(exe)
            meta = game_dir / "_translation_meta"
            meta.mkdir()
            checkpoint = meta / "translation_checkpoint.json"
            checkpoint.write_text(json.dumps({
                "items": [{"original": "王様", "translated": "国王"}],
            }, ensure_ascii=False), encoding="utf-8")

            launcher = create_bgi_hook_launcher(game_dir, _Engine(exe), checkpoint)

            assert launcher == game_dir / "启动汉化版.bat"
            assert (meta / "bgi_native_launcher.exe").read_bytes() == b"launcher64"
            assert (meta / "bgi_native_hook.dll").read_bytes() == b"hook64"
            assert (meta / "bgi_native_runtime_arch.txt").read_text(encoding="ascii").strip() == "x64"
    finally:
        if old_launcher is None:
            launcher_src.unlink(missing_ok=True)
        else:
            launcher_src.write_bytes(old_launcher)
        if old_hook is None:
            hook_src.unlink(missing_ok=True)
        else:
            hook_src.write_bytes(old_hook)
        try:
            if runtime_dir.exists() and not any(runtime_dir.iterdir()):
                runtime_dir.rmdir()
        except OSError:
            pass


def test_bgi_native_runtime_writes_empty_map_without_checkpoint(tmp_path, monkeypatch):
    runtime_dir = tmp_path / "assets" / "bgi_native_runtime" / "x86"
    _use_fake_bgi_runtime(monkeypatch, runtime_dir)
    launcher_src = runtime_dir / "bgi_native_launcher.exe"
    hook_src = runtime_dir / "bgi_native_hook.dll"
    old_launcher = launcher_src.read_bytes() if launcher_src.exists() else None
    old_hook = hook_src.read_bytes() if hook_src.exists() else None
    try:
        runtime_dir.mkdir(parents=True, exist_ok=True)
        launcher_src.write_bytes(b"launcher")
        hook_src.write_bytes(b"hook")
        with tempfile.TemporaryDirectory() as tmp:
            game_dir = Path(tmp) / "Game"
            game_dir.mkdir()
            exe = game_dir / "BGI.exe"
            _pe_x86(exe)

            launcher = create_bgi_hook_launcher(game_dir, _Engine(exe), None)

            assert launcher == game_dir / "启动汉化版.bat"
            map_text = (game_dir / "_translation_meta" / "bgi_native_map.tsv").read_text(encoding="ascii")
            assert map_text == "# base64_utf8_original\tbase64_utf8_translated\n"
    finally:
        if old_launcher is None:
            launcher_src.unlink(missing_ok=True)
        else:
            launcher_src.write_bytes(old_launcher)
        if old_hook is None:
            hook_src.unlink(missing_ok=True)
        else:
            hook_src.write_bytes(old_hook)
        try:
            if runtime_dir.exists() and not any(runtime_dir.iterdir()):
                runtime_dir.rmdir()
        except OSError:
            pass


def test_latest_checkpoint_lookup_never_returns_another_games_checkpoint(tmp_path: Path):
    game_a = tmp_path / "GameA"
    game_b = tmp_path / "GameB"
    workspace = tmp_path / "workspaces"
    game_a.mkdir()
    game_b.mkdir()
    checkpoint = workspace / "job_b" / "translation_checkpoint.json"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_text(
        json.dumps({"source": str(game_b.resolve()), "items": [{"original": "a", "translated": "b"}]}),
        encoding="utf-8",
    )

    assert _find_latest_checkpoint_for_game(game_a, workspace) is None
    assert _find_latest_checkpoint_for_game(game_b, workspace) == checkpoint


def test_kirikiri_launcher_prefers_native_runtime_when_built():
    repo_root = Path(__file__).parent.parent
    runtime_dir = repo_root / "assets" / "kirikiri_native_runtime" / "x86"
    launcher_src = runtime_dir / "kirikiri_native_launcher.exe"
    hook_src = runtime_dir / "kirikiri_native_hook.dll"
    old_launcher = launcher_src.read_bytes() if launcher_src.exists() else None
    old_hook = hook_src.read_bytes() if hook_src.exists() else None
    try:
        runtime_dir.mkdir(parents=True, exist_ok=True)
        launcher_src.write_bytes(b"kirikiri-launcher")
        hook_src.write_bytes(b"kirikiri-hook")
        with tempfile.TemporaryDirectory() as tmp:
            game_dir = Path(tmp) / "Game"
            game_dir.mkdir()
            exe = game_dir / "krkr.exe"
            _pe_x86(exe)
            (game_dir / "patch.xp3").write_bytes(b"XP3\r\n \n\x1a\x8bg\x01")
            meta = game_dir / "_translation_meta"
            meta.mkdir()
            checkpoint = meta / "translation_checkpoint.json"
            checkpoint.write_text(json.dumps({
                "items": [{"original": "選択肢", "translated": "选项"}],
            }, ensure_ascii=False), encoding="utf-8")

            launcher = create_kirikiri_native_launcher(game_dir, _Engine(exe), checkpoint)

            assert launcher == game_dir / "启动汉化版.bat"
            assert (meta / "kirikiri_native_launcher.exe").read_bytes() == b"kirikiri-launcher"
            assert (meta / "kirikiri_native_hook.dll").read_bytes() == b"kirikiri-hook"
            assert (meta / "kirikiri_native_map.tsv").exists()
            content = launcher.read_text(encoding="utf-8")
            assert "chcp 65001" in content
            assert "EncodedCommand" in content
            assert "kirikiri_native_launcher.exe" not in content
            assert "krkr.exe" not in content
            encoded_line = next(line for line in content.splitlines() if "-EncodedCommand" in line)
            encoded = encoded_line.split("-EncodedCommand", 1)[1].strip()
            script = base64.b64decode(encoded).decode("utf-16le")
            assert "[System.Diagnostics.ProcessStartInfo]::new()" in script
            assert "Start-Process" not in script
            assert "$env:KIRIKIRI_NATIVE_HOOK_PROFILE = 'display'" in script
            assert "$env:KIRIKIRI_ENABLE_EMBED_TEXT_REPLACE = '1'" in script
            assert "$env:KIRIKIRI_EMBED_WAIT_MS = '6000'" in script
            assert "$UseOverlay = $true" in script
            assert "core.translation_overlay_window" in script
            assert "kirikiri_overlay_start.log" in script
            assert "function StartOverlayWindow" in script
            assert "overlay_restart attempt=" in script
            assert "HasVisibleGameWindow ([int]$currentGame.ProcessId)) { $windowSeen = $true }" in script
            assert "HasVisibleGameWindow ([int]$currentGame.ProcessId)) { $windowSeen = $true; break }" not in script
            assert "$OverlayArgPrefix = @('-m', 'core.translation_overlay_window')" in script
            assert "$overlayArgs = @() + $OverlayArgPrefix + @($GameName, $GameExeName, $Exe)" in script
            assert "$overlayProc" in script
            assert "--wait" in script
            assert "WaitForExit()" in script
            assert ".WorkingDirectory = $GameDir" in script
            assert 'set "KIRIKIRI_GAME_DIR=%~dp0"' in content
            assert "start \"\" /D \"%GAME_DIR%\"" not in content
            assert "python" not in content.lower()
            assert "frida" not in content.lower()
    finally:
        if old_launcher is None:
            launcher_src.unlink(missing_ok=True)
        else:
            launcher_src.write_bytes(old_launcher)
        if old_hook is None:
            hook_src.unlink(missing_ok=True)
        else:
            hook_src.write_bytes(old_hook)
        try:
            if runtime_dir.exists() and not any(runtime_dir.iterdir()):
                runtime_dir.rmdir()
        except OSError:
            pass


def test_kirikiri_protected_patch_launcher_uses_native_patchstream_without_overlay(tmp_path):
    game_dir = tmp_path / "Game"
    meta = game_dir / "_translation_meta"
    patch_dir = meta / "kirikiri_patch"
    patch_dir.mkdir(parents=True)
    (patch_dir / "intro.txt.scn").write_bytes(b"PSB")
    (meta / "kirikiri_patch_manifest.txt").write_text("intro.txt.scn\n", encoding="utf-8")
    (meta / "kirikiri_patch_diagnostics.json").write_text(
        json.dumps({"root_patch": False, "needs_patch_bridge": True}),
        encoding="utf-8",
    )
    native_launcher = meta / "kirikiri_native_launcher.exe"

    assert _kirikiri_native_hook_profile(game_dir) == "patchstream"
    launcher = _write_native_kirikiri_launcher(
        game_dir,
        "krkr.exe",
        native_launcher,
    )
    script = _decode_encoded_command_from_bat(launcher.read_text(encoding="utf-8"))

    assert "$env:KIRIKIRI_NATIVE_HOOK_PROFILE = 'patchstream'" in script
    assert "$env:KIRIKIRI_ENABLE_EMBED_TEXT_REPLACE = '0'" in script
    assert "$env:KIRIKIRI_EMBED_WAIT_MS = '0'" in script
    assert "$UseOverlay = $false" in script
    assert "if ($UseOverlay -and" in script


def test_kirikiri_native_profile_requires_manifest_and_bridge_diagnosis(tmp_path):
    game_dir = tmp_path / "Game"
    meta = game_dir / "_translation_meta"
    (meta / "kirikiri_patch").mkdir(parents=True)

    assert _kirikiri_native_hook_profile(game_dir) == "display"

    (meta / "kirikiri_patch_manifest.txt").write_text("intro.txt.scn\n", encoding="utf-8")
    (meta / "kirikiri_patch_diagnostics.json").write_text(
        json.dumps({"root_patch": True, "needs_patch_bridge": False}),
        encoding="utf-8",
    )
    assert _kirikiri_native_hook_profile(game_dir) == "display"


def test_kirikiri_gui_realtime_launch_avoids_cmd_and_powershell(monkeypatch):
    repo_root = Path(__file__).parent.parent
    runtime_dir = repo_root / "assets" / "kirikiri_native_runtime" / "x86"
    launcher_src = runtime_dir / "kirikiri_native_launcher.exe"
    hook_src = runtime_dir / "kirikiri_native_hook.dll"
    old_launcher = launcher_src.read_bytes() if launcher_src.exists() else None
    old_hook = hook_src.read_bytes() if hook_src.exists() else None
    try:
        runtime_dir.mkdir(parents=True, exist_ok=True)
        launcher_src.write_bytes(b"kirikiri-launcher")
        hook_src.write_bytes(b"kirikiri-hook")
        with tempfile.TemporaryDirectory() as tmp:
            game_dir = Path(tmp) / "Game"
            game_dir.mkdir()
            exe = game_dir / "krkr.exe"
            _pe_x86(exe)
            calls = []

            class DummyProc:
                pid = 1234
                returncode = 0

                def poll(self):
                    return None

                def wait(self):
                    return 0

                def terminate(self):
                    return None

            def fake_popen(cmd, cwd=None, **kwargs):
                calls.append((cmd, cwd, kwargs))
                return DummyProc()

            monkeypatch.setattr("core.launcher.sys.platform", "win32")
            monkeypatch.setattr("core.launcher.subprocess.CREATE_NO_WINDOW", 0x08000000, raising=False)
            monkeypatch.setattr("core.launcher._hidden_startupinfo", lambda: "hidden-startup")
            monkeypatch.setattr("core.launcher.subprocess.Popen", fake_popen)

            assert launch_kirikiri_native_runtime(game_dir, _Engine(exe), wait=False) is True

            flattened = " ".join(" ".join(map(str, call[0])) for call in calls).lower()
            assert "cmd.exe" not in flattened
            assert "powershell" not in flattened
            assert calls[0][0][1:3] == ["-m", "core.translation_overlay_window"]
            assert Path(calls[1][0][0]).name == "kirikiri_native_launcher.exe"
            assert calls[1][0][1] == str(exe)
            assert "--wait" in calls[1][0]
            assert calls[1][2]["creationflags"] == 0x08000000
            assert calls[1][2]["startupinfo"] == "hidden-startup"
            assert calls[1][2]["env"]["KIRIKIRI_CAPTURE_HOOKS"] == "zx,embed,z2,kr2"
            assert calls[1][2]["env"]["KIRIKIRI_ENABLE_EMBED_TEXT_REPLACE"] == "1"
            assert calls[1][2]["env"]["KIRIKIRI_EMBED_WAIT_MS"] == "6000"
    finally:
        if old_launcher is None:
            launcher_src.unlink(missing_ok=True)
        else:
            launcher_src.write_bytes(old_launcher)
        if old_hook is None:
            hook_src.unlink(missing_ok=True)
        else:
            hook_src.write_bytes(old_hook)


def test_kirikiri_gui_realtime_launch_uses_packaged_overlay_entrypoint(monkeypatch):
    repo_root = Path(__file__).parent.parent
    runtime_dir = repo_root / "assets" / "kirikiri_native_runtime" / "x86"
    launcher_src = runtime_dir / "kirikiri_native_launcher.exe"
    hook_src = runtime_dir / "kirikiri_native_hook.dll"
    old_launcher = launcher_src.read_bytes() if launcher_src.exists() else None
    old_hook = hook_src.read_bytes() if hook_src.exists() else None
    try:
        runtime_dir.mkdir(parents=True, exist_ok=True)
        launcher_src.write_bytes(b"kirikiri-launcher")
        hook_src.write_bytes(b"kirikiri-hook")
        with tempfile.TemporaryDirectory() as tmp:
            game_dir = Path(tmp) / "Game"
            game_dir.mkdir()
            exe = game_dir / "krkr.exe"
            _pe_x86(exe)
            package_dir = Path(tmp) / "Package"
            package_dir.mkdir()
            packaged_exe = package_dir / "EngAixt.exe"
            packaged_exe.write_bytes(b"MZ")
            calls = []

            monkeypatch.setattr("core.launcher.sys.platform", "win32")
            monkeypatch.setattr("core.launcher.sys.executable", str(packaged_exe))
            monkeypatch.setattr("core.launcher.sys.frozen", True, raising=False)

            portable_launcher = create_kirikiri_native_launcher(game_dir, _Engine(exe))
            assert portable_launcher is not None
            encoded_line = next(line for line in portable_launcher.read_text(encoding="utf-8").splitlines() if "-EncodedCommand" in line)
            encoded = encoded_line.split("-EncodedCommand", 1)[1].strip()
            script = base64.b64decode(encoded).decode("utf-16le")
            assert "$OverlayArgPrefix = @('--overlay-window')" in script
            assert "core.translation_overlay_window" not in script

            class DummyProc:
                pid = 1234
                returncode = 0

                def poll(self):
                    return None

                def wait(self):
                    return 0

                def terminate(self):
                    return None

            def fake_popen(cmd, cwd=None, **kwargs):
                calls.append((cmd, cwd, kwargs))
                return DummyProc()

            monkeypatch.setattr("core.launcher.subprocess.CREATE_NO_WINDOW", 0x08000000, raising=False)
            monkeypatch.setattr("core.launcher._hidden_startupinfo", lambda: "hidden-startup")
            monkeypatch.setattr("core.launcher.subprocess.Popen", fake_popen)

            assert launch_kirikiri_native_runtime(game_dir, _Engine(exe), wait=False) is True

            overlay_cmd, overlay_cwd, overlay_kwargs = calls[0]
            assert overlay_cmd[:2] == [str(packaged_exe.resolve()), "--overlay-window"]
            assert overlay_cmd[2:] == [game_dir.name, exe.name, str(exe)]
            assert overlay_cwd == str(package_dir.resolve())
            assert Path(calls[1][0][0]).name == "kirikiri_native_launcher.exe"
            assert calls[1][2]["creationflags"] == 0x08000000
            assert calls[1][2]["startupinfo"] == "hidden-startup"
            assert overlay_kwargs["creationflags"] == 0x08000000
    finally:
        if old_launcher is None:
            launcher_src.unlink(missing_ok=True)
        else:
            launcher_src.write_bytes(old_launcher)
        if old_hook is None:
            hook_src.unlink(missing_ok=True)
        else:
            hook_src.write_bytes(old_hook)


def test_kirikiri_launcher_prefers_krkrpatch_loader_for_patchstream_when_available():
    repo_root = Path(__file__).parent.parent
    runtime_dir = repo_root / "assets" / "kirikiri_native_runtime" / "x86"
    launcher_src = runtime_dir / "kirikiri_native_launcher.exe"
    hook_src = runtime_dir / "kirikiri_native_hook.dll"
    old_launcher = launcher_src.read_bytes() if launcher_src.exists() else None
    old_hook = hook_src.read_bytes() if hook_src.exists() else None
    try:
        runtime_dir.mkdir(parents=True, exist_ok=True)
        launcher_src.write_bytes(b"kirikiri-launcher")
        hook_src.write_bytes(b"kirikiri-hook")
        with tempfile.TemporaryDirectory() as tmp:
            game_dir = Path(tmp) / "Game"
            game_dir.mkdir()
            exe = game_dir / "krkr.exe"
            _pe_x86(exe)
            (game_dir / "KrkrPatchLoader.exe").write_bytes(b"MZ")
            (game_dir / "KrkrPatch.dll").write_bytes(b"dll")
            (game_dir / "KrkrPatch.json").write_text("{}", encoding="utf-8")
            meta = game_dir / "_translation_meta"
            meta.mkdir()
            (meta / "kirikiri_patch.xp3").write_bytes(b"XP3")
            (meta / "kirikiri_patch_manifest.txt").write_text("intro.ks\n", encoding="utf-8")
            (meta / "kirikiri_patch_diagnostics.json").write_text(
                json.dumps({"root_patch": False, "needs_patch_bridge": True}),
                encoding="utf-8",
            )

            launcher = create_kirikiri_native_launcher(game_dir, _Engine(exe))

            assert launcher is not None
            assert launcher.parent == game_dir
            assert launcher.suffix == ".bat"
            content = launcher.read_text(encoding="utf-8")
            assert "chcp 65001" in content
            assert "EncodedCommand" in content
            import base64
            encoded_line = next(line for line in content.splitlines() if "-EncodedCommand" in line)
            encoded = encoded_line.split("-EncodedCommand", 1)[1].strip()
            script = base64.b64decode(encoded).decode("utf-16le")
            assert "kirikiri_native_launcher.exe" in script
            assert "KrkrPatchLoader.exe" in script
            assert "$UsePatchBridgeLoader = $true" in script
            assert "$psi.FileName = $PatchBridgeLoader" in script
            assert "$psi.Arguments = ''" in script
            assert "$env:KIRIKIRI_NATIVE_HOOK_PROFILE = 'patchstream'" in script
            assert "$UseOverlay = $false" in script
            assert "function HasVisibleGameWindow([int]$ProcessId)" in script
            assert "function HasVisibleGameWindow([int]$Pid)" not in script
            assert "--wait" in script
            assert "frida" not in script.lower()
    finally:
        if old_launcher is None:
            launcher_src.unlink(missing_ok=True)
        else:
            launcher_src.write_bytes(old_launcher)
        if old_hook is None:
            hook_src.unlink(missing_ok=True)
        else:
            hook_src.write_bytes(old_hook)


def test_kirikiri_native_launcher_waits_for_hook_ready_before_resume():
    source = (
        Path(__file__).parent.parent
        / "native"
        / "kirikiri_runtime"
        / "kirikiri_native_launcher.cpp"
    ).read_text(encoding="utf-8")

    assert "CREATE_SUSPENDED" in source
    assert "EngAixt_KiriKiriHookReady_" in source
    assert "CreateEventW" in source
    assert "WaitForSingleObject(readyEvent, 15000)" in source
    assert source.index("WaitForSingleObject(readyEvent, 15000)") < source.index("ResumeThread(pi.hThread)")
    assert "KiriKiri hook initialization timed out before the game was resumed." in source


def test_kirikiri_launcher_does_not_fallback_to_krkrpatch_by_default_when_native_missing():
    repo_root = Path(__file__).parent.parent
    runtime_dir = repo_root / "assets" / "kirikiri_native_runtime" / "x86"
    launcher_src = runtime_dir / "kirikiri_native_launcher.exe"
    hook_src = runtime_dir / "kirikiri_native_hook.dll"
    old_launcher = launcher_src.read_bytes() if launcher_src.exists() else None
    old_hook = hook_src.read_bytes() if hook_src.exists() else None
    launcher_src.unlink(missing_ok=True)
    hook_src.unlink(missing_ok=True)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            game_dir = Path(tmp) / "Game"
            game_dir.mkdir()
            exe = game_dir / "krkr.exe"
            _pe_x86(exe)
            (game_dir / "KrkrPatchLoader.exe").write_bytes(b"MZ")
            (game_dir / "KrkrPatch.dll").write_bytes(b"dll")
            (game_dir / "KrkrPatch.json").write_text("{}", encoding="utf-8")

            launcher = create_kirikiri_native_launcher(game_dir, _Engine(exe))

            assert launcher is None
    finally:
        if old_launcher is not None:
            runtime_dir.mkdir(parents=True, exist_ok=True)
            launcher_src.write_bytes(old_launcher)
        if old_hook is not None:
            runtime_dir.mkdir(parents=True, exist_ok=True)
            hook_src.write_bytes(old_hook)


def test_kirikiri_launcher_does_not_fallback_to_legacy_krkrpatch(monkeypatch):
    repo_root = Path(__file__).parent.parent
    runtime_dir = repo_root / "assets" / "kirikiri_native_runtime" / "x86"
    launcher_src = runtime_dir / "kirikiri_native_launcher.exe"
    hook_src = runtime_dir / "kirikiri_native_hook.dll"
    old_launcher = launcher_src.read_bytes() if launcher_src.exists() else None
    old_hook = hook_src.read_bytes() if hook_src.exists() else None
    launcher_src.unlink(missing_ok=True)
    hook_src.unlink(missing_ok=True)
    monkeypatch.setattr("core.launcher.get_config", lambda: Config(kirikiri_enable_static_patch=True))
    try:
        with tempfile.TemporaryDirectory() as tmp:
            game_dir = Path(tmp) / "Game"
            game_dir.mkdir()
            exe = game_dir / "krkr.exe"
            _pe_x86(exe)
            (game_dir / "KrkrPatchLoader.exe").write_bytes(b"MZ")
            (game_dir / "KrkrPatch.dll").write_bytes(b"dll")
            (game_dir / "KrkrPatch.json").write_text("{}", encoding="utf-8")

            launcher = create_kirikiri_native_launcher(game_dir, _Engine(exe))

            assert launcher is None
    finally:
        if old_launcher is not None:
            runtime_dir.mkdir(parents=True, exist_ok=True)
            launcher_src.write_bytes(old_launcher)
        if old_hook is not None:
            runtime_dir.mkdir(parents=True, exist_ok=True)
            hook_src.write_bytes(old_hook)


def test_kirikiri_launcher_ignores_krkrpatch_when_root_patch_is_enough():
    repo_root = Path(__file__).parent.parent
    runtime_dir = repo_root / "assets" / "kirikiri_native_runtime" / "x86"
    launcher_src = runtime_dir / "kirikiri_native_launcher.exe"
    hook_src = runtime_dir / "kirikiri_native_hook.dll"
    old_launcher = launcher_src.read_bytes() if launcher_src.exists() else None
    old_hook = hook_src.read_bytes() if hook_src.exists() else None
    try:
        runtime_dir.mkdir(parents=True, exist_ok=True)
        launcher_src.write_bytes(b"kirikiri-launcher")
        hook_src.write_bytes(b"kirikiri-hook")
        with tempfile.TemporaryDirectory() as tmp:
            game_dir = Path(tmp) / "Game"
            game_dir.mkdir()
            exe = game_dir / "krkr.exe"
            _pe_x86(exe)
            (game_dir / "KrkrPatchLoader.exe").write_bytes(b"MZ")
            (game_dir / "KrkrPatch.dll").write_bytes(b"dll")
            (game_dir / "KrkrPatch.json").write_text("{}", encoding="utf-8")
            meta = game_dir / "_translation_meta"
            meta.mkdir()
            (meta / "kirikiri_patch_diagnostics.json").write_text(
                json.dumps({"root_patch": True, "needs_patch_bridge": False}),
                encoding="utf-8",
            )
            (game_dir / "patch.xp3").write_bytes(b"XP3\r\n \n\x1a\x8bg\x01")

            launcher = create_kirikiri_native_launcher(game_dir, _Engine(exe))

            assert launcher == game_dir / "启动汉化版.bat"
            content = launcher.read_text(encoding="utf-8")
            encoded_line = next(line for line in content.splitlines() if "-EncodedCommand" in line)
            encoded = encoded_line.split("-EncodedCommand", 1)[1].strip()
            script = base64.b64decode(encoded).decode("utf-16le")
            assert "KrkrPatchLoader.exe" in script
            assert "$UsePatchBridgeLoader = $false" in script
            assert "kirikiri_native_launcher.exe" in script
            assert "$env:KIRIKIRI_NATIVE_HOOK_PROFILE = 'display'" in script
            assert "$env:KIRIKIRI_ENABLE_EMBED_TEXT_REPLACE = '0'" in script
            assert "$env:KIRIKIRI_EMBED_WAIT_MS = '0'" in script
            assert "$env:KIRIKIRI_CAPTURE_HOOKS = 'zx,embed,z2,kr2'" in script
            assert "$UseOverlay = $false" in script
            assert "--wait" in script
    finally:
        if old_launcher is None:
            launcher_src.unlink(missing_ok=True)
        else:
            launcher_src.write_bytes(old_launcher)
        if old_hook is None:
            hook_src.unlink(missing_ok=True)
        else:
            hook_src.write_bytes(old_hook)
        try:
            if runtime_dir.exists() and not any(runtime_dir.iterdir()):
                runtime_dir.rmdir()
        except OSError:
            pass


if __name__ == "__main__":
    test_manual_exe_overrides_engine_and_auto_selection()
    test_bgi_realtime_preload_accepts_checkpoint_json()
    test_bgi_auto_uses_native_launcher_without_overriding_manual_choice()
    test_bgi_preload_prefers_game_metadata_dir()
    test_bgi_preload_uses_game_checkpoint_before_workspace_checkpoint()
    test_bgi_launcher_prefers_bgi_exe_over_helper_exes()
    test_checkpoint_is_mirrored_to_game_metadata_dir()
    test_overlay_checkpoint_prefers_game_metadata_dir()
    test_bgi_hook_launcher_is_written_with_preload_and_sjis_ext()
    test_bgi_native_map_is_base64_tsv()
    test_bgi_launcher_prefers_native_runtime_when_built()
    test_bgi_native_launcher_keeps_non_ascii_exe_name_out_of_bat_codepage()
    test_kirikiri_launcher_prefers_native_runtime_when_built()
    print("Launcher tests passed")
