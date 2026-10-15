"""RPG Maker engine with separate MV/MZ and static RGSS routes.

MV/MZ: Runtime hook pipeline — scan game data in-memory, translate, replace at display layer.
       Never modifies JSON data files. Zero risk to game logic.

XP/VX/VX Ace: Static Marshal extraction and verified game-file deployment.
"""

from __future__ import annotations

import json
from pathlib import Path

from engines.base import EngineBase, EngineCapabilities, TextItem, registry
from engines.rpgmaker_rgss import RGSSStatic, deploy_changes, find_archives, find_data_files
from utils.logger import info, warning
from utils.rgss_archive import safe_relative_name
from utils.rvdata2 import (
    replace_any_strings_in_marshal_full,
)
from utils.text_extract import is_translatable


class RPGMakerEngine(EngineBase):
    name = "rpgmaker"
    label = "RPG Maker (MV/MZ/XP/VX/VXAce)"
    detect_priority = 95
    support_level = "stable"
    supports_extract = True
    supports_repack = True
    capabilities = EngineCapabilities(
        extract=True,
        repack=True,
        static_patch=True,
        runtime_patch=True,
        creates_launcher=False,
        portable_after_patch=True,
        requires_python=False,
        requires_frida=False,
        notes=(
            "MV/MZ 优先使用数据/运行时安全路径，旧版 RGSS 使用 Marshal/归档路径。",
            "mkxp 分支可使用运行时 hook 处理显示层文本。",
        ),
    )

    def __init__(self):
        super().__init__()
        self._rgss_static: RGSSStatic | None = None
        self._is_vxace: bool = False
        self._is_mv_mz: bool = False
        self._is_xp_mkxp: bool = False

    # ---- Detection ----

    def detect(self, path: Path) -> bool:
        if path.is_file():
            path = path.parent

        has_data = (path / "www" / "data").is_dir()
        has_json = bool(list(path.glob("www/data/*.json")))

        has_data_root = (path / "data").is_dir()
        has_nw = (path / "nw.dll").exists() or (path / "nw.exe").exists()
        has_rm_data = has_data_root and (
            bool(list(path.glob("data/Actors.json")))
            or bool(list(path.glob("data/CommonEvents.json")))
        )

        has_project = (path / "Game.rpgproject").exists() or (path / "game.rmmzproject").exists()
        has_game_ini = (path / "Game.ini").exists()
        has_rxdata = _has_rxdata(path)
        has_languages = _has_po_localization(path)
        has_mkxp_runtime = _has_mkxp_runtime(path)
        has_mv_mz_encrypted = (
            bool(list(path.glob("www/data/*.rpgm*")))
            or bool(list(path.glob("www/img/**/*.rpgm*")))
            or bool(list(path.glob("www/audio/**/*.rpgm*")))
        )

        if has_data and has_json:
            return True
        if has_mv_mz_encrypted and (path / "www").is_dir():
            return True
        if has_rm_data and has_nw:
            return True
        if has_game_ini and (bool(list(path.glob("*.rgss*"))) or bool(list(path.glob("Data/*.rvdata*"))) or has_rxdata):
            return True
        if has_rxdata and (has_languages or has_mkxp_runtime or _has_rpgmaker_dirs(path)):
            return True
        return has_project

    def detect_confidence(self, path: Path) -> tuple[int, list[str]]:
        game_dir = path if path.is_dir() else path.parent
        evidence = []
        score = 0

        if (game_dir / "www" / "data").is_dir() and list(game_dir.glob("www/data/*.json")):
            evidence.append("找到 www/data/*.json (RPG Maker MV/MZ)")
            score = max(score, 98)
        if list(game_dir.glob("www/data/*.rpgm*")) or list(game_dir.glob("www/img/**/*.rpgm*")):
            evidence.append("找到 RPG Maker MV/MZ 加密资源 (.rpgm*)")
            score = max(score, 94)

        data_root = game_dir / "data"
        has_nw = (game_dir / "nw.dll").exists() or (game_dir / "nw.exe").exists()
        has_rm_json = (
            bool(list(data_root.glob("Actors.json")))
            or bool(list(data_root.glob("CommonEvents.json")))
        ) if data_root.is_dir() else False
        if has_rm_json and has_nw:
            evidence.append("找到 data/*.json + NW.js (RPG Maker MZ 部署版)")
            score = max(score, 97)

        if (game_dir / "Game.rpgproject").exists() or (game_dir / "game.rmmzproject").exists():
            evidence.append("找到 RPG Maker 项目文件")
            score = max(score, 96)

        if (game_dir / "Game.ini").exists() and (list(game_dir.glob("*.rgss*")) or list(game_dir.glob("Data/*.rvdata*")) or list(game_dir.glob("Data/*.rxdata"))):
            evidence.append("找到 Game.ini + RGSS/RVDATA 数据 (VX/VXAce)")
            score = max(score, 97)
        has_rxdata = _has_rxdata(game_dir)
        has_languages = _has_po_localization(game_dir)
        has_mkxp_runtime = _has_mkxp_runtime(game_dir)
        if has_rxdata and (has_languages or has_mkxp_runtime or _has_rpgmaker_dirs(game_dir)):
            evidence.append("找到 Data/*.rxdata (RPG Maker XP / mkxp)")
            if has_languages:
                evidence.append("找到 Languages/*.po/.loc 本地化资源")
            if has_mkxp_runtime:
                evidence.append("找到 Ruby/SDL/mkxp 运行库特征")
            score = max(score, 96)
        return score, evidence

    # ---- Unpack (text extraction + grouped translation for MV/MZ) ----

    def unpack(self, path: Path, workspace: Path) -> list[TextItem]:
        game_dir = path if path.is_dir() else path.parent
        self._game_dir = game_dir
        self._rgss_static = None
        self._is_vxace = self._is_mv_mz = self._is_xp_mkxp = False

        archives = find_archives(game_dir)
        data_files = find_data_files(game_dir)
        rxdata_files = [file for file in data_files if file.suffix.lower() == ".rxdata"]
        if rxdata_files and _has_po_localization(game_dir):
            return self._unpack_xp_mkxp(game_dir, workspace, rxdata_files)
        if rxdata_files and not archives:
            return self._unpack_xp_mkxp(game_dir, workspace, rxdata_files)
        if archives or data_files:
            self._is_vxace = True
            self._rgss_static = RGSSStatic(game_dir, workspace)
            return self._rgss_static.unpack()

        # MV/MZ: static/runtime scan only. The shared pipeline owns cache,
        # cost checks, batching, and translator selection.
        self._is_mv_mz = True
        try:
            from core.rpgmaker_runtime import scan
            prog = getattr(self, '_progress', None)
            return scan(game_dir, on_progress=prog, manifest=getattr(self, "_manifest", None)) or []
        except ImportError:
            warning("RPG Maker 运行时模块不可用")
            return []

    # ---- Repack (deploy translations) ----

    def repack(self, items: list[TextItem], workspace: Path) -> None:
        if not items:
            return

        po_items = any(item.meta.get("source_kind") == "po_localization" for item in items)
        marshal_items = any(Path(item.file).suffix.lower() in {".rxdata", ".rvdata", ".rvdata2"} for item in items)
        if po_items and marshal_items:
            raise ValueError("RPG Maker checkpoint mixes PO localization and RGSS data routes")
        if po_items:
            self._repack_xp_mkxp(items, workspace)
            return
        if marshal_items:
            game_dir = Path(self._game_dir) if hasattr(self, "_game_dir") else None
            if game_dir is None:
                raise ValueError("RGSS repack requires the actual game directory")
            if self._rgss_static is None or self._rgss_static.game_dir.resolve() != game_dir.resolve():
                self._rgss_static = RGSSStatic(game_dir, workspace)
            self._rgss_static.repack(items, getattr(self, "_manifest", None))
            return

        # MV/MZ: deploy replace mode
        if self._is_mv_mz:
            game_path = Path(self._game_dir) if hasattr(self, '_game_dir') else workspace.parent
            try:
                from core.rpgmaker_runtime import translate_and_deploy
                count = translate_and_deploy(
                    game_path,
                    items,
                    workspace=str(workspace),
                    manifest=getattr(self, "_manifest", None),
                )
                if count > 0:
                    info(f"已部署 {count} 条运行时翻译替换")
            except ImportError:
                warning("RPG Maker 运行时模块不可用")

    def diagnostics_snapshot(self) -> dict:
        if self._rgss_static is None:
            return {}
        return {
            "route": "rgss_static",
            "archive": str(self._rgss_static.archive_path or ""),
            "repack_verification": self._rgss_static.verification,
        }

    # ---- Static RGSS and XP/mkxp localization ----

    def _unpack_vxace(self, game_dir: Path, workspace: Path,
                      rgss3a_files: list[Path], rvdata_files: list[Path]) -> list[TextItem]:
        self._is_vxace = True
        self._game_dir = game_dir
        self._rgss_static = RGSSStatic(game_dir, workspace)
        return self._rgss_static.unpack()

    def _repack_vxace(self, items: list[TextItem], workspace: Path) -> None:
        self.repack(items, workspace)

    def _unpack_xp_mkxp(self, game_dir: Path, workspace: Path,
                        rxdata_files: list[Path]) -> list[TextItem]:
        self._is_xp_mkxp = True
        self._game_dir = game_dir

        items = self._extract_po_localization(game_dir)
        if items:
            info(f"提取到 {len(items)} 条 PO 本地化文本 (RPG Maker XP/mkxp)")
            return items

        self._rgss_static = RGSSStatic(game_dir, workspace)
        return self._rgss_static.unpack()

    def _extract_po_localization(self, game_dir: Path) -> list[TextItem]:
        languages_dir = game_dir / "Languages"
        if not languages_dir.is_dir():
            return []

        source_pos = _find_source_pos(languages_dir)
        if not source_pos:
            return []

        items: list[TextItem] = []
        seen: set[str] = set()
        for source_po in source_pos:
            source_entries = _parse_po_entries(source_po)
            for entry in source_entries.values():
                msgid = entry["msgid"]
                if not msgid or _is_po_meta_key(msgid):
                    continue
                source_text = entry["msgstr"] or msgid
                if not is_translatable(source_text):
                    continue
                dedupe_key = f"{source_po.relative_to(game_dir)}\0{msgid}\0{source_text}"
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                items.append(TextItem(
                    file=str(source_po.relative_to(game_dir)),
                    key=msgid,
                    original=source_text,
                    translated="",
                    context="RPG Maker XP/mkxp PO localization",
                    meta={
                        "source_kind": "po_localization",
                        "msgid": msgid,
                        "source_po": str(source_po.relative_to(game_dir)),
                        "target_po": str(source_po.relative_to(game_dir)),
                        "target_loc": str(source_po.with_suffix(".loc").relative_to(game_dir)),
                    },
                ))
        return items

    def _repack_xp_mkxp(self, items: list[TextItem], workspace: Path) -> None:
        po_items = [
            item for item in items
            if item.meta.get("source_kind") == "po_localization"
            and item.translated and item.translated != item.original
        ]
        if not po_items:
            warning("RPG Maker XP/mkxp: 没有可写入的 PO 译文")
            return

        game_dir = Path(self._game_dir) if hasattr(self, "_game_dir") else None
        if not game_dir:
            raise ValueError("RPG Maker PO repack requires the actual game directory")

        catalogs: dict[str, list[TextItem]] = {}
        for item in po_items:
            relative = safe_relative_name(str(item.meta.get("target_po") or item.file))
            catalogs.setdefault(relative, []).append(item)
        changes: dict[Path, bytes] = {}
        for relative, catalog_items in catalogs.items():
            target_po = game_dir / relative
            translations = {
                str(item.meta.get("msgid") or item.key): item.translated
                for item in catalog_items
            }
            content = target_po.read_text(encoding="utf-8-sig")
            new_content, changed = _rewrite_po_msgstr(content, translations)
            if changed == 0:
                raise ValueError(f"RPG Maker PO checkpoint no longer matches: {relative}")
            changes[target_po] = new_content.encode("utf-8")
            locs: dict[str, dict[str, str]] = {}
            for item in catalog_items:
                loc = safe_relative_name(str(item.meta.get("target_loc") or Path(relative).with_suffix(".loc")))
                locs.setdefault(loc, {})[item.original] = item.translated
            for loc, loc_translations in locs.items():
                target_loc = game_dir / loc
                if target_loc.is_file():
                    original = target_loc.read_bytes()
                    if replace_any_strings_in_marshal_full(original, {}) != original:
                        raise ValueError(f"Unsupported RPG Maker LOC structure: {loc}")
                    changes[target_loc] = replace_any_strings_in_marshal_full(original, loc_translations)
        deploy_changes(game_dir, changes, getattr(self, "_manifest", None))
        info(f"RPG Maker XP/mkxp PO/LOC 写入完成: {len(changes)} 个文件")

    def find_exe(self, path: Path) -> Path | None:
        game_dir = path if path.is_dir() else path.parent
        from core.exe_selector import find_main_exe

        if _has_rxdata(game_dir) and _has_mkxp_runtime(game_dir):
            shim = game_dir / "steamshim.exe"
            if shim.exists():
                return shim

        exe = find_main_exe(game_dir, recursive=False)
        if exe:
            return exe
        www_exe = game_dir / "www"
        return super().find_exe(www_exe) if www_exe.is_dir() else super().find_exe(game_dir)


_PO_META_KEYS = {
    "",
    "POT_VERSION",
    "NAME_SWEARS",
    "NAME_NIKOS",
    "NAMES_LIKE_NIKO",
    "NAMES_LIKE_MOMDAD",
    "NAMES_GROSS",
}


def _has_rxdata(game_dir: Path) -> bool:
    return bool(list(game_dir.glob("Data/*.rxdata")))


def _has_po_localization(game_dir: Path) -> bool:
    lang_dir = game_dir / "Languages"
    if not lang_dir.is_dir():
        return False
    return bool(list(lang_dir.glob("*.po"))) or bool(list(lang_dir.glob("*.loc")))


def _has_mkxp_runtime(game_dir: Path) -> bool:
    names = {p.name.lower() for p in game_dir.iterdir() if p.is_file()}
    if any(name.startswith("x64-vcruntime") and "ruby" in name for name in names):
        return True
    if "sdl2.dll" in names and any(name.endswith(".exe") for name in names):
        return True
    return any(name.startswith("mkxp") for name in names)


def _has_rpgmaker_dirs(game_dir: Path) -> bool:
    required = ("Data", "Graphics", "Audio")
    return all((game_dir / name).is_dir() for name in required)


def _find_source_po(languages_dir: Path) -> Path | None:
    source_pos = _find_source_pos(languages_dir)
    return source_pos[0] if source_pos else None


def _find_source_pos(languages_dir: Path) -> list[Path]:
    preferred = ("ja.po", "jp.po", "japanese.po")
    result: list[Path] = []
    for name in preferred:
        path = languages_dir / name
        if path.is_file():
            result.append(path)
    for subdir in ("internal",):
        nested = languages_dir / subdir
        if not nested.is_dir():
            continue
        for name in preferred:
            path = nested / name
            if path.is_file():
                result.append(path)
    if result:
        return result
    candidates = []
    for po in sorted(languages_dir.glob("*.po")):
        entries = _parse_po_entries(po)
        translatable = sum(
            1 for entry in entries.values()
            if not _is_po_meta_key(entry["msgid"]) and is_translatable(entry["msgstr"] or entry["msgid"])
        )
        candidates.append((translatable, po))
    if not candidates:
        return []
    candidates.sort(key=lambda row: (row[0], row[1].name.lower()), reverse=True)
    return [candidates[0][1]] if candidates[0][0] > 0 else []


def _find_existing_target_po(languages_dir: Path) -> Path | None:
    preferred = ("zh_CN.po", "zh-Hans.po", "zh.po", "cn.po")
    for name in preferred:
        path = languages_dir / name
        if path.is_file():
            return path
    matches = sorted(languages_dir.glob("zh*.po"))
    return matches[0] if matches else None


def _is_po_meta_key(msgid: str) -> bool:
    return msgid in _PO_META_KEYS or msgid.startswith("NAME_") or msgid.startswith("NAMES_")


def _parse_po_entries(path: Path | None) -> dict[str, dict[str, str]]:
    if not path or not path.is_file():
        return {}
    content = path.read_text(encoding="utf-8-sig", errors="replace")
    entries: dict[str, dict[str, str]] = {}
    msgid = ""
    msgstr = ""
    field = ""
    have_entry = False

    def finish():
        nonlocal msgid, msgstr, field, have_entry
        if have_entry:
            entries[msgid] = {"msgid": msgid, "msgstr": msgstr}
        msgid = ""
        msgstr = ""
        field = ""
        have_entry = False

    for raw in content.splitlines():
        line = raw.strip()
        if not line:
            finish()
            continue
        if line.startswith("#"):
            continue
        if line.startswith("msgid "):
            if have_entry:
                finish()
            msgid = _unquote_po(line[6:].strip())
            field = "msgid"
            have_entry = True
        elif line.startswith("msgstr "):
            msgstr = _unquote_po(line[7:].strip())
            field = "msgstr"
            have_entry = True
        elif line.startswith('"'):
            if field == "msgid":
                msgid += _unquote_po(line)
            elif field == "msgstr":
                msgstr += _unquote_po(line)
    finish()
    return entries


def _rewrite_po_msgstr(content: str, translations: dict[str, str]) -> tuple[str, int]:
    lines = content.splitlines()
    output: list[str] = []
    current_msgid = ""
    field = ""
    changed = 0
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("msgid "):
            current_msgid = _unquote_po(stripped[6:].strip())
            field = "msgid"
            output.append(line)
        elif stripped.startswith("msgstr ") and current_msgid in translations:
            output.append(f'msgstr "{_escape_po(translations[current_msgid])}"')
            changed += 1
            field = "msgstr"
            i += 1
            while i < len(lines) and lines[i].strip().startswith('"'):
                i += 1
            continue
        elif stripped.startswith("msgstr "):
            field = "msgstr"
            output.append(line)
        elif stripped.startswith('"') and field == "msgid":
            current_msgid += _unquote_po(stripped)
            output.append(line)
        else:
            output.append(line)
        i += 1
    trailing_newline = "\n" if content.endswith(("\n", "\r\n")) else ""
    return "\n".join(output) + trailing_newline, changed


def _unquote_po(value: str) -> str:
    value = value.strip()
    try:
        return json.loads(value)
    except Exception:
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            value = value[1:-1]
        return value.replace(r"\"", '"').replace(r"\n", "\n").replace(r"\t", "\t").replace(r"\\", "\\")


def _escape_po(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', r"\"").replace("\n", r"\n")


registry.register(RPGMakerEngine())
