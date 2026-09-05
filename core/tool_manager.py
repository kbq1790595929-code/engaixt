"""External tool discovery, download, extraction, and manifest tracking.

The translator can support far more game variants when it can reuse the best
open-source tools that already exist for each engine. This module keeps that
logic in one place so engines do not need fragile hard-coded paths.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tarfile
import time
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from utils.logger import info, warning


@dataclass(frozen=True)
class ToolSpec:
    name: str
    executable_names: tuple[str, ...]
    companion_names: tuple[str, ...] = ()
    github_repo: str = ""
    asset_includes: tuple[str, ...] = ()
    asset_excludes: tuple[str, ...] = ()
    install_all_release_assets: bool = False
    direct_urls: tuple[str, ...] = ()
    homepage: str = ""
    notes: str = ""
    min_size: int = 1
    aliases: tuple[str, ...] = field(default_factory=tuple)
    version: str = ""
    source_url: str = ""
    license: str = ""
    license_url: str = ""
    sha256: str = ""
    engine_scope: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    bundled_relative_path: str = ""
    command_template: tuple[str, ...] = ()
    output_contract: tuple[str, ...] = ()


_GITHUB_UA = "game-translator-tool/1.0"
UpdateProgress = Callable[[dict], None]


_KNOWN_TOOLS: dict[str, ToolSpec] = {
    # Godot reverse engineering / PCK tools. Recent releases ship a GDRE
    # package; older builds may expose gdsdecomp.exe or gdpack.exe separately.
    "gdre_tools": ToolSpec(
        name="gdre_tools",
        executable_names=("gdre_tools.exe", "gdre_tools_console.exe"),
        github_repo="GDRETools/gdsdecomp",
        asset_includes=("windows", ".zip"),
        asset_excludes=("android", "linux", "macos"),
        homepage="https://github.com/GDRETools/gdsdecomp",
        notes="Godot PCK extraction/decompilation tools.",
    ),
    "gdsdecomp": ToolSpec(
        name="gdsdecomp",
        executable_names=("gdsdecomp.exe",),
        github_repo="GDRETools/gdsdecomp",
        asset_includes=("windows", ".zip"),
        asset_excludes=("android", "linux", "macos"),
        homepage="https://github.com/GDRETools/gdsdecomp",
        notes="Legacy CLI name; latest releases usually ship gdre_tools.exe instead.",
    ),
    "gdpack": ToolSpec(
        name="gdpack",
        executable_names=("gdpack.exe",),
        github_repo="GDRETools/gdsdecomp",
        asset_includes=("windows", ".zip"),
        asset_excludes=("android", "linux", "macos"),
        homepage="https://github.com/GDRETools/gdsdecomp",
        notes="Required for encrypted Godot PCK repacking when available as a compatible CLI.",
    ),
    # Unity static extract fallback. XUnity realtime remains the preferred
    # automatic path, but AssetRipper helps when a game needs offline assets.
    "assetripper": ToolSpec(
        name="assetripper",
        executable_names=(
            "AssetRipper.exe",
            "AssetRipper.GUI.Free.exe",
            "AssetRipper.CLI.exe",
            "AssetRipper.Console.exe",
        ),
        github_repo="AssetRipper/AssetRipper",
        asset_includes=("win_x64", ".zip"),
        asset_excludes=("linux", "mac", "arm64"),
        homepage="https://github.com/AssetRipper/AssetRipper",
        min_size=10_000_000,
    ),
    # Ren'Py decompiler. This is downloaded as source because the upstream
    # release assets are Ren'Py in-game files rather than a Windows CLI bundle.
    "unrpyc": ToolSpec(
        name="unrpyc",
        executable_names=("unrpyc.py",),
        homepage="https://github.com/CensoredUsername/unrpyc",
    ),
    "uberwolf": ToolSpec(
        name="uberwolf",
        executable_names=("UberWolfCli.exe",),
        github_repo="Sinflower/UberWolf",
        asset_includes=("uberwolfcli.exe",),
        asset_excludes=("uberwolf.exe",),
        homepage="https://github.com/Sinflower/UberWolf",
        notes="MIT-licensed WOLF RPG archive/key/protection tool used by the static pipeline.",
        min_size=1_000_000,
    ),
    "7zip": ToolSpec(
        name="7zip",
        executable_names=("7z.exe", "7za.exe"),
        direct_urls=("https://github.com/ip7z/7zip/releases/download/26.01/7z2601-extra.7z",),
        homepage="https://www.7-zip.org/",
        notes="Used to extract RAR/7z tool archives such as GARbro.",
        min_size=1_000_000,
    ),
    # Optional visual novel archive browser. The official release usually
    # ships the GUI only, so keep console and GUI identities separate: engines
    # must never launch a GUI executable in background batch mode.
    "garbro": ToolSpec(
        name="garbro",
        executable_names=("GARbro.Console.exe", "GARbro.GUI.exe", "GARbro.exe"),
        github_repo="morkt/GARbro",
        asset_includes=("garbro",),
        asset_excludes=("setup",),
        homepage="https://github.com/morkt/GARbro",
        notes="Optional XP3/VN archive extraction helper; GUI builds are useful manually.",
    ),
    "garbro_console": ToolSpec(
        name="garbro_console",
        executable_names=("GARbro.Console.exe",),
        github_repo="crskycode/GARbro",
        asset_includes=("garbro-mod", ".zip"),
        asset_excludes=("source",),
        homepage="https://github.com/crskycode/GARbro",
        notes="Modern GARbro command-line executable. Required for automatic protected XP3 extraction.",
        min_size=10_000_000,
        version="managed",
        source_url="https://github.com/crskycode/GARbro",
        license="MIT",
        license_url="https://github.com/morkt/GARbro/blob/master/LICENSE",
        engine_scope=("kirikiri",),
        capabilities=("archive_list", "archive_extract"),
        bundled_relative_path="_internal/tools/kirikiri/garbro",
        command_template=("{tool}", "{archive}"),
        output_contract=("script_entries", "extracted_files"),
    ),
    "garbro_mod": ToolSpec(
        name="garbro_mod",
        executable_names=("GARbro.Console.exe",),
        github_repo="crskycode/GARbro",
        asset_includes=("garbro-mod", ".zip"),
        asset_excludes=("source",),
        homepage="https://github.com/crskycode/GARbro",
        notes="GARbro Mod build with newer visual-novel archive schemes, including protected KiriKiri/YUZUSOFT XP3 variants.",
        min_size=10_000_000,
        version="managed",
        source_url="https://github.com/crskycode/GARbro",
        license="MIT",
        license_url="https://github.com/morkt/GARbro/blob/master/LICENSE",
        engine_scope=("kirikiri",),
        capabilities=("archive_list", "archive_extract"),
        bundled_relative_path="_internal/tools/kirikiri/garbro",
        command_template=("{tool}", "{archive}"),
        output_contract=("script_entries", "extracted_files"),
    ),
    "garbro_gui": ToolSpec(
        name="garbro_gui",
        executable_names=("GARbro.GUI.exe", "GARbro.exe"),
        homepage="https://github.com/morkt/GARbro",
        notes="GARbro GUI executable. Useful for manual XP3/VN archive extraction.",
    ),
    "krkrdump": ToolSpec(
        name="krkrdump",
        executable_names=("KrkrDumpLoader.exe",),
        companion_names=("KrkrDump.dll",),
        github_repo="crskycode/KrkrDump",
        asset_includes=(".zip",),
        homepage="https://github.com/crskycode/KrkrDump",
        notes="KiriKiri/KiriKiriZ file-stream dumper for protected XP3 resources.",
        min_size=100_000,
    ),
    "krkrpatch": ToolSpec(
        name="krkrpatch",
        executable_names=("KrkrPatchLoader.exe",),
        companion_names=("KrkrPatch.dll",),
        github_repo="crskycode/KrkrPatch",
        asset_includes=(".zip",),
        homepage="https://github.com/crskycode/KrkrPatch",
        notes="KiriKiri file-stream patch bridge for protected XP3 games.",
        min_size=100_000,
    ),
    "krkrextract": ToolSpec(
        name="krkrextract",
        executable_names=("KrkrExtract.Lite.exe",),
        companion_names=("KrkrExtract.Core.dll", "KrkrExtract.UI.Lite.dll"),
        github_repo="xmoezzz/KrkrExtract",
        asset_includes=("krkrextract",),
        homepage="https://github.com/xmoezzz/KrkrExtract",
        notes="KiriKiri XP3-like archive unpacker and Universal Dumper/Patch GUI helper.",
        min_size=100_000,
        install_all_release_assets=True,
    ),
    "kirikiri_xp3pack": ToolSpec(
        name="kirikiri_xp3pack",
        executable_names=("Xp3Pack.exe",),
        direct_urls=("https://github.com/arcusmaximus/KirikiriTools/releases/download/1.7/Xp3Pack.exe",),
        homepage="https://github.com/arcusmaximus/KirikiriTools",
        notes="Creates unencrypted XP3 archives accepted by the KirikiriTools version.dll bridge.",
        min_size=5_000,
        version="1.7",
        source_url="https://github.com/arcusmaximus/KirikiriTools/releases/tag/1.7",
        license="MIT",
        license_url="https://github.com/arcusmaximus/KirikiriTools/blob/master/LICENSE",
        engine_scope=("kirikiri",),
        capabilities=("archive_pack",),
        bundled_relative_path="_internal/tools/kirikiri/kirikiri_tools",
        command_template=("{tool}", "{input_dir}", "{output_archive}"),
        output_contract=("xp3_readable", "entry_count_match"),
    ),
    "kirikiri_unencrypted_version": ToolSpec(
        name="kirikiri_unencrypted_version",
        executable_names=("version.dll",),
        direct_urls=("https://github.com/arcusmaximus/KirikiriTools/releases/download/1.7/version.dll",),
        homepage="https://github.com/arcusmaximus/KirikiriTools",
        notes="KirikiriTools bridge DLL that lets games load unencrypted XP3 patches.",
        min_size=50_000,
        version="1.7",
        source_url="https://github.com/arcusmaximus/KirikiriTools/releases/tag/1.7",
        license="MIT",
        license_url="https://github.com/arcusmaximus/KirikiriTools/blob/master/LICENSE",
        engine_scope=("kirikiri",),
        capabilities=("xp3_bridge", "sjis_compatibility"),
        bundled_relative_path="_internal/tools/kirikiri/kirikiri_tools",
        output_contract=("manifest_owned", "rollback_safe"),
    ),
    "vntextpatch": ToolSpec(
        name="vntextpatch",
        executable_names=("VNTextPatch.exe", "VNTextPatch"),
        github_repo="arcusmaximus/VNTranslationTools",
        homepage="https://github.com/arcusmaximus/VNTranslationTools",
        notes="备用 KRKR KS/SCN 脚本提取、导入、角色名和 SJIS tunnel 适配器。",
        version="source-built",
        source_url="https://github.com/arcusmaximus/VNTranslationTools",
        license="MIT",
        license_url="https://github.com/arcusmaximus/VNTranslationTools/blob/main/LICENSE",
        engine_scope=("kirikiri",),
        capabilities=("script_export", "script_import", "speaker_names", "sjis_tunnel", "word_wrap"),
        bundled_relative_path="_internal/tools/kirikiri/vntextpatch",
        command_template=("{tool}", "extractlocal", "{input_dir}", "{output_dir}"),
        output_contract=("text_items", "speaker_roles", "script_round_trip"),
    ),
    "vntextproxy": ToolSpec(
        name="vntextproxy",
        executable_names=("VNTextProxy.dll",),
        github_repo="arcusmaximus/VNTranslationTools",
        homepage="https://github.com/arcusmaximus/VNTranslationTools",
        notes="KRKR 静态 SJIS tunnel 的显示层代理，仅在原生显示层不适用时启用。",
        version="source-built",
        source_url="https://github.com/arcusmaximus/VNTranslationTools",
        license="MIT",
        license_url="https://github.com/arcusmaximus/VNTranslationTools/blob/main/LICENSE",
        engine_scope=("kirikiri",),
        capabilities=("sjis_tunnel_decode", "font_compatibility", "locale_compatibility"),
        bundled_relative_path="_internal/tools/kirikiri/vntextproxy",
        output_contract=("sjis_ext_bin", "single_display_backend"),
    ),
    "msg_tool": ToolSpec(
        name="msg_tool",
        executable_names=("msg_tool.exe", "msg-tool.exe", "msg_tool", "msg-tool"),
        github_repo="lifegpc/msg-tool",
        homepage="https://github.com/lifegpc/msg-tool",
        notes="KRKR KS/SCN/TJS2/XP3 的第二备用导出、导入和封包工具。",
        version="source-built",
        source_url="https://github.com/lifegpc/msg-tool",
        license="GPL-3.0",
        license_url="https://github.com/lifegpc/msg-tool/blob/master/LICENSE",
        engine_scope=("kirikiri",),
        capabilities=("archive_unpack", "archive_pack", "script_export", "script_import"),
        bundled_relative_path="_internal/tools/kirikiri/msg_tool",
        command_template=("{tool}", "unpack", "{archive}", "{output_dir}"),
        output_contract=("script_files", "archive_round_trip", "exit_code_not_sufficient"),
    ),
    "unrealpak": ToolSpec(
        name="unrealpak",
        executable_names=("UnrealPak.exe",),
        homepage="https://docs.unrealengine.com/",
        notes="Installed with Unreal Engine; not redistributed by this tool.",
    ),
    "fmodel": ToolSpec(
        name="fmodel",
        executable_names=("FModel.exe",),
        github_repo="4sval/FModel",
        asset_includes=("fmodel.zip",),
        homepage="https://github.com/4sval/FModel",
        notes="Optional Unreal asset browser/extractor.",
        min_size=5_000_000,
    ),
    "rpgmdec": ToolSpec(
        name="rpgmdec",
        executable_names=("rpgmdec.exe",),
        homepage="https://github.com/rpg-maker-translation-tools/rpgmdec",
    ),
    "rpgmad": ToolSpec(
        name="rpgmad",
        executable_names=("rpgmad.exe", "rpgmad_v4.2.0.exe", "rpgmad"),
        homepage="https://github.com/rpg-maker-translation-tools/rpgm-archive-decrypter",
    ),
}


_ALIASES = {
    "gdre": "gdre_tools",
    "gdre_tools.exe": "gdre_tools",
    "assetstudio": "assetstudio",
    "assetripper.exe": "assetripper",
    "garbro.console": "garbro_console",
    "garbro.console.exe": "garbro_console",
    "garbro_console.exe": "garbro_console",
    "garbro-mod": "garbro_mod",
    "garbro_mod": "garbro_mod",
    "garbro.gui": "garbro_gui",
    "garbro.gui.exe": "garbro_gui",
    "krkrdump.exe": "krkrdump",
    "krkrdumploader": "krkrdump",
    "krkrdumploader.exe": "krkrdump",
    "krkrpatch.exe": "krkrpatch",
    "krkrpatchloader": "krkrpatch",
    "krkrpatchloader.exe": "krkrpatch",
    "krkrextract": "krkrextract",
    "krkrextract.exe": "krkrextract",
    "krkrextract.lite": "krkrextract",
    "krkrextract.lite.exe": "krkrextract",
    "xp3pack": "kirikiri_xp3pack",
    "xp3pack.exe": "kirikiri_xp3pack",
    "kirikiri_version": "kirikiri_unencrypted_version",
    "vntextpatch": "vntextpatch",
    "vntextpatch.exe": "vntextpatch",
    "vntextproxy": "vntextproxy",
    "vntextproxy.dll": "vntextproxy",
    "msg-tool": "msg_tool",
    "msg_tool": "msg_tool",
    "msg-tool.exe": "msg_tool",
    "7z": "7zip",
    "7za": "7zip",
    "7zip.exe": "7zip",
    "unrealpak.exe": "unrealpak",
    "uberwolf": "uberwolf",
    "uberwolfcli": "uberwolf",
    "uberwolfcli.exe": "uberwolf",
    "wolfdec": "uberwolf",
    "wolfdec.exe": "uberwolf",
}


def get_tools_dir() -> Path:
    from config import get_config

    cfg = get_config()
    if cfg.tools_dir:
        return Path(cfg.tools_dir)
    return Path.home() / "Downloads" / ".game_translator" / "tools"


def get_project_tools_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "tools"


def get_bundled_tools_dir() -> Path:
    """Return tools shipped inside a source checkout or frozen package."""
    try:
        from core.resources import resource_path
        return resource_path("_internal", "tools")
    except Exception:
        return Path(__file__).resolve().parent.parent / "_internal" / "tools"


def get_manifest_path() -> Path:
    return get_tools_dir() / "manifest.json"


def list_tool_dirs() -> list[Path]:
    """Return search roots, ordered from user-configured to bundled tools."""
    roots = [
        get_tools_dir(),
        get_bundled_tools_dir(),
        get_project_tools_dir(),
        Path(__file__).resolve().parent.parent / "downloads",
    ]
    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        try:
            key = str(root.resolve())
        except Exception:
            key = str(root)
        if key not in seen:
            unique.append(root)
            seen.add(key)
    return unique


def known_tool_names() -> list[str]:
    return sorted(_KNOWN_TOOLS)


def get_tool_spec(name: str) -> ToolSpec | None:
    return _KNOWN_TOOLS.get(_canonical_name(name))


def find_tool(name: str) -> Path | None:
    """Find a tool on PATH, in the configured tools dir, or bundled tools."""
    canonical = _canonical_name(name)
    if canonical == "unrpyc":
        candidate = get_tools_dir() / "unrpyc" / "unrpyc.py"
        if _unrpyc_complete(candidate):
            return candidate
        for root in list_tool_dirs():
            if not root.exists():
                continue
            for p in _iter_files(root):
                if p.name.lower() == "unrpyc.py" and _unrpyc_complete(p):
                    return p
        return None

    if canonical == "unrealpak":
        unrealpak = _find_unrealpak_installation()
        if unrealpak:
            return unrealpak

    if canonical == "7zip":
        seven_zip = _find_7zip_installation()
        if seven_zip:
            return seven_zip

    spec = _KNOWN_TOOLS.get(canonical)
    executable_names = spec.executable_names if spec else (f"{canonical}.exe", canonical)

    wanted = {n.lower() for n in executable_names}
    stem_wanted = {
        Path(n).stem.lower()
        for n in executable_names
        if Path(n).suffix.lower() in {"", ".exe", ".bat", ".cmd", ".py"}
    }
    for root in list_tool_dirs():
        if not root.exists():
            continue
        for p in _iter_files(root):
            lower_name = p.name.lower()
            lower_stem = p.stem.lower()
            if lower_name in wanted or lower_stem in stem_wanted:
                return p

    # PATH lookup last, so the self-managed tools directory wins when both
    # local and system installs exist.
    for exe_name in executable_names:
        candidates = [exe_name]
        stem = Path(exe_name).stem
        if stem != exe_name:
            candidates.append(stem)
        for candidate in candidates:
            found = shutil.which(candidate)
            if found:
                return Path(found)
    return None


def ensure_tool(name: str) -> Path | None:
    """Ensure a tool exists and return its path.

    If auto-download is disabled, this only searches existing locations.
    """
    found = find_tool(name)
    if found:
        return found

    canonical = _canonical_name(name)
    if canonical == "unrpyc":
        return ensure_unrpyc()

    # gdpack/gdsdecomp are legacy/variant-specific command-line names. The
    # modern GDRE release provides gdre_tools.exe, which is useful elsewhere but
    # does not guarantee the same --encrypt/--decrypt CLI contract. Avoid
    # repeatedly downloading the same package and returning the wrong binary.
    if canonical in {"gdpack", "gdsdecomp"}:
        warning(f"{canonical} CLI not found; gdre_tools may still be available for normal PCK extraction.")
        return None

    spec = _KNOWN_TOOLS.get(canonical)
    if not spec:
        warning(f"Unknown external tool: {name}")
        return None

    from config import get_config

    if not get_config().auto_download_tools:
        warning(f"Tool {name} not found and automatic download is disabled.")
        return None

    installed = _install_tool_package(spec)
    if installed:
        return installed
    return find_tool(name)


def ensure_unrpyc() -> Path | None:
    found = find_tool("unrpyc")
    if found:
        return found

    from config import get_config

    if not get_config().auto_download_tools:
        warning("unrpyc.py not found and automatic download is disabled.")
        return None

    dest_root = get_tools_dir() / "unrpyc"
    zip_url = "https://github.com/CensoredUsername/unrpyc/archive/refs/heads/master.zip"
    zip_path = get_tools_dir() / "_downloads" / "unrpyc-master.zip"
    try:
        _download_url(zip_url, zip_path, min_size=10_000)
        tmp_root = get_tools_dir() / "_extract" / "unrpyc"
        if tmp_root.exists():
            shutil.rmtree(tmp_root)
        _safe_extract_zip(zip_path, tmp_root)
        source = next((p for p in tmp_root.iterdir() if p.is_dir() and p.name.startswith("unrpyc-")), None)
        if not source:
            raise ValueError("unrpyc source directory not found in archive")
        if dest_root.exists():
            shutil.rmtree(dest_root)
        shutil.copytree(source, dest_root)
        if not _unrpyc_complete(dest_root / "unrpyc.py"):
            raise ValueError("unrpyc archive did not contain required files")
        shutil.rmtree(tmp_root, ignore_errors=True)
        manifest = _load_manifest()
        manifest["unrpyc"] = {
            "source": "https://github.com/CensoredUsername/unrpyc",
            "path": str(dest_root / "unrpyc.py"),
        }
        _save_manifest(manifest)
        info(f"unrpyc installed: {dest_root / 'unrpyc.py'}")
        return dest_root / "unrpyc.py"
    except Exception as e:
        warning(f"Failed to install unrpyc: {e}")
        return None


def ensure_recommended_tools(tool_names: Iterable[str] | None = None,
                             progress_callback: UpdateProgress | None = None,
                             auto_update: bool = True) -> dict[str, str | None]:
    """Install/find a broad default tool set and return resolved paths."""
    names = list(tool_names or [
        "7zip",
        "gdre_tools",
        "assetripper",
        "unrpyc",
        "uberwolf",
        "fmodel",
        "rpgmdec",
        "rpgmad",
        "garbro",
        "unrealpak",
    ])
    results: dict[str, str | None] = {}
    if auto_update:
        maybe_auto_update_tools(names, progress_callback=progress_callback)
    total = len(names)
    for index, name in enumerate(names, 1):
        canonical = _canonical_name(name)
        _emit_tool_progress(
            progress_callback,
            phase="ensure_start",
            tool=canonical,
            index=index,
            total=total,
            message=f"检查/安装工具 {canonical} ({index}/{total})",
        )
        try:
            path = ensure_tool(name)
            results[name] = str(path) if path else None
            _emit_tool_progress(
                progress_callback,
                phase="ensure_done",
                tool=canonical,
                index=index,
                total=total,
                status="ready" if path else "missing",
                path=str(path) if path else "",
                message=f"{canonical} {'已就绪' if path else '缺失'} ({index}/{total})",
            )
        except Exception as e:
            warning(f"Tool setup failed for {name}: {e}")
            results[name] = None
            _emit_tool_progress(
                progress_callback,
                phase="ensure_failed",
                tool=canonical,
                index=index,
                total=total,
                status="failed",
                error=str(e),
                message=f"{canonical} 安装检查失败 ({index}/{total})",
            )
    return results


def update_recommended_tools(tool_names: Iterable[str] | None = None, *, force: bool = True,
                             progress_callback: UpdateProgress | None = None) -> dict[str, dict]:
    """Check managed tools and update stale GitHub/direct downloads.

    This only touches packages installed inside the configured tools directory.
    System PATH tools, bundled project tools, and absent optional tools are
    reported but never modified.
    """
    names = list(tool_names or [
        "7zip",
        "gdre_tools",
        "assetripper",
        "unrpyc",
        "uberwolf",
        "fmodel",
        "rpgmdec",
        "rpgmad",
        "garbro",
        "garbro_console",
        "garbro_mod",
        "krkrdump",
        "krkrpatch",
        "krkrextract",
        "kirikiri_xp3pack",
        "kirikiri_unencrypted_version",
        "unrealpak",
    ])
    results: dict[str, dict] = {}
    total = len(names)
    _emit_tool_progress(progress_callback, phase="update_begin", index=0, total=total, message="开始检查工具更新")
    for index, name in enumerate(names, 1):
        canonical = _canonical_name(name)
        spec = _KNOWN_TOOLS.get(canonical)
        _emit_tool_progress(
            progress_callback,
            phase="tool_check",
            tool=canonical,
            index=index,
            total=total,
            message=f"检查工具更新 {canonical} ({index}/{total})",
        )
        if not spec:
            results[canonical] = {"status": "unknown"}
            _emit_tool_progress(
                progress_callback,
                phase="tool_done",
                tool=canonical,
                index=index,
                total=total,
                status="unknown",
                message=f"{canonical} 未知工具 ({index}/{total})",
            )
            continue
        try:
            results[canonical] = _update_tool_package(
                spec,
                force=force,
                progress_callback=progress_callback,
                index=index,
                total=total,
            )
        except Exception as exc:
            warning(f"Tool update failed for {canonical}: {exc}")
            results[canonical] = {"status": "failed", "error": str(exc)}
        status = str(results[canonical].get("status", ""))
        _emit_tool_progress(
            progress_callback,
            phase="tool_done",
            tool=canonical,
            index=index,
            total=total,
            status=status,
            message=f"{canonical}: {status or 'done'} ({index}/{total})",
        )
    _emit_tool_progress(progress_callback, phase="update_done", index=total, total=total, message="工具更新检查完成")
    return results


def maybe_auto_update_tools(tool_names: Iterable[str] | None = None,
                            progress_callback: UpdateProgress | None = None) -> dict[str, dict]:
    """Run a throttled update pass when config enables automatic tool updates."""
    from config import DEFAULT_TOOL_UPDATE_INTERVAL_HOURS, get_config

    cfg = get_config()
    if not getattr(cfg, "auto_update_tools", True):
        return {}
    if not getattr(cfg, "auto_download_tools", True):
        return {}

    manifest = _load_manifest()
    now = int(time.time())
    interval_hours = int(getattr(cfg, "tool_update_interval_hours", DEFAULT_TOOL_UPDATE_INTERVAL_HOURS) or 0)
    interval_seconds = max(0, interval_hours) * 3600
    meta = manifest.get("_tool_update", {})
    last_checked = int(meta.get("checked_at", 0) or 0) if isinstance(meta, dict) else 0
    if interval_seconds and now - last_checked < interval_seconds:
        _emit_tool_progress(
            progress_callback,
            phase="update_skipped",
            status="throttled",
            message="工具更新检查未到间隔，跳过",
        )
        return {}

    results = update_recommended_tools(tool_names, force=False, progress_callback=progress_callback)
    manifest = _load_manifest()
    manifest["_tool_update"] = {
        "checked_at": now,
        "interval_hours": interval_hours,
        "tool_names": [_canonical_name(n) for n in (tool_names or [])],
        "summary": _summarize_update_results(results),
    }
    _save_manifest(manifest)
    summary = manifest["_tool_update"]["summary"]
    if summary.get("updated"):
        info(
            "工具自动更新完成: "
            f"更新 {summary.get('updated', 0)}，已是最新 {summary.get('current', 0)}，"
            f"跳过 {summary.get('skipped', 0)}，失败 {summary.get('failed', 0)}"
        )
    return results


def _summarize_update_results(results: dict[str, dict]) -> dict[str, int]:
    summary = {"updated": 0, "current": 0, "skipped": 0, "failed": 0}
    for result in results.values():
        status = str(result.get("status", ""))
        if status == "updated":
            summary["updated"] += 1
        elif status in {"current", "checked"}:
            summary["current"] += 1
        elif status == "failed":
            summary["failed"] += 1
        else:
            summary["skipped"] += 1
    return summary


def verify_tool(name: str) -> bool:
    path = find_tool(name)
    if not path or not path.exists():
        return False
    manifest = _load_manifest()
    entry = manifest.get(_canonical_name(name), {})
    expected = entry.get("sha256")
    if not expected:
        return True
    try:
        return _compute_sha256(path) == expected
    except Exception:
        return False


def tool_status(name: str) -> dict:
    canonical = _canonical_name(name)
    spec = _KNOWN_TOOLS.get(canonical)
    path = find_tool(canonical)
    manifest = _load_manifest()
    entry = manifest.get(canonical, {})
    return {
        "name": canonical,
        "found": bool(path),
        "path": str(path) if path else "",
        "homepage": spec.homepage if spec else "",
        "notes": spec.notes if spec else "",
        "managed": _is_managed_tool_path(path) if path else False,
        "release_tag": entry.get("release_tag", "") if isinstance(entry, dict) else "",
        "asset": entry.get("asset", "") if isinstance(entry, dict) else "",
        "updated_at": entry.get("updated_at", 0) if isinstance(entry, dict) else 0,
    }


def _install_tool_package(spec: ToolSpec) -> Path | None:
    package_dir = get_tools_dir() / spec.name
    package_dir.mkdir(parents=True, exist_ok=True)

    urls: list[tuple[str, str]] = []
    if spec.github_repo:
        if spec.install_all_release_assets or spec.companion_names:
            urls.extend(_get_latest_release_assets(spec))
        else:
            release_asset = _get_latest_release_asset(spec)
            if release_asset:
                urls.append(release_asset)
    urls.extend((Path(url).name or f"{spec.name}.download", url) for url in spec.direct_urls)

    if not urls:
        warning(f"No automatic download source is configured for {spec.name}.")
        return None

    installed: Path | None = None
    failures: list[str] = []
    for asset_name, url in urls:
        try:
            download_path = get_tools_dir() / "_downloads" / _safe_filename(asset_name)
            _download_url(url, download_path, min_size=spec.min_size)
            candidate = _install_downloaded_file(download_path, package_dir, spec)
            installed = candidate or installed
            if installed and _tool_install_complete(package_dir, spec):
                manifest = _load_manifest()
                manifest[spec.name] = {
                    "url": url,
                    "asset": asset_name,
                    "path": str(installed),
                    "sha256": _compute_sha256(installed) if installed.is_file() else "",
                    "managed": True,
                    "updated_at": int(time.time()),
                }
                _save_manifest(manifest)
                info(f"{spec.name} installed: {installed}")
                return installed
        except Exception as e:
            failures.append(f"{asset_name}: {e}")
            warning(f"Failed to install {spec.name} from {url}: {e}")
    if installed and _tool_install_complete(package_dir, spec):
        manifest = _load_manifest()
        manifest[spec.name] = {
            "asset": "multiple",
            "path": str(installed),
            "sha256": _compute_sha256(installed) if installed.is_file() else "",
            "managed": True,
            "updated_at": int(time.time()),
        }
        _save_manifest(manifest)
        info(f"{spec.name} installed: {installed}")
        return installed
    if failures:
        warning(f"{spec.name} install incomplete: {'; '.join(failures[:3])}")
    return None


def _update_tool_package(spec: ToolSpec, *, force: bool = False,
                         progress_callback: UpdateProgress | None = None,
                         index: int = 0, total: int = 0) -> dict:
    canonical = spec.name
    package_dir = get_tools_dir() / canonical
    manifest = _load_manifest()
    entry = manifest.get(canonical, {})
    current_path = find_tool(canonical)
    managed = _is_managed_tool_path(current_path) if current_path else False

    if current_path and not managed:
        return {"status": "skipped", "reason": "external_or_bundled_tool", "path": str(current_path)}
    if not current_path:
        # Updates are not installs. Missing optional tools are installed only
        # through ensure_tool/ensure_recommended_tools so a manual update check
        # cannot unexpectedly download many large packages.
        return {"status": "skipped", "reason": "not_installed"}

    if canonical == "unrpyc":
        # unrpyc follows a branch archive rather than release assets. Keep the
        # first version simple: install when absent, but do not auto-replace a
        # working checkout without a stable upstream version marker.
        if current_path and _unrpyc_complete(current_path):
            return {"status": "skipped", "reason": "source_archive_without_version", "path": str(current_path)}
        installed = ensure_unrpyc()
        return {"status": "updated" if installed else "failed", "path": str(installed) if installed else ""}

    if not spec.github_repo and not spec.direct_urls:
        return {"status": "skipped", "reason": "no_update_source", "path": str(current_path or "")}

    _emit_tool_progress(
        progress_callback,
        phase="resolve_release",
        tool=canonical,
        index=index,
        total=total,
        message=f"查询 {canonical} 最新版本",
    )
    assets, release_meta = _resolve_tool_downloads(spec)
    if not assets:
        _touch_tool_check(canonical, entry, release_meta)
        return {"status": "checked", "reason": "no_matching_asset", "path": str(current_path)}

    latest_key = _release_key(release_meta, assets)
    current_key = str(entry.get("release_key") or entry.get("release_tag") or entry.get("url") or "")
    if current_path and current_key == latest_key:
        _touch_tool_check(canonical, entry, release_meta)
        return {"status": "current", "release_key": latest_key, "path": str(current_path)}

    tmp_root = get_tools_dir() / "_updates" / f"{canonical}-{int(time.time())}"
    backup_root = get_tools_dir() / "_backup" / f"{canonical}-{int(time.time())}"
    try:
        tmp_root.mkdir(parents=True, exist_ok=True)
        installed: Path | None = None
        asset_total = len(assets)
        for asset_index, (asset_name, url) in enumerate(assets, 1):
            _emit_tool_progress(
                progress_callback,
                phase="download_start",
                tool=canonical,
                asset=asset_name,
                asset_index=asset_index,
                asset_total=asset_total,
                index=index,
                total=total,
                message=f"下载 {canonical}: {asset_name} ({asset_index}/{asset_total})",
            )
            download_path = get_tools_dir() / "_downloads" / _safe_filename(asset_name)
            _download_url(url, download_path, min_size=spec.min_size)
            _emit_tool_progress(
                progress_callback,
                phase="download_done",
                tool=canonical,
                asset=asset_name,
                asset_index=asset_index,
                asset_total=asset_total,
                index=index,
                total=total,
                message=f"已下载 {canonical}: {asset_name}",
            )
            _emit_tool_progress(
                progress_callback,
                phase="extract_start",
                tool=canonical,
                asset=asset_name,
                asset_index=asset_index,
                asset_total=asset_total,
                index=index,
                total=total,
                message=f"解包 {canonical}: {asset_name}",
            )
            candidate = _install_downloaded_file(download_path, tmp_root, spec)
            installed = candidate or installed

        if not installed:
            installed = _find_in_root(tmp_root, spec.executable_names)
        if not installed or not _tool_install_complete(tmp_root, spec):
            raise RuntimeError("updated package did not contain required tool files")

        _emit_tool_progress(
            progress_callback,
            phase="replace_start",
            tool=canonical,
            index=index,
            total=total,
            message=f"替换 {canonical} 工具文件",
        )
        if package_dir.exists():
            backup_root.parent.mkdir(parents=True, exist_ok=True)
            if backup_root.exists():
                shutil.rmtree(backup_root)
            package_dir.replace(backup_root)
        package_dir.parent.mkdir(parents=True, exist_ok=True)
        tmp_root.replace(package_dir)

        installed_final = _find_in_root(package_dir, spec.executable_names)
        if not installed_final or not _tool_install_complete(package_dir, spec):
            raise RuntimeError("updated package failed final validation")

        manifest = _load_manifest()
        manifest[canonical] = {
            **(entry if isinstance(entry, dict) else {}),
            "source": spec.homepage or (f"https://github.com/{spec.github_repo}" if spec.github_repo else ""),
            "release_tag": str(release_meta.get("tag_name", "")),
            "release_id": release_meta.get("id", ""),
            "release_key": latest_key,
            "asset": ", ".join(asset_name for asset_name, _ in assets),
            "url": assets[0][1] if len(assets) == 1 else "",
            "path": str(installed_final),
            "sha256": _compute_sha256(installed_final) if installed_final.is_file() else "",
            "checked_at": int(time.time()),
            "updated_at": int(time.time()),
            "managed": True,
        }
        _save_manifest(manifest)
        shutil.rmtree(backup_root, ignore_errors=True)
        info(f"{canonical} updated: {installed_final}")
        return {
            "status": "updated",
            "release_key": latest_key,
            "path": str(installed_final),
            "asset": manifest[canonical]["asset"],
        }
    except Exception as exc:
        if package_dir.exists():
            shutil.rmtree(package_dir, ignore_errors=True)
        if backup_root.exists():
            try:
                backup_root.replace(package_dir)
            except Exception:
                warning(f"Failed to restore previous {canonical} package from backup: {backup_root}")
        raise exc
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def _touch_tool_check(name: str, entry: dict, release_meta: dict):
    manifest = _load_manifest()
    current = manifest.get(name, {})
    if not isinstance(current, dict):
        current = {}
    current.update(entry if isinstance(entry, dict) else {})
    current["checked_at"] = int(time.time())
    if release_meta.get("tag_name"):
        current.setdefault("release_tag", str(release_meta.get("tag_name")))
    manifest[name] = current
    _save_manifest(manifest)


def _resolve_tool_downloads(spec: ToolSpec) -> tuple[list[tuple[str, str]], dict]:
    if spec.github_repo:
        release = _get_latest_release(spec)
        if not release:
            return [], {}
        if spec.install_all_release_assets or spec.companion_names:
            return _select_release_assets(spec, release, all_required=True), release
        return _select_release_assets(spec, release, all_required=False), release

    assets = [(Path(url).name or f"{spec.name}.download", url) for url in spec.direct_urls]
    return assets, {"tag_name": "direct", "id": ""}


def _release_key(release_meta: dict, assets: list[tuple[str, str]]) -> str:
    tag = str(release_meta.get("tag_name") or release_meta.get("id") or "")
    asset_part = "|".join(f"{name}:{url}" for name, url in assets)
    return f"{tag}|{asset_part}" if tag else asset_part


def _emit_tool_progress(callback: UpdateProgress | None, **payload):
    if not callback:
        return
    try:
        callback(dict(payload))
    except Exception:
        pass


def _install_downloaded_file(download_path: Path, package_dir: Path,
                             spec: ToolSpec) -> Path | None:
    lower = download_path.name.lower()
    if spec.name == "7zip":
        _install_7zip(download_path, package_dir)
    elif lower.endswith(".zip"):
        if not zipfile.is_zipfile(download_path):
            raise ValueError(f"Downloaded file is not a valid zip: {download_path.name}")
        _safe_extract_zip(download_path, package_dir)
    elif lower.endswith((".tar.gz", ".tgz", ".tar")):
        _safe_extract_tar(download_path, package_dir)
    elif lower.endswith(".rar"):
        _safe_extract_with_7zip(download_path, package_dir)
    elif lower.endswith(".exe"):
        dest = package_dir / download_path.name
        if dest.resolve() != download_path.resolve():
            shutil.copy2(download_path, dest)
    elif lower.endswith(".dll"):
        dest = package_dir / download_path.name
        if dest.resolve() != download_path.resolve():
            shutil.copy2(download_path, dest)
    else:
        warning(f"Downloaded {download_path.name}, but this archive type is not auto-extracted.")

    return _find_in_root(package_dir, spec.executable_names) or find_tool(spec.name)


def _tool_install_complete(package_dir: Path, spec: ToolSpec) -> bool:
    if not _find_in_root(package_dir, spec.executable_names):
        return False
    for name in spec.companion_names:
        if not _find_in_root(package_dir, (name,)):
            return False
    return True


def _is_managed_tool_path(path: Path | None) -> bool:
    if not path:
        return False
    try:
        resolved = path.resolve()
        managed_root = get_tools_dir().resolve()
        return _is_relative_to(resolved, managed_root)
    except Exception:
        return False


def _get_latest_release(spec: ToolSpec) -> dict:
    if not spec.github_repo:
        return {}
    url = f"https://api.github.com/repos/{spec.github_repo}/releases/latest"
    request = urllib.request.Request(url, headers={"User-Agent": _GITHUB_UA})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as e:
        warning(f"Could not query GitHub release for {spec.github_repo}: {e}")
        return {}


def _select_release_assets(spec: ToolSpec, release: dict, *, all_required: bool = False) -> list[tuple[str, str]]:
    assets = release.get("assets", [])
    if not isinstance(assets, list):
        return []

    required = tuple(spec.executable_names) + tuple(spec.companion_names)
    if all_required and required:
        wanted = {Path(name).name.lower() for name in required}
        matches: list[tuple[str, str]] = []
        for asset in assets:
            name = str(asset.get("name", ""))
            if name.lower() not in wanted:
                continue
            dl = asset.get("browser_download_url")
            if dl:
                matches.append((name, str(dl)))
        if matches:
            return matches

    includes = tuple(s.lower() for s in spec.asset_includes)
    excludes = tuple(s.lower() for s in spec.asset_excludes)
    for asset in assets:
        name = str(asset.get("name", ""))
        lower = name.lower()
        if includes and not all(token in lower for token in includes):
            continue
        if excludes and any(token in lower for token in excludes):
            continue
        dl = asset.get("browser_download_url")
        if dl:
            return [(name, str(dl))]

    for asset in assets:
        name = str(asset.get("name", ""))
        lower = name.lower()
        if lower in {exe.lower() for exe in spec.executable_names}:
            dl = asset.get("browser_download_url")
            if dl:
                return [(name, str(dl))]
    return []


def _get_latest_release_assets(spec: ToolSpec) -> list[tuple[str, str]]:
    """Return all release assets needed by a tool with split exe/dll assets."""
    release = _get_latest_release(spec)
    if not release:
        return []
    return _select_release_assets(
        spec,
        release,
        all_required=bool(spec.install_all_release_assets and (spec.executable_names or spec.companion_names)),
    )


def _get_latest_release_asset(spec: ToolSpec) -> tuple[str, str] | None:
    release = _get_latest_release(spec)
    if not release:
        return None
    selected = _select_release_assets(spec, release, all_required=False)
    return selected[0] if selected else None


def _download_url(url: str, dest: Path, min_size: int = 1) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size >= min_size:
        return dest
    tmp = dest.with_suffix(dest.suffix + ".part")
    if tmp.exists():
        tmp.unlink()
    info(f"Downloading tool asset: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": _GITHUB_UA})
    with urllib.request.urlopen(request, timeout=120) as response:
        with open(tmp, "wb") as out:
            shutil.copyfileobj(response, out)
    if tmp.stat().st_size < min_size:
        tmp.unlink(missing_ok=True)
        raise ValueError(f"Downloaded file is too small: {url}")
    tmp.replace(dest)
    return dest


def _find_in_root(root: Path, executable_names: Iterable[str]) -> Path | None:
    wanted = {n.lower() for n in executable_names}
    stem_wanted = {
        Path(n).stem.lower()
        for n in executable_names
        if Path(n).suffix.lower() in {"", ".exe", ".bat", ".cmd", ".py"}
    }
    if not root.exists():
        return None
    for p in _iter_files(root):
        if p.name.lower() in wanted or p.stem.lower() in stem_wanted:
            return p
    return None


def _iter_files(root: Path):
    try:
        for p in root.rglob("*"):
            if p.is_file():
                yield p
    except Exception:
        return


def _safe_extract_zip(zip_path: Path, dest: Path):
    dest_resolved = dest.resolve()
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            target = (dest / member.filename).resolve()
            if not _is_relative_to(target, dest_resolved):
                raise ValueError(f"Unsafe zip path: {member.filename}")
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            _clear_windows_zone_identifier(target)


def _safe_extract_tar(tar_path: Path, dest: Path):
    dest_resolved = dest.resolve()
    with tarfile.open(tar_path, "r:*") as tf:
        for member in tf.getmembers():
            target = (dest / member.name).resolve()
            if not _is_relative_to(target, dest_resolved):
                raise ValueError(f"Unsafe tar path: {member.name}")
        tf.extractall(dest)


def _safe_extract_with_7zip(archive_path: Path, dest: Path):
    seven_zip = ensure_tool("7zip")
    if not seven_zip:
        raise RuntimeError("7-Zip is required to extract this archive")
    dest.mkdir(parents=True, exist_ok=True)
    import subprocess

    result = subprocess.run(
        [str(seven_zip), "x", str(archive_path), f"-o{dest}", "-y"],
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr or result.stdout or f"7-Zip failed: {archive_path}")


def _install_7zip(installer_path: Path, package_dir: Path):
    """Install the portable 7-Zip extra package into the tool directory."""
    package_dir.mkdir(parents=True, exist_ok=True)
    try:
        import py7zr  # type: ignore
    except ImportError as e:
        raise RuntimeError("Python package py7zr is required to extract portable 7-Zip") from e
    with py7zr.SevenZipFile(installer_path, "r") as archive:
        archive.extractall(package_dir)
    if not _find_in_root(package_dir, ("7z.exe", "7za.exe")):
        raise RuntimeError("7-Zip package did not contain 7z.exe/7za.exe")


def _find_unrealpak_installation() -> Path | None:
    candidates: list[Path] = []
    roots = [
        Path(os.environ.get("UE_ROOT", "")),
        Path(os.environ.get("UNREAL_ENGINE_ROOT", "")),
        Path("C:/Program Files/Epic Games"),
        Path("C:/Program Files/Unreal Engine"),
        Path("D:/Program Files/Epic Games"),
        Path("D:/Epic Games"),
    ]
    for root in roots:
        if not str(root) or not root.exists():
            continue
        patterns = [
            "UE_*/Engine/Binaries/Win64/UnrealPak.exe",
            "*/Engine/Binaries/Win64/UnrealPak.exe",
            "Engine/Binaries/Win64/UnrealPak.exe",
        ]
        for pattern in patterns:
            candidates.extend(root.glob(pattern))
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    return candidates[0]


def _find_7zip_installation() -> Path | None:
    candidates = [
        Path("C:/Program Files/7-Zip/7z.exe"),
        Path("C:/Program Files (x86)/7-Zip/7z.exe"),
        Path.home() / "Downloads" / "7za" / "7za.exe",
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def _clear_windows_zone_identifier(path: Path):
    if os.name != "nt":
        return
    zone = Path(str(path) + ":Zone.Identifier")
    try:
        if zone.exists():
            zone.unlink()
    except Exception:
        pass


def _canonical_name(name: str) -> str:
    key = name.strip().lower().replace("-", "_")
    key = _ALIASES.get(key, key)
    if key.endswith(".exe"):
        key = key[:-4]
    return key


def _safe_filename(name: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return clean or "download.bin"


def _load_manifest() -> dict:
    mp = get_manifest_path()
    if mp.exists():
        try:
            return json.loads(mp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, TypeError):
            pass
    return {}


def _save_manifest(manifest: dict):
    mp = get_manifest_path()
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")


def _compute_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _unrpyc_complete(path: Path) -> bool:
    if not path.exists():
        return False
    root = path.parent
    required = [
        root / "unrpyc.py",
        root / "decompiler" / "__init__.py",
        root / "decompiler" / "magic.py",
        root / "decompiler" / "renpycompat.py",
        root / "decompiler" / "translate.py",
    ]
    return all(p.exists() and p.stat().st_size > 0 for p in required)
