from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from config import get_config
from core.exe_selector import find_main_exe
from core.path_resolver import resolve_game_path
from core.resources import app_root, resource_path
from utils.logger import info, warning, debug


def launch_game(game_path: Path, engine: object | None = None,
               block: bool = False) -> bool:
    """启动游戏。返回 True 表示成功启动。

    block=True 时会等待游戏进程退出再返回（用于需要维持代理服务器的场景）。
    """
    game_path = Path(resolve_game_path(game_path))
    exe_path = _select_launch_exe(game_path, engine)

    if not exe_path:
        warning("未找到可执行文件，无法启动游戏")
        return False

    info(f"正在启动游戏: {exe_path}")
    try:
        if sys.platform == "win32":
            proc = subprocess.Popen(
                [str(exe_path)],
                cwd=str(exe_path.parent),
                shell=True,
            )
        else:
            proc = subprocess.Popen(
                [str(exe_path)],
                cwd=str(exe_path.parent),
                start_new_session=True,
            )
        if block:
            info("等待游戏退出...")
            proc.wait()
            info("游戏已退出")
        return True
    except Exception as e:
        warning(f"启动失败: {e}")
        return False


def _hidden_startupinfo():
    if sys.platform != "win32":
        return None
    try:
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        return startupinfo
    except Exception:
        return None


def launch_translated_launcher(launcher: Path, cwd: Path | None = None, hidden: bool = True):
    """Start a generated translated-game launcher.

    GUI/pipeline launches should not flash a cmd.exe window just because the
    portable user-facing launcher is a .bat file.
    """
    launcher = Path(launcher)
    workdir = Path(cwd) if cwd else launcher.parent
    if sys.platform == "win32":
        creationflags = 0
        startupinfo = None
        if hidden:
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            startupinfo = _hidden_startupinfo()
        suffix = launcher.suffix.lower()
        if suffix in {".bat", ".cmd"}:
            comspec = os.environ.get("ComSpec") or "cmd.exe"
            cmd = [comspec, "/d", "/c", "call", str(launcher)]
        elif suffix == ".ps1":
            cmd = [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(launcher),
            ]
        elif suffix in {".vbs", ".vbe"}:
            cmd = ["wscript.exe", str(launcher)]
        else:
            cmd = [str(launcher)]
        return subprocess.Popen(
            cmd,
            cwd=str(workdir),
            creationflags=creationflags,
            startupinfo=startupinfo,
        )
    return subprocess.Popen([str(launcher)], cwd=str(workdir), start_new_session=True)


def launch_with_injector(game_path: Path, injector_type: str = "xunity",
                         engine: object | None = None,
                         checkpoint: Path | None = None) -> bool:
    """使用注入器启动游戏（用于实时翻译 Hook）。

    xunity 模式下会阻塞等待游戏退出，期间保持翻译代理服务器存活。
    """
    game_path = Path(resolve_game_path(game_path))
    info(f"使用 {injector_type} 注入器启动游戏...")

    if injector_type == "xunity":
        return _launch_with_xunity(game_path, engine)

    if injector_type == "frida":
        if getattr(engine, "name", "") == "bgi":
            return _launch_with_bgi_frida_tunnel(game_path, engine, checkpoint)
        if getattr(engine, "name", "") == "kirikiri":
            return _launch_with_kirikiri_frida_capture(game_path, engine, checkpoint)
        if _is_godot_engine(engine):
            return _launch_with_godot_display_hook(game_path, engine, checkpoint)
        if getattr(engine, "name", "") == "gamemaker":
            return _launch_with_gamemaker_frida_overlay(game_path, engine, checkpoint)
        if getattr(engine, "name", "") == "rpgmaker" and _is_rpgmaker_mkxp(game_path):
            return _launch_with_rpgmaker_mkxp_frida(game_path, engine, checkpoint)
        return launch_game(game_path, engine)

    return launch_game(game_path, engine)


def _launch_with_bgi_frida_tunnel(game_path: Path, engine: object | None,
                                  checkpoint: Path | None = None) -> bool:
    """Launch BGI with the SJIS tunnel rendering hook."""
    game_path = Path(resolve_game_path(game_path))
    game_dir = game_path if game_path.is_dir() else game_path.parent
    exe_path = _select_bgi_launch_exe(game_path, engine)
    if not exe_path:
        warning("BGI Frida 注入失败：未找到可执行文件")
        return False

    script = resource_path("frida", "run_bgi_realtime.py")
    if not script.exists():
        warning(f"BGI Frida 注入失败：缺少脚本 {script}")
        return False

    checkpoint_path = _resolve_bgi_preload(game_path, checkpoint)
    cmd = [_console_python_executable(), str(script), str(exe_path), "--no-live-translate"]
    if checkpoint_path:
        cmd.extend(["--preload", str(checkpoint_path)])
    sjis_ext = game_dir / "sjis_ext.bin"
    if sjis_ext.exists():
        cmd.extend(["--sjis-ext", str(sjis_ext)])
        cmd.append("--ansi-render-replace")
        cmd.append("--font-create-hook")
    else:
        warning("BGI Frida 注入：未找到 sjis_ext.bin，非 CP932 中文可能无法正常显示")
    font_scale = getattr(get_config(), "bgi_font_height_scale", 0.95)
    cmd.extend(["--font-height-scale", str(font_scale)])
    hook_log = game_dir / "_translation_meta" / "bgi_hook_runtime.log"
    cmd.extend(["--log-file", str(hook_log)])

    info(f"使用 BGI SJIS tunnel 渲染 hook (font scale={font_scale})")
    try:
        proc = subprocess.Popen(cmd, cwd=str(app_root()))
        proc.wait()
        return proc.returncode == 0
    except Exception as e:
        warning(f"BGI Frida 注入启动失败: {e}")
        return False


def create_bgi_hook_launcher(game_path: Path, engine: object | None = None,
                             checkpoint: Path | None = None) -> Path | None:
    """Create a portable launcher that always starts BGI with the hook."""
    game_path = Path(resolve_game_path(game_path))
    game_dir = game_path if game_path.is_dir() else game_path.parent
    exe_path = _select_bgi_launch_exe(game_path, engine)
    if not exe_path:
        warning("BGI launcher not created: no executable found")
        return None

    exe_rel = _relative_to_dir(exe_path, game_dir)
    if not exe_rel:
        warning(f"BGI launcher not created: executable is outside game directory: {exe_path}")
        return None

    checkpoint_path = _resolve_bgi_preload(game_path, checkpoint)
    native_launcher = _prepare_native_bgi_runtime(game_dir, checkpoint_path, exe_path)
    if native_launcher:
        launcher = _write_native_bgi_launcher(game_dir, exe_rel, native_launcher)
        info(f"BGI native hook launcher written: {launcher}")
        return launcher

    hook_dir = _prepare_portable_bgi_hook(game_dir, checkpoint_path)
    if not hook_dir:
        return None

    font_scale = getattr(get_config(), "bgi_font_height_scale", 0.95)
    ps_lines = [
        "$ErrorActionPreference = 'Stop'",
        "$env:PYTHONUTF8 = '1'",
        "$env:PYTHONIOENCODING = 'utf-8'",
        "$GameDir = [Environment]::GetEnvironmentVariable('BGI_GAME_DIR', 'Process')",
        "if ([string]::IsNullOrWhiteSpace($GameDir)) { $GameDir = (Get-Location).Path }",
        "$GameDir = [System.IO.Path]::GetFullPath($GameDir)",
        "$MetaDir = Join-Path $GameDir '_translation_meta'",
        "$HookDir = Join-Path $MetaDir 'bgi_hook'",
        "$Runner = Join-Path $HookDir 'run_bgi_realtime.py'",
        f"$Exe = Join-Path $GameDir {_ps_quote(exe_rel)}",
        "$LogFile = Join-Path $MetaDir 'bgi_hook_runtime.log'",
        "$SjisExt = Join-Path $GameDir 'sjis_ext.bin'",
        "function Fail([string]$Message) { Write-Host $Message -ForegroundColor Red; Read-Host 'Press Enter to close'; exit 1 }",
        "if (-not (Test-Path -LiteralPath $Runner)) { Fail 'Missing portable hook runner: _translation_meta\\bgi_hook\\run_bgi_realtime.py' }",
        "if (-not (Test-Path -LiteralPath $Exe)) { Fail ('Game executable not found: ' + $Exe) }",
        "$Preload = $null",
        "$PreloadCandidates = @(",
        "    (Join-Path $MetaDir 'bgi_runtime_map_cached.json'),",
        "    (Join-Path $MetaDir 'translation_checkpoint.json'),",
        "    (Join-Path $GameDir 'translation_checkpoint.json')",
        ")",
        "foreach ($Candidate in $PreloadCandidates) { if (Test-Path -LiteralPath $Candidate) { $Preload = $Candidate; break } }",
        "if (-not $Preload) { Fail 'Translation preload JSON not found. Re-run the translator or keep _translation_meta with the game folder.' }",
        "function Test-Python([string]$Command, [string[]]$PrefixArgs) {",
        "    try { & $Command @PrefixArgs -c 'import sys' > $null 2> $null; return $LASTEXITCODE -eq 0 } catch { return $false }",
        "}",
        "$PythonCmd = $null",
        "$PythonPrefix = @()",
        "$PortablePythonCandidates = @(",
        "    (Join-Path $MetaDir 'python\\python.exe'),",
        "    (Join-Path $GameDir 'python\\python.exe')",
        ")",
        "foreach ($Candidate in $PortablePythonCandidates) { if (Test-Path -LiteralPath $Candidate) { $PythonCmd = $Candidate; break } }",
        "if (-not $PythonCmd -and (Get-Command py -ErrorAction SilentlyContinue)) { if (Test-Python 'py' @('-3')) { $PythonCmd = 'py'; $PythonPrefix = @('-3') } }",
        "if (-not $PythonCmd -and (Get-Command python -ErrorAction SilentlyContinue)) { if (Test-Python 'python' @()) { $PythonCmd = 'python' } }",
        "if (-not $PythonCmd) { Fail 'Python 3 was not found. Install Python and run: pip install frida' }",
        "& $PythonCmd @PythonPrefix -c 'import frida' > $null 2> $null",
        "if ($LASTEXITCODE -ne 0) { Fail 'Python package frida was not found. Install it with: pip install frida' }",
        "$RunnerArgs = @()",
        "$RunnerArgs += $PythonPrefix",
        "$RunnerArgs += @($Runner, $Exe, '--no-live-translate', '--font-height-scale', " + _ps_quote(str(font_scale)) + ", '--log-file', $LogFile, '--preload', $Preload)",
        "if (Test-Path -LiteralPath $SjisExt) { $RunnerArgs += @('--sjis-ext', $SjisExt, '--ansi-render-replace', '--font-create-hook') }",
        "Set-Location -LiteralPath $GameDir",
        "& $PythonCmd @RunnerArgs",
        "if ($LASTEXITCODE -ne 0) { Read-Host 'BGI hook exited with an error. Press Enter to close' }",
    ]
    encoded = base64.b64encode("\r\n".join(ps_lines).encode("utf-16le")).decode("ascii")
    launcher = game_dir / "启动汉化版.bat"
    launcher.write_text(
        "@echo off\r\n"
        "setlocal\r\n"
        "set \"BGI_GAME_DIR=%~dp0\"\r\n"
        "powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand "
        f"{encoded}\r\n"
        "endlocal\r\n",
        encoding="utf-8",
    )
    info(f"BGI hook launcher written: {launcher}")
    return launcher


def _write_native_bgi_launcher(game_dir: Path, exe_rel: str, native_launcher: Path) -> Path:
    launcher = game_dir / "启动汉化版.bat"
    native_rel = _relative_to_dir(native_launcher, game_dir) or str(native_launcher)
    ps_lines = [
        "$ErrorActionPreference = 'Stop'",
        "$GameDir = [Environment]::GetEnvironmentVariable('GAME_DIR', 'Process')",
        "if ([string]::IsNullOrWhiteSpace($GameDir)) { $GameDir = (Get-Location).Path }",
        "$GameDir = [System.IO.Path]::GetFullPath($GameDir)",
        f"$NativeLauncher = Join-Path $GameDir {_ps_quote(native_rel)}",
        f"$Exe = Join-Path $GameDir {_ps_quote(exe_rel)}",
        "function Fail([string]$Message) { Write-Host $Message -ForegroundColor Red; Read-Host 'Press Enter to close'; exit 1 }",
        "if (-not (Test-Path -LiteralPath $NativeLauncher)) { Fail ('BGI native launcher not found: ' + $NativeLauncher) }",
        "if (-not (Test-Path -LiteralPath $Exe)) { Fail ('Game executable not found: ' + $Exe) }",
        "$psi = New-Object System.Diagnostics.ProcessStartInfo",
        "$psi.FileName = $NativeLauncher",
        "$psi.WorkingDirectory = $GameDir",
        "$psi.UseShellExecute = $false",
        "$psi.Arguments = '\"' + $Exe.Replace('\"', '\\\"') + '\"'",
        "[System.Diagnostics.Process]::Start($psi) | Out-Null",
    ]
    encoded = base64.b64encode("\r\n".join(ps_lines).encode("utf-16le")).decode("ascii")
    launcher.write_text(
        "@echo off\r\n"
        "setlocal\r\n"
        "set \"GAME_DIR=%~dp0\"\r\n"
        "cd /d \"%GAME_DIR%\"\r\n"
        "powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand "
        f"{encoded}\r\n"
        "endlocal\r\n",
        encoding="ascii",
    )
    return launcher


def create_kirikiri_native_launcher(game_path: Path, engine: object | None = None,
                                    checkpoint: Path | None = None) -> Path | None:
    """Create a portable KiriKiri launcher for runtime display replacement."""
    game_path = Path(resolve_game_path(game_path))
    game_dir = game_path if game_path.is_dir() else game_path.parent
    exe_path = _select_launch_exe(game_path, engine)
    if not exe_path:
        warning("KiriKiri launcher not created: no executable found")
        return None
    exe_rel = _relative_to_dir(exe_path, game_dir)
    if not exe_rel:
        warning(f"KiriKiri launcher not created: executable is outside game directory: {exe_path}")
        return None

    if not _is_pe_x86(exe_path):
        warning(f"KiriKiri native launcher skipped: {exe_path.name} is not a 32-bit PE executable")
        if not bool(getattr(get_config(), "kirikiri_enable_static_patch", False)):
            return None
        krkrpatch_loader = game_dir / "KrkrPatchLoader.exe"
        krkrpatch_config = game_dir / "KrkrPatch.json"
        krkrpatch_dll = game_dir / "KrkrPatch.dll"
        if (
            krkrpatch_loader.exists()
            and krkrpatch_config.exists()
            and krkrpatch_dll.exists()
            and _kirikiri_needs_patch_bridge(game_dir)
        ):
            launcher = _write_krkrpatch_launcher(game_dir, exe_rel, krkrpatch_loader)
            info(f"KiriKiri KrkrPatch launcher written: {launcher}")
            return launcher
        return None

    native_launcher = _prepare_native_kirikiri_runtime(game_dir, checkpoint)
    if not native_launcher:
        if not bool(getattr(get_config(), "kirikiri_enable_static_patch", False)):
            return None
        krkrpatch_loader = game_dir / "KrkrPatchLoader.exe"
        krkrpatch_config = game_dir / "KrkrPatch.json"
        krkrpatch_dll = game_dir / "KrkrPatch.dll"
        if (
            krkrpatch_loader.exists()
            and krkrpatch_config.exists()
            and krkrpatch_dll.exists()
            and _kirikiri_needs_patch_bridge(game_dir)
        ):
            launcher = _write_krkrpatch_launcher(game_dir, exe_rel, krkrpatch_loader)
            info(f"KiriKiri KrkrPatch launcher written: {launcher}")
            return launcher
        return None
    launcher = _write_native_kirikiri_launcher(game_dir, exe_rel, native_launcher, engine=engine)
    info(f"KiriKiri 汉化启动器已生成: {launcher}")
    return launcher


def create_godot_display_hook_launcher(game_path: Path, engine: object | None = None,
                                       checkpoint: Path | None = None) -> Path | None:
    """Create a launcher that starts Godot with the local translation display hook."""
    game_path = Path(resolve_game_path(game_path))
    game_dir = game_path if game_path.is_dir() else game_path.parent
    exe_path = _select_launch_exe(game_path, engine)
    if not exe_path:
        warning("Godot hook launcher not created: no executable found")
        return None

    exe_rel = _relative_to_dir(exe_path, game_dir)
    if not exe_rel:
        warning(f"Godot hook launcher not created: executable is outside game directory: {exe_path}")
        return None

    hook_dir = _prepare_portable_godot_hook(game_dir, checkpoint)
    if not hook_dir:
        return None

    launcher = game_dir / "启动汉化版.bat"
    runner_rel = _relative_to_dir(hook_dir / "run_godot_display_hook.py", game_dir) or str(hook_dir / "run_godot_display_hook.py")
    checkpoint_rel = "_translation_meta\\translation_checkpoint.json"
    ps_lines = [
        "$ErrorActionPreference = 'Stop'",
        "$env:PYTHONUTF8 = '1'",
        "$env:PYTHONIOENCODING = 'utf-8'",
        "$GameDir = [Environment]::GetEnvironmentVariable('GODOT_GAME_DIR', 'Process')",
        "if ([string]::IsNullOrWhiteSpace($GameDir)) { $GameDir = (Get-Location).Path }",
        "$GameDir = [System.IO.Path]::GetFullPath($GameDir)",
        "$MetaDir = Join-Path $GameDir '_translation_meta'",
        f"$Runner = Join-Path $GameDir {_ps_quote(runner_rel)}",
        f"$Exe = Join-Path $GameDir {_ps_quote(exe_rel)}",
        f"$Checkpoint = Join-Path $GameDir {_ps_quote(checkpoint_rel)}",
        "$LogFile = Join-Path $MetaDir 'godot_hook_runtime.log'",
        "$CaptureFile = Join-Path $MetaDir 'godot_runtime_capture.jsonl'",
        "function Fail([string]$Message) { Write-Host $Message -ForegroundColor Red; Read-Host 'Press Enter to close'; exit 1 }",
        "if (-not (Test-Path -LiteralPath $Runner)) { Fail 'Missing Godot hook runner: _translation_meta\\godot_hook\\run_godot_display_hook.py' }",
        "if (-not (Test-Path -LiteralPath $Exe)) { Fail ('Game executable not found: ' + $Exe) }",
        "if (-not (Test-Path -LiteralPath $Checkpoint)) { Fail 'Translation checkpoint not found. Re-run the translator first.' }",
        "function Test-Python([string]$Command, [string[]]$PrefixArgs) {",
        "    try { & $Command @PrefixArgs -c 'import sys' > $null 2> $null; return $LASTEXITCODE -eq 0 } catch { return $false }",
        "}",
        "$PythonCmd = $null",
        "$PythonPrefix = @()",
        "$PortablePythonCandidates = @(",
        "    (Join-Path $MetaDir 'python\\python.exe'),",
        "    (Join-Path $GameDir 'python\\python.exe')",
        ")",
        "foreach ($Candidate in $PortablePythonCandidates) { if (Test-Path -LiteralPath $Candidate) { $PythonCmd = $Candidate; break } }",
        "if (-not $PythonCmd -and (Get-Command py -ErrorAction SilentlyContinue)) { if (Test-Python 'py' @('-3')) { $PythonCmd = 'py'; $PythonPrefix = @('-3') } }",
        "if (-not $PythonCmd -and (Get-Command python -ErrorAction SilentlyContinue)) { if (Test-Python 'python' @()) { $PythonCmd = 'python' } }",
        "if (-not $PythonCmd) { Fail 'Python 3 was not found. Install Python and run: pip install frida' }",
        "& $PythonCmd @PythonPrefix -c 'import frida' > $null 2> $null",
        "if ($LASTEXITCODE -ne 0) { Fail 'Python package frida was not found. Install it with: pip install frida' }",
        "$RunnerArgs = @()",
        "$RunnerArgs += $PythonPrefix",
        "$RunnerArgs += @($Runner, $Exe, '--preload', $Checkpoint, '--log-file', $LogFile, '--capture', $CaptureFile, '--no-live-translate')",
        "Set-Location -LiteralPath $GameDir",
        "& $PythonCmd @RunnerArgs",
        "if ($LASTEXITCODE -ne 0) { Read-Host 'Godot hook exited with an error. Press Enter to close' }",
    ]
    encoded = base64.b64encode("\r\n".join(ps_lines).encode("utf-16le")).decode("ascii")
    launcher.write_text(
        "@echo off\r\n"
        "setlocal\r\n"
        "set \"GODOT_GAME_DIR=%~dp0\"\r\n"
        "powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand "
        f"{encoded}\r\n"
        "endlocal\r\n",
        encoding="utf-8",
    )
    info(f"Godot display hook launcher written: {launcher}")
    return launcher


def _kirikiri_needs_patch_bridge(game_dir: Path) -> bool:
    diag_path = game_dir / "_translation_meta" / "kirikiri_patch_diagnostics.json"
    if not diag_path.exists():
        return True
    try:
        data = json.loads(diag_path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        debug(f"KiriKiri patch diagnostics read failed: {exc}")
        return True
    if bool(data.get("root_patch")) and not bool(data.get("needs_patch_bridge")):
        return False
    return True


def _kirikiri_native_hook_profile(game_dir: Path) -> str:
    """Use the static stream bridge only for a complete protected patch set."""
    meta_dir = game_dir / "_translation_meta"
    manifest = meta_dir / "kirikiri_patch_manifest.txt"
    try:
        has_manifest = manifest.is_file() and bool(manifest.read_text(encoding="utf-8-sig").strip())
    except OSError:
        has_manifest = False
    has_patch = (
        (meta_dir / "kirikiri_patch").is_dir()
        or (meta_dir / "kirikiri_patch.xp3").is_file()
        or (game_dir / "patch.xp3").is_file()
    )
    if has_manifest and has_patch and _kirikiri_needs_patch_bridge(game_dir):
        return "patchstream"
    return "display"


def prepare_kirikiri_native_runtime(game_path: Path) -> Path | None:
    game_path = Path(resolve_game_path(game_path))
    game_dir = game_path if game_path.is_dir() else game_path.parent
    return _prepare_native_kirikiri_runtime(game_dir)


def launch_kirikiri_native_runtime(
    game_path: Path,
    engine: object | None = None,
    checkpoint: Path | None = None,
    *,
    hidden: bool = True,
    wait: bool = False,
) -> bool:
    """Launch KiriKiri through the native display hook without bat/powershell.

    The portable ``启动汉化版.bat`` is still generated for users who want a
    self-contained game folder. GUI realtime launch uses this direct path so it
    does not flash cmd.exe or powershell.exe windows.
    """
    game_path = Path(resolve_game_path(game_path))
    game_dir = game_path if game_path.is_dir() else game_path.parent
    exe_path = _select_launch_exe(game_path, engine)
    if not exe_path:
        warning("KiriKiri native realtime launch failed: no executable found")
        return False
    if not _is_pe_x86(exe_path):
        warning(f"KiriKiri native realtime launch skipped: {exe_path.name} is not a 32-bit PE executable")
        return False

    checkpoint_path = _resolve_checkpoint_for_overlay(game_path, checkpoint)
    native_launcher = _prepare_native_kirikiri_runtime(game_dir, checkpoint_path)
    if not native_launcher:
        return False

    meta_dir = game_dir / "_translation_meta"
    meta_dir.mkdir(parents=True, exist_ok=True)
    start_log = meta_dir / "kirikiri_overlay_start.log"

    def log_start(message: str) -> None:
        try:
            start_log.open("a", encoding="utf-8").write(
                time.strftime("%Y-%m-%d %H:%M:%S ") + message + "\n"
            )
        except Exception:
            pass

    creationflags = 0
    startupinfo = None
    if sys.platform == "win32" and hidden:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        startupinfo = _hidden_startupinfo()

    overlay_cmd, overlay_cwd, overlay_env = _overlay_window_command(
        game_dir.name,
        exe_path.name,
        str(exe_path),
    )

    def start_overlay_window(reason: str) -> subprocess.Popen | None:
        try:
            started = subprocess.Popen(
                overlay_cmd,
                cwd=overlay_cwd,
                env=overlay_env,
                creationflags=creationflags,
            )
            log_start(
                "overlay_started "
                f"reason={reason} pid={getattr(started, 'pid', '')} "
                f"cwd={overlay_cwd} cmd={overlay_cmd!r} exe={exe_path}"
            )
            return started
        except Exception as exc:
            log_start(f"overlay_start_failed reason={reason}: {exc}")
            return None

    def monitor_overlay_window(native_proc: subprocess.Popen, initial_overlay: subprocess.Popen | None) -> None:
        overlay = initial_overlay
        restart_count = 0
        while native_proc.poll() is None:
            if overlay is None or overlay.poll() is not None:
                if overlay is not None:
                    log_start(f"overlay_exited exit_code={overlay.returncode}; restarting")
                restart_count += 1
                overlay = start_overlay_window(f"restart-{restart_count}")
                if overlay is None:
                    time.sleep(2.0)
                    continue
            time.sleep(0.75)
        if overlay is not None and overlay.poll() is None:
            try:
                log_start("overlay_terminate_after_game_exit")
                overlay.terminate()
            except Exception as exc:
                log_start(f"overlay_terminate_failed: {exc}")

    overlay_proc = start_overlay_window("initial")

    native_env = os.environ.copy()
    native_env["KIRIKIRI_NATIVE_HOOK_PROFILE"] = "display"

    # 从配置读取嵌入状态
    from config import get_config
    cfg = get_config()
    embed_enabled = bool(getattr(cfg, "kirikiri_overlay_embedded", True))
    native_env["KIRIKIRI_ENABLE_EMBED_TEXT_REPLACE"] = "1" if embed_enabled else "0"
    native_env["KIRIKIRI_EMBED_WAIT_MS"] = "6000"

    # DLL 内部已实现短路优先级策略（参照 LunaTranslator），不再需要外部白名单控制。
    # 按优先级尝试：zx → embed → z2/kr2，装成功一个就跳过剩余的，避免多 hook 冲突。
    capture_hooks = "zx,embed,z2,kr2"

    native_env["KIRIKIRI_LUNA_CAPTURE_HOOKS"] = capture_hooks
    try:
        proc = subprocess.Popen(
            [str(native_launcher), str(exe_path), "--wait"],
            cwd=str(game_dir),
            env=native_env,
            creationflags=creationflags,
            startupinfo=startupinfo,
        )
        info(f"KiriKiri native realtime launched without shell script: {exe_path}")
        monitor_thread = threading.Thread(
            target=monitor_overlay_window,
            args=(proc, overlay_proc),
            daemon=True,
        )
        monitor_thread.start()
        if wait:
            proc.wait()
            monitor_thread.join(timeout=3.0)
            return proc.returncode == 0
        return True
    except Exception as exc:
        warning(f"KiriKiri native realtime launch failed: {exc}")
        log_start(f"native_launch_failed: {exc}")
        try:
            if overlay_proc and overlay_proc.poll() is None:
                overlay_proc.terminate()
        except Exception:
            pass
        return False


def _prepare_native_kirikiri_runtime(game_dir: Path, checkpoint_path: Path | None = None) -> Path | None:
    source_dir = resource_path("assets", "kirikiri_native_runtime", "x86")
    launcher_src = source_dir / "kirikiri_native_launcher.exe"
    hook_src = source_dir / "kirikiri_native_hook.dll"
    if not launcher_src.exists() or not hook_src.exists():
        warning("KiriKiri native runtime not found; cannot create display hook launcher")
        return None

    meta_dir = game_dir / "_translation_meta"
    meta_dir.mkdir(parents=True, exist_ok=True)
    launcher_dst = meta_dir / "kirikiri_native_launcher.exe"
    hook_dst = meta_dir / "kirikiri_native_hook.dll"
    try:
        _copy_or_reuse_locked_runtime(launcher_src, launcher_dst)
        _copy_or_reuse_locked_runtime(hook_src, hook_dst)
        checkpoint = checkpoint_path or meta_dir / "translation_checkpoint.json"
        if checkpoint and checkpoint.exists():
            _write_kirikiri_native_map(checkpoint, meta_dir / "kirikiri_native_map.tsv")
    except Exception as exc:
        warning(f"KiriKiri native runtime not deployed: {exc}")
        return None
    return launcher_dst


def _copy_or_reuse_locked_runtime(src: Path, dst: Path) -> None:
    for attempt in range(4):
        try:
            shutil.copy2(src, dst)
            return
        except PermissionError:
            if _runtime_files_match(src, dst):
                return
            if attempt == 3:
                raise PermissionError(
                    f"KiriKiri runtime is locked and outdated: {dst}. "
                    "Close the game before launching again."
                )
            time.sleep(0.5)


def _runtime_files_match(src: Path, dst: Path) -> bool:
    try:
        if not src.is_file() or not dst.is_file() or src.stat().st_size != dst.stat().st_size:
            return False
        return _sha256_file(src) == _sha256_file(dst)
    except OSError:
        return False


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_native_kirikiri_launcher(game_dir: Path, exe_rel: str, native_launcher: Path, engine: object | None = None) -> Path:
    launcher = game_dir / "启动汉化版.bat"
    native_rel = _relative_to_dir(native_launcher, game_dir) or str(native_launcher)
    overlay_exe, overlay_prefix_args, overlay_cwd, overlay_needs_pythonpath = _overlay_window_launch_spec()
    no_window_timeout = max(10, min(300, int(getattr(get_config(), "kirikiri_no_window_timeout_seconds", 45) or 45)))
    capture_hooks_for_bat = "zx,embed,z2,kr2"
    hook_profile = _kirikiri_native_hook_profile(game_dir)
    use_overlay = hook_profile == "display"
    ps_lines = [
        "$ErrorActionPreference = 'Stop'",
        "$GameDir = [Environment]::GetEnvironmentVariable('KIRIKIRI_GAME_DIR', 'Process')",
        "if ([string]::IsNullOrWhiteSpace($GameDir)) { $GameDir = (Get-Location).Path }",
        "$GameDir = [System.IO.Path]::GetFullPath($GameDir)",
        "Set-Location -LiteralPath $GameDir",
        f"$NativeLauncher = Join-Path $GameDir {_ps_quote(native_rel)}",
        f"$Exe = Join-Path $GameDir {_ps_quote(exe_rel)}",
        f"$OverlayExe = {_ps_quote(overlay_exe)}",
        f"$OverlayCwd = {_ps_quote(overlay_cwd)}",
        f"$OverlayArgPrefix = {_ps_array(overlay_prefix_args)}",
        f"$OverlayNeedsPythonPath = {'$true' if overlay_needs_pythonpath else '$false'}",
        f"$NoWindowTimeout = {no_window_timeout}",
        f"$UseOverlay = {'$true' if use_overlay else '$false'}",
        "$OverlayStartLog = Join-Path $GameDir '_translation_meta\\kirikiri_overlay_start.log'",
        "$GameName = Split-Path -Leaf $GameDir",
        "$GameExeName = [System.IO.Path]::GetFileName($Exe)",
        "function Fail([string]$Message) { Write-Host $Message -ForegroundColor Red; Read-Host 'Press Enter to close'; exit 1 }",
        "function QuoteArg([string]$Value) { '\"' + $Value.Replace('\"', '\\\"') + '\"' }",
        "function LogOverlay([string]$Message) {",
        "  try {",
        "    $dir = Split-Path -Parent $OverlayStartLog",
        "    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }",
        "    Add-Content -LiteralPath $OverlayStartLog -Encoding UTF8 -Value ((Get-Date).ToString('yyyy-MM-dd HH:mm:ss') + ' ' + $Message)",
        "  } catch {}",
        "}",
        "function StartOverlayWindow {",
        "  if (-not (Test-Path -LiteralPath $OverlayExe)) { LogOverlay ('overlay_exe_missing: ' + $OverlayExe); return $null }",
        "  if (-not (Test-Path -LiteralPath $OverlayCwd)) { LogOverlay ('overlay_cwd_missing: ' + $OverlayCwd); return $null }",
        "  try {",
        "    $overlayArgs = @() + $OverlayArgPrefix + @($GameName, $GameExeName, $Exe)",
        "    $overlayPsi = [System.Diagnostics.ProcessStartInfo]::new()",
        "    $overlayPsi.FileName = $OverlayExe",
        "    $overlayPsi.Arguments = ($overlayArgs | ForEach-Object { QuoteArg $_ }) -join ' '",
        "    $overlayPsi.WorkingDirectory = $OverlayCwd",
        "    $overlayPsi.UseShellExecute = $false",
        "    if ($OverlayNeedsPythonPath) { $overlayPsi.EnvironmentVariables['PYTHONPATH'] = $OverlayCwd }",
        "    $started = [System.Diagnostics.Process]::Start($overlayPsi)",
        "    if ($started) { LogOverlay ('overlay_started pid=' + $started.Id + ' overlay=' + $OverlayExe + ' args=' + $overlayPsi.Arguments + ' exe=' + $Exe); return $started }",
        "    LogOverlay 'overlay_start_returned_null'",
        "  } catch {",
        "    LogOverlay ('overlay_start_failed: ' + $_.Exception.Message)",
        "  }",
        "  return $null",
        "}",
        "function GetGameProcessInfo {",
        "  Get-CimInstance Win32_Process -Filter (\"name='\" + $GameExeName.Replace(\"'\", \"''\") + \"'\") | Where-Object { $_.ExecutablePath -and ([System.IO.Path]::GetFullPath($_.ExecutablePath) -ieq $Exe) } | Select-Object -First 1",
        "}",
        "function HasVisibleGameWindow([int]$Pid) {",
        "  $p = Get-Process -Id $Pid -ErrorAction SilentlyContinue",
        "  if (-not $p) { return $false }",
        "  $p.Refresh()",
        "  return ([IntPtr]$p.MainWindowHandle -ne [IntPtr]::Zero)",
        "}",
        "if (-not (Test-Path -LiteralPath $NativeLauncher)) { Fail ('KiriKiri native launcher not found: ' + $NativeLauncher) }",
        "if (-not (Test-Path -LiteralPath $Exe)) { Fail ('Game executable not found: ' + $Exe) }",
        f"$env:KIRIKIRI_NATIVE_HOOK_PROFILE = '{hook_profile}'",
        f"$env:KIRIKIRI_ENABLE_EMBED_TEXT_REPLACE = '{'1' if use_overlay else '0'}'",
        f"$env:KIRIKIRI_EMBED_WAIT_MS = '{'6000' if use_overlay else '0'}'",
        f"$env:KIRIKIRI_LUNA_CAPTURE_HOOKS = '{capture_hooks_for_bat}'",
        "$overlayProc = $null",
        "$overlayRestartCount = 0",
        "$overlayExitLogged = $false",
        "$QuotedExe = '\"' + $Exe.Replace('\"', '\\\"') + '\"'",
        "$psi = [System.Diagnostics.ProcessStartInfo]::new()",
        "$psi.FileName = $NativeLauncher",
        "$psi.Arguments = $QuotedExe + ' --wait'",
        "$psi.WorkingDirectory = $GameDir",
        "try {",
        "  $proc = [System.Diagnostics.Process]::Start($psi)",
        "  if (-not $proc) { Fail 'Failed to start KiriKiri native launcher.' }",
        "  $deadline = [DateTime]::UtcNow.AddSeconds($NoWindowTimeout)",
        "  $gameProcInfo = $null",
        "  $windowSeen = $false",
        "  while (-not $proc.HasExited) {",
        "    $currentGame = GetGameProcessInfo",
        "    if ($currentGame) {",
        "      $gameProcInfo = $currentGame",
        "      if ($UseOverlay -and ($null -eq $overlayProc -or $overlayProc.HasExited) -and $overlayRestartCount -lt 3) {",
        "        if ($overlayProc -and $overlayProc.HasExited -and -not $overlayExitLogged) { LogOverlay ('overlay_exited early exit_code=' + $overlayProc.ExitCode); $overlayExitLogged = $true }",
        "        $overlayRestartCount += 1",
        "        LogOverlay ('overlay_restart attempt=' + $overlayRestartCount)",
        "        $overlayProc = StartOverlayWindow",
        "      }",
        "      if (HasVisibleGameWindow ([int]$currentGame.ProcessId)) { $windowSeen = $true }",
        "    }",
        "    if ($gameProcInfo -and -not $windowSeen -and [DateTime]::UtcNow -gt $deadline) {",
        "      Write-Host ('KiriKiri game process started without a visible window for ' + $NoWindowTimeout + ' seconds; cleaning it up.') -ForegroundColor Yellow",
        "      try { Stop-Process -Id ([int]$gameProcInfo.ProcessId) -Force -ErrorAction SilentlyContinue } catch {}",
        "      break",
        "    }",
        "    Start-Sleep -Milliseconds 500",
        "  }",
        "  $proc.WaitForExit()",
        "} finally {",
        "  LogOverlay 'launcher_script_ending; overlay_self_managed=1'",
        "}",
    ]
    encoded = base64.b64encode("\r\n".join(ps_lines).encode("utf-16le")).decode("ascii")
    launcher.write_text(
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        "setlocal\r\n"
        "set \"KIRIKIRI_GAME_DIR=%~dp0\"\r\n"
        "powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand "
        f"{encoded}\r\n"
        "endlocal\r\n",
        encoding="utf-8",
    )
    return launcher


def _write_krkrpatch_launcher(game_dir: Path, exe_rel: str, krkrpatch_loader: Path) -> Path:
    launcher = game_dir / "启动汉化版.bat"
    loader_rel = _relative_to_dir(krkrpatch_loader, game_dir) or str(krkrpatch_loader)
    ps_lines = [
        "$ErrorActionPreference = 'Stop'",
        "$GameDir = [Environment]::GetEnvironmentVariable('KIRIKIRI_GAME_DIR', 'Process')",
        "if ([string]::IsNullOrWhiteSpace($GameDir)) { $GameDir = (Get-Location).Path }",
        "$GameDir = [System.IO.Path]::GetFullPath($GameDir)",
        "Set-Location -LiteralPath $GameDir",
        f"$Loader = Join-Path $GameDir {_ps_quote(loader_rel)}",
        f"$Exe = Join-Path $GameDir {_ps_quote(exe_rel)}",
        "$Config = Join-Path $GameDir 'KrkrPatch.json'",
        "$Dll = Join-Path $GameDir 'KrkrPatch.dll'",
        "function Fail([string]$Message) { Write-Host $Message -ForegroundColor Red; Read-Host 'Press Enter to close'; exit 1 }",
        "if (-not (Test-Path -LiteralPath $Loader)) { Fail ('KrkrPatchLoader not found: ' + $Loader) }",
        "if (-not (Test-Path -LiteralPath $Dll)) { Fail ('KrkrPatch.dll not found: ' + $Dll) }",
        "if (-not (Test-Path -LiteralPath $Config)) { Fail ('KrkrPatch.json not found: ' + $Config) }",
        "if (-not (Test-Path -LiteralPath $Exe)) { Fail ('Game executable not found: ' + $Exe) }",
        "$psi = [System.Diagnostics.ProcessStartInfo]::new()",
        "$psi.FileName = $Loader",
        "$psi.WorkingDirectory = $GameDir",
        "$proc = [System.Diagnostics.Process]::Start($psi)",
        "if (-not $proc) { Fail 'Failed to start KrkrPatchLoader.' }",
    ]
    encoded = base64.b64encode("\r\n".join(ps_lines).encode("utf-16le")).decode("ascii")
    launcher.write_text(
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        "setlocal\r\n"
        "powershell -NoProfile -ExecutionPolicy Bypass -EncodedCommand "
        f"{encoded}\r\n"
        "endlocal\r\n",
        encoding="utf-8",
    )
    return launcher


def _relative_to_dir(path: Path, root: Path) -> str | None:
    try:
        rel = path.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    return str(rel).replace("/", "\\")


def _is_pe_x86(path: Path) -> bool:
    return _pe_machine(path) == 0x14C


def _is_pe_x64(path: Path) -> bool:
    return _pe_machine(path) == 0x8664


def _pe_machine(path: Path) -> int | None:
    try:
        with path.open("rb") as f:
            header = f.read(0x1000)
            if len(header) < 0x40 or header[:2] != b"MZ":
                return None
            pe_offset = int.from_bytes(header[0x3C:0x40], "little")
            if pe_offset + 6 > len(header):
                f.seek(pe_offset)
                sig = f.read(6)
            else:
                sig = header[pe_offset:pe_offset + 6]
            if sig[:4] != b"PE\0\0":
                return None
            return int.from_bytes(sig[4:6], "little")
    except Exception:
        return None


def _prepare_native_bgi_runtime(game_dir: Path, checkpoint_path: Path | None, exe_path: Path | None = None) -> Path | None:
    arch = "x64" if exe_path and _is_pe_x64(exe_path) else "x86"
    source_dir = resource_path("assets", "bgi_native_runtime", arch)
    launcher_src = source_dir / "bgi_native_launcher.exe"
    hook_src = source_dir / "bgi_native_hook.dll"
    if not launcher_src.exists() or not hook_src.exists():
        if arch == "x64":
            warning("BGI x64 native runtime not found; x64 game cannot use the x86 display hook")
        return None
    if not _is_valid_native_bgi_runtime(launcher_src, hook_src, arch):
        warning(
            f"BGI {arch} native runtime is invalid or incomplete; "
            "falling back to the portable BGI launcher"
        )
        return None

    meta_dir = game_dir / "_translation_meta"
    meta_dir.mkdir(parents=True, exist_ok=True)
    launcher_dst = meta_dir / "bgi_native_launcher.exe"
    hook_dst = meta_dir / "bgi_native_hook.dll"
    try:
        shutil.copy2(launcher_src, launcher_dst)
        shutil.copy2(hook_src, hook_dst)
        if checkpoint_path:
            _write_bgi_native_map(checkpoint_path, meta_dir / "bgi_native_map.tsv")
        else:
            (meta_dir / "bgi_native_map.tsv").write_text("# base64_utf8_original\tbase64_utf8_translated\n", encoding="ascii")
        (meta_dir / "bgi_native_runtime_arch.txt").write_text(arch + "\n", encoding="ascii")
    except Exception as exc:
        warning(f"BGI native runtime not deployed, falling back to Python/Frida launcher: {exc}")
        return None
    return launcher_dst


def _is_valid_native_bgi_runtime(launcher_path: Path, hook_path: Path, arch: str) -> bool:
    """Reject truncated or non-PE runtime assets before copying them to a game."""
    expected_machine = 0x8664 if arch == "x64" else 0x014C
    return all(
        _read_pe_machine(path) == expected_machine
        for path in (launcher_path, hook_path)
    )


def _read_pe_machine(path: Path) -> int | None:
    try:
        with path.open("rb") as stream:
            dos_header = stream.read(0x40)
            if len(dos_header) < 0x40 or dos_header[:2] != b"MZ":
                return None
            pe_offset = int.from_bytes(dos_header[0x3C:0x40], "little")
            stream.seek(pe_offset)
            pe_header = stream.read(6)
            if len(pe_header) != 6 or pe_header[:4] != b"PE\0\0":
                return None
            return int.from_bytes(pe_header[4:6], "little")
    except (OSError, ValueError):
        return None


def _write_bgi_native_map(checkpoint_path: Path, out_path: Path) -> int:
    translations = _load_translation_pairs(checkpoint_path)
    lines = ["# base64_utf8_original\tbase64_utf8_translated"]
    count = 0
    for original, translated in translations.items():
        if not original or not translated or original == translated:
            continue
        lines.append(f"{_b64_utf8(original)}\t{_b64_utf8(translated)}")
        count += 1
    out_path.write_text("\n".join(lines) + "\n", encoding="ascii")
    return count


def _write_kirikiri_native_map(checkpoint_path: Path, out_path: Path) -> int:
    translations = _load_translation_pairs(checkpoint_path)
    lines = ["# base64_utf8_original\tbase64_utf8_translated"]
    emitted: set[tuple[str, str]] = set()
    count = 0
    for original, translated in translations.items():
        if not _is_runtime_display_pair(original, translated):
            continue
        for src, dst in _kirikiri_display_map_variants(original, translated):
            key = (src, dst)
            if key in emitted:
                continue
            emitted.add(key)
            lines.append(f"{_b64_utf8(src)}\t{_b64_utf8(dst)}")
            count += 1
    out_path.write_text("\n".join(lines) + "\n", encoding="ascii")
    return count


def _is_runtime_display_pair(original: str, translated: str) -> bool:
    if not original or not translated or original == translated:
        return False
    stripped = original.strip()
    if not stripped:
        return False
    if stripped.startswith(("*", ";", "//", "/*")):
        return False
    if stripped.startswith("@") and not _kirikiri_command_visible_parts(stripped):
        return False
    if stripped.startswith("[") and not _kirikiri_bracket_text_can_be_visible(stripped):
        return False
    if stripped.startswith(".") and not any("\u3040" <= ch <= "\u30ff" or "\u4e00" <= ch <= "\u9fff" for ch in stripped):
        return False
    if len(original) > 1000 or len(translated) > 1500:
        return False
    return True


_KIRIKIRI_ATTR_RE = re.compile(r"\b(name|word|text)\s*=\s*(['\"])(.*?)\2", re.I | re.S)
_KIRIKIRI_LINE_TAG_RE = re.compile(r"\[(?:r|p|er|cm|ct|current|clearfix|clearhistory)\]", re.I)
_KIRIKIRI_INLINE_TAG_RE = re.compile(r"\[[^\]]+\]")
_KIRIKIRI_QUOTE_OPEN_RE = re.compile(r"\[>>\]")
_KIRIKIRI_QUOTE_CLOSE_RE = re.compile(r"\[<<\]")


def _kirikiri_command_attrs(text: str) -> dict[str, str]:
    return {match.group(1).lower(): match.group(3) for match in _KIRIKIRI_ATTR_RE.finditer(str(text or ""))}


def _kirikiri_command_visible_parts(text: str) -> tuple[str, str] | None:
    attrs = _kirikiri_command_attrs(text)
    word = (attrs.get("word") or attrs.get("text") or "").strip()
    if not word:
        return None
    name = (attrs.get("name") or "").strip()
    name = re.sub(r"（.*?）", "", name).strip()
    return name, word


def _kirikiri_bracket_text_can_be_visible(text: str) -> bool:
    stripped = str(text or "").strip()
    lowered = stripped.lower()
    return (
        stripped.startswith("[>>]")
        or stripped.startswith("[地]")
        or lowered.startswith("[text]")
        or _kirikiri_command_visible_parts(stripped) is not None
    )


def _kirikiri_display_map_variants(original: str, translated: str) -> list[tuple[str, str]]:
    variants: list[tuple[str, str]] = []

    def add(src: str, dst: str) -> None:
        src = (src or "").strip()
        dst = (dst or "").strip()
        if src and dst and src != dst:
            variants.append((src, dst))

    original_command = _kirikiri_command_visible_parts(original)
    if original_command:
        original_name, original_word = original_command
        translated_command = _kirikiri_command_visible_parts(translated)
        translated_word = translated_command[1] if translated_command else str(translated or "").strip()
        translated_name = translated_command[0] if translated_command else original_name
        add(original_word, translated_word)
        if original_name:
            add(f"{original_name}「{original_word}」", f"{translated_name or original_name}「{translated_word}」")
        return variants

    add(original, translated)
    # \u6309\u89c4\u8303\u5316\u7b56\u7565\u5bf9\u9f50 src/dst\uff1a\u540c\u4e00\u7b56\u7565\u7684 key \u624d\u914d\u5bf9\uff0c\u907f\u514d min(len) \u4e0b\u6807\u9519\u4f4d\u4e32\u4f4d\u3002
    visible_srcs = _kirikiri_visible_text_variants(original)
    visible_dsts = _kirikiri_visible_text_variants(translated)
    for style in _KIRIKIRI_VISIBLE_STYLES:
        src = visible_srcs.get(style)
        dst = visible_dsts.get(style)
        if src and dst:
            add(src, dst)
    return variants


# \u8fd0\u884c\u65f6 KagParserVisibleText \u7684\u89c4\u8303\u5316\u7b56\u7565\u987a\u5e8f\uff0c\u9759\u6001\u4fa7\u5fc5\u987b\u9010\u4e00\u590d\u523b\u3002
_KIRIKIRI_VISIBLE_STYLES = ("runtime_like", "line_joined", "compact")


def _kirikiri_visible_text_variants(text: str) -> dict[str, str]:
    """\u590d\u523b\u8fd0\u884c\u65f6 KagParserVisibleText \u7684\u89c4\u8303\u5316\uff0c\u751f\u6210\u53ef\u4e0e\u8fd0\u884c\u65f6\u5bf9\u9f50\u7684 key \u96c6\u5408\u3002

    \u5173\u952e\u70b9\uff1a\u8fd0\u884c\u65f6\u628a [>>]/[<<] \u8f6c\u6210 \u300c\u300d \u5f15\u53f7\u540e\u518d\u5220\u5176\u4f59\u6807\u7b7e\uff0c\u9759\u6001\u4fa7\u82e5\u76f4\u63a5\u5220
    [>>] \u4f1a\u5bfc\u81f4\u5bf9\u8bdd\u6587\u672c\u5168\u90e8\u4e22\u5f15\u53f7\u3001\u65e0\u6cd5\u5339\u914d\u3002\u8fd9\u91cc\u5148\u6ce8\u5165\u5f15\u53f7\u518d\u5220\u6807\u7b7e\u3002
    \u8fd4\u56de {\u7b56\u7565\u540d: key}\uff0c\u4f9b _kirikiri_display_map_variants \u6309\u7b56\u7565\u5bf9\u9f50 src/dst\u3002
    """
    value = str(text or "")
    if not value:
        return {}
    # 1) \u590d\u523b\u8fd0\u884c\u65f6\uff1a[>>] -> \u300c\uff0c[<<] -> \u300d\uff08\u5728\u5220\u6807\u7b7e\u4e4b\u524d\uff09
    value = _KIRIKIRI_QUOTE_OPEN_RE.sub("\u300c", value)
    value = _KIRIKIRI_QUOTE_CLOSE_RE.sub("\u300d", value)
    with_newlines = _KIRIKIRI_LINE_TAG_RE.sub("\n", value)
    without_tags = _KIRIKIRI_INLINE_TAG_RE.sub("", with_newlines)
    collapsed_spaces = re.sub(r"[ \t\u3000]+", " ", without_tags)
    # runtime_like\uff1a\u6298\u53e0\u6362\u884c\u4e24\u4fa7\u7a7a\u683c\u4f46\u4e0d\u9010\u884c strip \u5185\u5bb9\uff0c\u8d34\u8fd1\u8fd0\u884c\u65f6 CollapseWideSpaces
    runtime_like = re.sub(r"[ \t\u3000]*\n[ \t\u3000]*", "\n", collapsed_spaces).strip()
    # line_joined\uff1a\u9010\u884c strip \u540e join\uff08\u4fdd\u7559\u65e7\u884c\u4e3a\uff0c\u8986\u76d6\u591a\u884c\u6d88\u606f\uff09
    line_joined = "\n".join(part.strip() for part in collapsed_spaces.splitlines() if part.strip())
    # compact\uff1a\u5220\u9664\u6240\u6709\u7a7a\u767d\uff08\u5728\u5f15\u53f7\u6ce8\u5165\u4e4b\u540e\u8ba1\u7b97\uff0c\u4e0e\u8fd0\u884c\u65f6 CompactWideText \u5bf9\u9f50\uff09
    compact = re.sub(r"\s+", "", without_tags)
    out: dict[str, str] = {}
    for style, candidate in (
        ("runtime_like", runtime_like),
        ("line_joined", line_joined),
        ("compact", compact),
    ):
        candidate = candidate.strip()
        if candidate:
            out[style] = candidate
    return out


def _b64_utf8(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def _load_translation_pairs(path: Path) -> dict[str, str]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    pairs: dict[str, str] = {}
    if isinstance(data, dict) and isinstance(data.get("translations"), dict):
        raw_items = data["translations"].items()
        for original, translated in raw_items:
            original_s = str(original or "")
            translated_s = str(translated or "")
            if original_s and translated_s:
                pairs[original_s] = translated_s
    elif isinstance(data, dict) and isinstance(data.get("items"), list):
        for item in data["items"]:
            if not isinstance(item, dict):
                continue
            original_s = str(item.get("original") or "")
            translated_s = str(item.get("translated") or "")
            if original_s and translated_s:
                pairs[original_s] = translated_s
    elif isinstance(data, dict):
        for original, translated in data.items():
            if isinstance(translated, (dict, list)):
                continue
            original_s = str(original or "")
            translated_s = str(translated or "")
            if original_s and translated_s:
                pairs[original_s] = translated_s
    elif isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            original_s = str(item.get("original") or "")
            translated_s = str(item.get("translated") or "")
            if original_s and translated_s:
                pairs[original_s] = translated_s
    return pairs


def _prepare_portable_bgi_hook(game_dir: Path, checkpoint_path: Path | None) -> Path | None:
    frida_dir = resource_path("frida")
    meta_dir = game_dir / "_translation_meta"
    hook_dir = meta_dir / "bgi_hook"
    try:
        hook_dir.mkdir(parents=True, exist_ok=True)
        for name in ("run_bgi_realtime.py", "bgi_realtime_hook.js"):
            src = frida_dir / name
            if not src.exists():
                warning(f"BGI launcher not created: missing {src}")
                return None
            shutil.copy2(src, hook_dir / name)
        if checkpoint_path and checkpoint_path.exists() and not _relative_to_dir(checkpoint_path, game_dir):
            target_name = checkpoint_path.name
            if target_name not in {"bgi_runtime_map_cached.json", "translation_checkpoint.json"}:
                target_name = "translation_checkpoint.json"
            shutil.copy2(checkpoint_path, meta_dir / target_name)
    except Exception as exc:
        warning(f"BGI launcher not created: failed to prepare portable hook files: {exc}")
        return None
    return hook_dir


def _ps_quote(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _console_python_executable() -> str:
    """Prefer python.exe for user-facing hook launchers.

    The GUI may run under pythonw.exe, which hides stderr/stdout. BGI hook
    failures then become invisible and the launcher can sit in an error prompt
    with no useful diagnostics.
    """
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        console = exe.with_name("python.exe")
        if console.exists():
            return str(console)
    return str(exe)


def _window_python_executable() -> str:
    """Prefer pythonw.exe for GUI helper windows when available."""
    exe = Path(sys.executable)
    if exe.name.lower() == "python.exe":
        windowed = exe.with_name("pythonw.exe")
        if windowed.exists():
            return str(windowed)
    return str(exe)


def _overlay_window_launch_spec() -> tuple[str, list[str], str, bool]:
    """Return executable, fixed args, cwd, and whether PYTHONPATH is needed.

    In source runs the overlay is a Python module. In PyInstaller builds
    ``sys.executable`` is EngAixt.exe, so ``EngAixt.exe -m ...`` exits before
    Tk can create the subtitle window. Packaged builds must re-enter the app
    through the private ``--overlay-window`` command instead.
    """
    if getattr(sys, "frozen", False):
        exe = str(Path(sys.executable).resolve())
        return exe, ["--overlay-window"], str(Path(exe).parent), False
    return _window_python_executable(), ["-m", "core.translation_overlay_window"], str(app_root()), True


def _overlay_window_command(
    game_name: str,
    game_exe: str,
    game_exe_path: str,
    shm_name: str = "",
) -> tuple[list[str], str, dict[str, str]]:
    exe, prefix_args, cwd, needs_pythonpath = _overlay_window_launch_spec()
    env = os.environ.copy()
    if needs_pythonpath:
        root = str(app_root())
        env["PYTHONPATH"] = (
            root if not env.get("PYTHONPATH")
            else root + os.pathsep + env["PYTHONPATH"]
        )
    args = [exe, *prefix_args, game_name, game_exe, game_exe_path]
    if shm_name:
        args.append(shm_name)
    return args, cwd, env


def _ps_array(values: list[str]) -> str:
    return "@(" + ", ".join(_ps_quote(value) for value in values) + ")"


def _resolve_bgi_preload(game_path: Path, checkpoint: Path | None) -> Path | None:
    game_dir = game_path if game_path.is_dir() else game_path.parent
    for rel in (
        Path("_translation_meta") / "bgi_runtime_map_cached.json",
        Path("_translation_meta") / "translation_checkpoint.json",
        Path("translation_checkpoint.json"),
    ):
        candidate = game_dir / rel
        if candidate.exists():
            return candidate
    if checkpoint and checkpoint.exists():
        return checkpoint
    return _resolve_checkpoint_for_overlay(game_path, checkpoint)


def _is_godot_engine(engine: object | None) -> bool:
    return str(getattr(engine, "name", "") or "").lower() in {"godot", "godot_pck", "godot_frida"}


def _select_godot_runtime_pck(game_dir: Path) -> Path | None:
    candidates: list[Path] = []
    for sub in ["", "contents", "game", "data", "pack"]:
        search = game_dir / sub if sub else game_dir
        if not search.is_dir():
            continue
        candidates.extend(
            p for p in search.glob("*.pck")
            if not p.name.lower().startswith("translation_")
        )
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_size)


def _ensure_godot_runtime_cjk_fonts(game_dir: Path) -> bool:
    """Patch only Godot fontdata for runtime display hooks.

    Godot runtime translation replaces text at the display layer. If the game
    ships Latin-only fonts, the hook succeeds but Chinese glyphs render blank.
    This keeps the text path dynamic while making the packaged fonts CJK-capable.
    """
    pck_path = _select_godot_runtime_pck(game_dir)
    if not pck_path:
        return False

    meta_dir = game_dir / "_translation_meta"
    marker = meta_dir / "godot_cjk_font_patch.json"
    backup = pck_path.with_suffix(pck_path.suffix + ".pre_tool")
    source = backup if backup.exists() else pck_path

    try:
        from core.font_replacer import (
            collect_runtime_fontdata_paths_from_pck,
            get_bundled_fontdata,
            replace_fonts_in_pck,
        )

        fontdata = get_bundled_fontdata()
        if not fontdata:
            warning("Godot CJK 字体补丁跳过：缺少内嵌字体资源")
            return False

        source_stat = source.stat()
        if marker.exists():
            try:
                state = json.loads(marker.read_text(encoding="utf-8"))
            except Exception:
                state = {}
            if (
                state.get("pck") == str(pck_path.resolve())
                and state.get("source_size") == source_stat.st_size
                and state.get("source_mtime_ns") == source_stat.st_mtime_ns
                and state.get("fontdata_size") == len(fontdata)
                and state.get("patched_size") == pck_path.stat().st_size
            ):
                return True

        meta_dir.mkdir(parents=True, exist_ok=True)
        if not backup.exists():
            shutil.copy2(pck_path, backup)
            source = backup
            source_stat = source.stat()
            info(f"Godot 原始 PCK 已备份: {backup.name}")

        temp_output = meta_dir / (pck_path.name + ".cjk_font.tmp")
        if temp_output.exists():
            temp_output.unlink()

        selected_fonts = collect_runtime_fontdata_paths_from_pck(source)
        if not selected_fonts:
            warning("Godot CJK 字体补丁跳过：未识别到运行时字体引用")
            return False

        info(f"Godot runtime hook: 正在部署 CJK 字体补丁 ({len(selected_fonts)} 个运行时字体)")
        if not replace_fonts_in_pck(source, temp_output, selected_paths=selected_fonts):
            return False
        if not temp_output.exists() or temp_output.stat().st_size < 1024:
            warning("Godot CJK 字体补丁异常：输出 PCK 无效")
            return False

        shutil.move(str(temp_output), str(pck_path))
        marker.write_text(json.dumps({
            "pck": str(pck_path.resolve()),
            "source": str(source.resolve()),
            "source_size": source_stat.st_size,
            "source_mtime_ns": source_stat.st_mtime_ns,
            "fontdata_size": len(fontdata),
            "patched_size": pck_path.stat().st_size,
            "mode": "runtime_fontdata_only_for_display_hook",
            "font_count": len(selected_fonts),
            "fonts": sorted(selected_fonts),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        info(f"Godot CJK 字体补丁已部署: {pck_path.name}")
        return True
    except Exception as exc:
        warning(f"Godot CJK 字体补丁失败: {exc}")
        return False


def _prepare_portable_godot_hook(game_dir: Path, checkpoint_path: Path | None) -> Path | None:
    frida_dir = resource_path("frida")
    meta_dir = game_dir / "_translation_meta"
    hook_dir = meta_dir / "godot_hook"
    try:
        hook_dir.mkdir(parents=True, exist_ok=True)
        for name in ("run_godot_display_hook.py", "godot_display_hook.js"):
            src = frida_dir / name
            if not src.exists():
                warning(f"Godot hook launcher not created: missing {src}")
                return None
            shutil.copy2(src, hook_dir / name)
        overlay_src = resource_path("core", "runtime_overlay.py")
        if overlay_src.exists():
            shutil.copy2(overlay_src, hook_dir / "runtime_overlay.py")
        resolved_checkpoint = checkpoint_path if checkpoint_path and checkpoint_path.exists() else None
        if resolved_checkpoint is None:
            resolved_checkpoint = _resolve_checkpoint_for_overlay(game_dir, checkpoint_path)
        if resolved_checkpoint and resolved_checkpoint.exists():
            target = meta_dir / "translation_checkpoint.json"
            if resolved_checkpoint.resolve() != target.resolve():
                shutil.copy2(resolved_checkpoint, target)
        if os.environ.get("GAME_TRANSLATOR_GODOT_PATCH_CJK_FONTS") == "1":
            _ensure_godot_runtime_cjk_fonts(game_dir)
    except Exception as exc:
        warning(f"Godot hook launcher not created: failed to prepare hook files: {exc}")
        return None
    return hook_dir


def _launch_with_godot_display_hook(game_path: Path, engine: object | None,
                                    checkpoint: Path | None = None) -> bool:
    """Launch Godot with a local translation display hook.

    This is an offline display path: it preloads already translated checkpoint
    text and does not call an API for misses.
    """
    game_path = Path(resolve_game_path(game_path))
    game_dir = game_path if game_path.is_dir() else game_path.parent
    exe_path = _select_launch_exe(game_path, engine)
    if not exe_path:
        warning("Godot display hook failed: no executable found")
        return False

    hook_dir = _prepare_portable_godot_hook(game_dir, checkpoint)
    if not hook_dir:
        return False

    checkpoint_path = _resolve_checkpoint_for_overlay(game_path, checkpoint)
    if not checkpoint_path:
        warning("Godot display hook failed: translation checkpoint not found")
        return False

    script = hook_dir / "run_godot_display_hook.py"
    hook_log = game_dir / "_translation_meta" / "godot_hook_runtime.log"
    capture = game_dir / "_translation_meta" / "godot_runtime_capture.jsonl"
    cmd = [
        _console_python_executable(),
        str(script),
        str(exe_path),
        "--preload",
        str(checkpoint_path),
        "--log-file",
        str(hook_log),
        "--capture",
        str(capture),
        "--no-live-translate",
    ]

    info("使用 Godot 显示层 hook 加载本地离线译文")
    info(f"Godot 运行时译文来源: {checkpoint_path}")
    try:
        creationflags = 0
        startupinfo = None
        if sys.platform == "win32":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            startupinfo = _hidden_startupinfo()
        proc = subprocess.Popen(
            cmd,
            cwd=str(app_root()),
            creationflags=creationflags,
            startupinfo=startupinfo,
        )
        proc.wait()
        return proc.returncode == 0
    except Exception as e:
        warning(f"Godot display hook launch failed: {e}")
        return False


def _launch_with_kirikiri_frida_capture(game_path: Path, engine: object | None,
                                        checkpoint: Path | None = None) -> bool:
    """Launch KiriKiri with a local-preload/capture hook.

    This runner does not call the translation API by default. It displays
    preloaded checkpoint translations and records misses for a later offline
    batch translation pass.
    """
    game_path = Path(resolve_game_path(game_path))
    game_dir = game_path if game_path.is_dir() else game_path.parent
    exe_path = _select_launch_exe(game_path, engine)
    if not exe_path:
        warning("KiriKiri Frida hook failed: no executable found")
        return False

    script = resource_path("frida", "run_kirikiri_realtime.py")
    if not script.exists():
        warning(f"KiriKiri Frida hook failed: missing script {script}")
        return False

    meta_dir = game_dir / "_translation_meta"
    capture = meta_dir / "kirikiri_runtime_capture.jsonl"
    hook_log = meta_dir / "kirikiri_hook_runtime.log"
    checkpoint_path = _resolve_checkpoint_for_overlay(game_path, checkpoint)

    cmd = [
        _console_python_executable(),
        str(script),
        str(exe_path),
        "--capture",
        str(capture),
        "--log-file",
        str(hook_log),
        "--no-live-translate",
    ]
    if checkpoint_path:
        cmd.extend(["--preload", str(checkpoint_path)])

    info("使用 KiriKiri Frida 本地译文显示/文本捕获 hook")
    if checkpoint_path:
        info(f"KiriKiri 运行时译文来源: {checkpoint_path}")
    else:
        info("KiriKiri 暂无预加载译文，本次会捕获实际显示文本供离线批量翻译")

    try:
        proc = subprocess.Popen(cmd, cwd=str(app_root()))
        proc.wait()
        return proc.returncode == 0
    except Exception as e:
        warning(f"KiriKiri Frida hook 启动失败: {e}")
        return False


def _launch_with_gamemaker_frida_overlay(game_path: Path, engine: object | None,
                                         checkpoint: Path | None = None) -> bool:
    """Launch a GameMaker game with the D3D11/DirectWrite translation overlay."""
    game_path = Path(resolve_game_path(game_path))
    exe_path = _select_launch_exe(game_path, engine)
    if not exe_path:
        warning("GameMaker Frida 注入失败：未找到可执行文件")
        return False

    script = resource_path("frida", "run_d3d11_overlay.py")
    if not script.exists():
        warning(f"GameMaker Frida 注入失败：缺少脚本 {script}")
        return False

    checkpoint_path = _resolve_checkpoint_for_overlay(game_path, checkpoint)
    cmd = [sys.executable, str(script), str(exe_path), "--window-title", exe_path.stem]
    if checkpoint_path:
        cmd.extend(["--checkpoint", str(checkpoint_path)])

    info("使用 GameMaker D3D11/DirectWrite 运行时叠加翻译")
    if checkpoint_path:
        info(f"运行时译文来源: {checkpoint_path}")
    else:
        warning("未找到翻译检查点，将只显示内置菜单翻译样例")

    try:
        proc = subprocess.Popen(cmd, cwd=str(app_root()))
        info("等待游戏退出以保持 Frida 翻译叠加层...")
        proc.wait()
        return proc.returncode == 0
    except Exception as e:
        warning(f"GameMaker Frida 注入启动失败: {e}")
        return False


def _launch_with_rpgmaker_mkxp_frida(game_path: Path, engine: object | None,
                                     checkpoint: Path | None = None) -> bool:
    """Launch RPG Maker XP/mkxp through steamshim.exe and hook SDL_ttf text."""
    game_path = Path(resolve_game_path(game_path))
    game_dir = game_path if game_path.is_dir() else game_path.parent
    exe_path = _select_launch_exe(game_path, engine)
    if not exe_path:
        warning("RPG Maker XP/mkxp Frida 注入失败：未找到可执行文件")
        return False

    script = resource_path("frida", "run_rpgmaker_mkxp_hook.py")
    if not script.exists():
        warning(f"RPG Maker XP/mkxp Frida 注入失败：缺少脚本 {script}")
        return False

    checkpoint_path = _resolve_checkpoint_for_overlay(game_path, checkpoint)
    cmd = [sys.executable, str(script), str(exe_path)]
    if checkpoint_path:
        cmd.extend(["--checkpoint", str(checkpoint_path)])
    source_locale, target_locale = _resolve_runtime_locales(checkpoint_path)
    if target_locale:
        cmd.extend(["--target-locale", target_locale])
    if source_locale:
        cmd.extend(["--source-locale", source_locale])

    info("使用 RPG Maker XP/mkxp SDL_ttf 运行时翻译 hook")
    if checkpoint_path:
        info(f"运行时译文来源: {checkpoint_path}")
    else:
        warning("未找到翻译检查点，将只使用内置主菜单兜底译文")

    try:
        proc = subprocess.Popen(cmd, cwd=str(app_root()))
        info("等待游戏退出以保持 RPG Maker Frida hook...")
        proc.wait()
        return proc.returncode == 0
    except Exception as e:
        warning(f"RPG Maker XP/mkxp Frida 注入启动失败: {e}")
        return False


def _resolve_runtime_locales(checkpoint: Path | None) -> tuple[str | None, str | None]:
    source_locale: str | None = None
    target_locale: str | None = "zh_CN"
    if checkpoint and checkpoint.exists():
        try:
            data = json.loads(checkpoint.read_text(encoding="utf-8"))
            source_locale = _normalize_po_locale(data.get("source_lang"), default=None)
            target_locale = _normalize_po_locale(data.get("target_lang"), default="zh_CN")
        except Exception:
            pass
    return source_locale, target_locale


def _normalize_po_locale(value: object, default: str | None = None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return default
    normalized = text.replace("-", "_")
    aliases = {
        "zh": "zh_CN",
        "zh_cn": "zh_CN",
        "zh_hans": "zh_CN",
        "zh_chs": "zh_CN",
        "cn": "zh_CN",
        "jp": "ja",
        "jpn": "ja",
        "ja_jp": "ja",
    }
    return aliases.get(normalized.lower(), normalized)


def _resolve_checkpoint_for_overlay(game_path: Path, checkpoint: Path | None) -> Path | None:
    if checkpoint and checkpoint.exists():
        return checkpoint
    game_dir = game_path if game_path.is_dir() else game_path.parent
    game_checkpoint = game_dir / "_translation_meta" / "translation_checkpoint.json"
    if game_checkpoint.exists():
        return game_checkpoint
    latest = _find_latest_checkpoint_for_game(game_dir)
    return latest


def _is_rpgmaker_mkxp(game_path: Path) -> bool:
    game_dir = game_path if game_path.is_dir() else game_path.parent
    return (
        bool(list(game_dir.glob("Data/*.rxdata")))
        and (game_dir / "Languages").is_dir()
        and (
            (game_dir / "steamshim.exe").exists()
            or any(p.name.lower().startswith("x64-vcruntime") and "ruby" in p.name.lower() for p in game_dir.glob("*.dll"))
            or (game_dir / "SDL2.dll").exists()
        )
    )


def _find_latest_checkpoint_for_game(game_dir: Path, workspace_root: Path | None = None) -> Path | None:
    root = workspace_root or Path.home() / "Downloads" / ".game_translator" / "workspaces"
    if not root.exists():
        return None
    game_dir_resolved = str(game_dir.resolve()).lower()
    candidates = sorted(
        root.glob("**/translation_checkpoint.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for candidate in candidates[:50]:
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except Exception:
            continue
        source = str(data.get("source", "")).lower()
        if source and (source == game_dir_resolved or source.startswith(game_dir_resolved)):
            return candidate
    return None


def _launch_with_xunity(game_path: Path, engine: object | None = None) -> bool:
    """使用 XUnity.AutoTranslator (BepInEx) 启动 Unity 游戏。

    阻塞等待游戏退出，期间保持翻译代理服务器存活。
    游戏退出后自动停止代理服务器。
    """
    game_dir = game_path if game_path.is_dir() else game_path.parent

    if engine is None or not hasattr(engine, "ensure_translation_proxy"):
        warning("Unity 实时翻译启动被拒绝：缺少可管理翻译代理的 XUnity engine")
        return False
    if not engine.ensure_translation_proxy():
        warning("Unity 实时翻译启动被拒绝：127.0.0.1:5120 未监听")
        return False

    # 检查 BepInEx 是否已安装
    bepinex_dir = game_dir / "BepInEx"

    if not bepinex_dir.exists():
        info("BepInEx 未安装，正在配置...")
        if not _setup_bepinex(game_dir):
            warning("BepInEx 自动安装失败，请手动安装 BepInEx + XUnity.AutoTranslator")
            return launch_game(game_path, engine)

    # 使用 block=True 启动游戏，等待游戏退出
    result = launch_game(game_path, engine, block=True)

    # 游戏退出后，将本次会话积累的服务器翻译缓存同步到 XUAT 本地缓存
    # 下次启动时 XUAT 直接命中 Translation_zh.txt，无需 HTTP 往返，零延迟。
    try:
        from engines.xunity import TranslationCacheGenerator
        cache_gen = TranslationCacheGenerator(game_dir)
        synced = cache_gen.sync_from_server_cache()
        if synced > 0:
            info(f"翻译缓存已同步到本地: +{synced} 条，下次启动即时命中")
    except Exception:
        pass  # 同步失败不影响主流程

    # 游戏退出后，停止翻译代理服务器
    if engine and hasattr(engine, "_translation_proxy") and engine._translation_proxy:
        engine._translation_proxy.stop()
        info("翻译代理服务器已停止")

    return result


def _setup_bepinex(game_dir: Path) -> bool:
    """自动安装 BepInEx 到 Unity 游戏目录。"""
    import zipfile
    import urllib.request

    bepinex_url = "https://github.com/BepInEx/BepInEx/releases/download/v5.4.22/BepInEx_x64_5.4.22.0.zip"
    tools_dir = Path.home() / "Downloads" / ".game_translator" / "tools"
    tools_dir.mkdir(parents=True, exist_ok=True)

    zip_path = tools_dir / "BepInEx_x64.zip"
    try:
        info("正在下载 BepInEx...")
        urllib.request.urlretrieve(bepinex_url, str(zip_path))
    except Exception as e:
        warning(f"下载 BepInEx 失败: {e}")
        return False

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(str(game_dir))
        info("BepInEx 安装完成")
        return True
    except Exception as e:
        warning(f"解压 BepInEx 失败: {e}")
        return False


def _find_exe_fallback(path: Path) -> Path | None:
    return find_main_exe(path, recursive=True)


def _select_launch_exe(game_path: Path, engine: object | None = None) -> Path | None:
    """Pick the executable to launch, preserving an explicit user-selected exe."""
    if game_path.is_file() and game_path.suffix.lower() == ".exe":
        info(f"使用手动指定的启动程序: {game_path}")
        return game_path

    exe_path: Path | None = None

    if engine and hasattr(engine, "find_exe"):
        exe_path = engine.find_exe(game_path)
        if exe_path:
            info(f"引擎定位到启动程序: {exe_path}")

    if not exe_path:
        exe_path = _find_exe_fallback(game_path)

    return exe_path


def _select_bgi_launch_exe(game_path: Path, engine: object | None = None) -> Path | None:
    """Pick the BGI interpreter executable, while preserving an explicit exe choice."""
    if game_path.is_file() and game_path.suffix.lower() == ".exe":
        return _select_launch_exe(game_path, engine)

    game_dir = game_path if game_path.is_dir() else game_path.parent
    preferred = game_dir / "BGI.exe"
    if preferred.exists():
        info(f"BGI 启动器优先使用解释器: {preferred}")
        return preferred

    for candidate in game_dir.glob("*.exe"):
        if candidate.name.lower() == "bgi.exe":
            info(f"BGI 启动器优先使用解释器: {candidate}")
            return candidate

    return _select_launch_exe(game_path, engine)
