from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.exe_selector import find_main_exe  # noqa: E402
from core.path_resolver import resolve_game_path  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


CONTROL_RE = re.compile(
    r"\\[A-Za-z]{1,3}\s*\[[^\]]*\]"
    r"|\\[\.\|!>\^<>{}]"
    r"|\\FS\[\d+\]"
    r"|\\[A-Za-z]+",
    re.IGNORECASE,
)
FACE_TAG_RE = re.compile(r"^@[A-Za-z0-9_]+\s*")


def _default_output() -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path.home() / "Downloads" / ".game_translator" / "hook_captures" / f"rpgmaker_mkxp_{stamp}.json"


def _resolve_target(target: str) -> tuple[Path, Path]:
    resolved = Path(resolve_game_path(target))
    if resolved.is_file() and resolved.suffix.lower() == ".exe":
        return resolved.parent, resolved

    game_dir = resolved if resolved.is_dir() else resolved.parent
    shim = game_dir / "steamshim.exe"
    if shim.exists():
        return game_dir, shim
    exe = find_main_exe(game_dir, recursive=False) or find_main_exe(game_dir, recursive=True)
    if exe is None:
        raise FileNotFoundError(f"No executable found under {game_dir}")
    return game_dir, exe


def _clean_visible(text: str) -> str:
    text = str(text or "").replace("\\n", "\n")
    cleaned: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        while line.startswith("$"):
            line = line[1:].lstrip()
        while True:
            new_line = FACE_TAG_RE.sub("", line)
            if new_line == line:
                break
            line = new_line.lstrip()
        line = CONTROL_RE.sub("", line).strip()
        if line:
            cleaned.append(line)
    return "\n".join(cleaned)


def _add_unique(mapping: dict[str, str], key: str, value: str, *, override: bool = False) -> None:
    key = str(key or "").strip()
    value = str(value or "").strip()
    if not key or not value or key == value:
        return
    if override:
        mapping[key] = value
    else:
        mapping.setdefault(key, value)


def _add_translation_variants(mapping: dict[str, str], original: str, translated: str, *, override: bool = False) -> None:
    _add_unique(mapping, original, translated, override=override)

    clean_original = _clean_visible(original)
    clean_translated = _clean_visible(translated)
    _add_unique(mapping, clean_original, clean_translated, override=override)

    src_lines = [line.strip() for line in clean_original.splitlines() if line.strip()]
    dst_lines = [line.strip() for line in clean_translated.splitlines() if line.strip()]
    if len(src_lines) == len(dst_lines):
        for src, dst in zip(src_lines, dst_lines):
            _add_unique(mapping, src, dst, override=override)


def _po_unquote(value: str) -> str:
    try:
        decoded = ast.literal_eval(value)
        return decoded if isinstance(decoded, str) else ""
    except Exception:
        value = value.strip()
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            value = value[1:-1]
        return value.replace(r"\"", '"').replace(r"\n", "\n").replace(r"\t", "\t").replace(r"\\", "\\")


def _parse_po(path: Path) -> dict[str, str]:
    entries: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except Exception:
        return entries

    msgid_parts: list[str] = []
    msgstr_parts: list[str] = []
    state: str | None = None

    def flush() -> None:
        nonlocal msgid_parts, msgstr_parts, state
        msgid = "".join(msgid_parts)
        msgstr = "".join(msgstr_parts)
        if msgid and msgstr:
            entries.setdefault(msgid, msgstr)
        msgid_parts = []
        msgstr_parts = []
        state = None

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            flush()
            continue
        if line.startswith("#") or line.startswith("msgctxt "):
            continue
        if line.startswith("msgid "):
            flush()
            msgid_parts = [_po_unquote(line[6:].strip())]
            state = "msgid"
            continue
        if line.startswith("msgstr "):
            msgstr_parts = [_po_unquote(line[7:].strip())]
            state = "msgstr"
            continue
        if line.startswith('"'):
            if state == "msgid":
                msgid_parts.append(_po_unquote(line))
            elif state == "msgstr":
                msgstr_parts.append(_po_unquote(line))
            continue
        flush()
    flush()
    return entries


def _preferred_po_target(language_dir: Path, target_locale: str) -> Path | None:
    candidates = [
        target_locale,
        target_locale.replace("-", "_"),
        "zh_CN",
        "zh-Hans",
        "zh",
        "zh_CHT",
    ]
    seen: set[str] = set()
    for name in candidates:
        if not name or name in seen:
            continue
        seen.add(name)
        path = language_dir / f"{name}.po"
        if path.exists():
            return path
    return None


def _po_source_sort_key(path: Path, source_locale: str | None) -> tuple[int, str]:
    stem = path.stem
    preferred = [source_locale, "ja", "en"] if source_locale else ["ja", "en"]
    for index, name in enumerate(preferred):
        if name and stem.lower() == name.lower():
            return index, stem.lower()
    return len(preferred), stem.lower()


def _load_po_translation_map(
    game_dir: Path,
    *,
    target_locale: str = "zh_CN",
    source_locale: str | None = None,
) -> dict[str, str]:
    mapping: dict[str, str] = {}
    language_dirs = [game_dir / "Languages", game_dir / "Languages" / "internal"]
    target_names = {
        target_locale.lower(),
        target_locale.replace("-", "_").lower(),
        "zh_cn",
        "zh-hans",
        "zh",
        "zh_cht",
    }

    for language_dir in language_dirs:
        if not language_dir.is_dir():
            continue
        target_path = _preferred_po_target(language_dir, target_locale)
        if not target_path:
            continue
        target_entries = _parse_po(target_path)
        if not target_entries:
            continue

        for msgid, translated in target_entries.items():
            _add_translation_variants(mapping, msgid, translated, override=False)

        allowed_sources: set[str] | None = None
        if source_locale:
            normalized = source_locale.replace("-", "_").lower()
            allowed_sources = {normalized}
            if "_" in normalized:
                allowed_sources.add(normalized.split("_", 1)[0])
        source_paths = [
            path for path in language_dir.glob("*.po")
            if path.stem.lower() not in target_names
        ]
        for source_path in sorted(source_paths, key=lambda path: _po_source_sort_key(path, source_locale)):
            if allowed_sources is not None and source_path.stem.replace("-", "_").lower() not in allowed_sources:
                continue
            source_entries = _parse_po(source_path)
            for msgid, source_text in source_entries.items():
                translated = target_entries.get(msgid)
                if not translated:
                    continue
                _add_translation_variants(mapping, source_text, translated, override=True)
    return mapping


def _load_translation_map(
    checkpoint: Path | None,
    game_dir: Path,
    *,
    target_locale: str = "zh_CN",
    source_locale: str | None = None,
) -> dict[str, str]:
    mapping: dict[str, str] = {}
    if checkpoint and checkpoint.exists():
        data = json.loads(checkpoint.read_text(encoding="utf-8"))
        for raw in data.get("items", []):
            original = str(raw.get("original", "")).strip()
            translated = str(raw.get("translated", "")).strip()
            if not original or not translated or original == translated:
                continue
            _add_translation_variants(mapping, original, translated)

    # Gettext-style games often already ship aligned PO catalogs. When the
    # player runs a non-Chinese locale, aligning source msgstr values to the
    # target PO avoids brittle OCR/hard-coded single-game fixes and also
    # overrides polluted checkpoint rows.
    po_mapping = _load_po_translation_map(
        game_dir,
        target_locale=target_locale,
        source_locale=source_locale,
    )
    mapping.update(po_mapping)
    _apply_runtime_overrides(mapping, checkpoint, game_dir)

    # Minimal safety net for first-screen verification when a partial checkpoint
    # is used. Full pipeline translations override these through setdefault.
    for src, dst in {
        "スタート": "开始",
        "設定": "设置",
        "終了": "退出",
        "ロード": "读取",
        "セーブ": "保存",
        "やり直す？": "重新开始？",
    }.items():
        mapping.setdefault(src, dst)
    return mapping


def _iter_runtime_override_pairs(payload: object):
    if isinstance(payload, dict):
        for key, value in payload.items():
            if isinstance(value, str):
                yield str(key), value
            elif isinstance(value, dict):
                original = value.get("original") or value.get("source") or key
                translated = value.get("translated") or value.get("target") or value.get("text")
                if translated:
                    yield str(original), str(translated)
    elif isinstance(payload, list):
        for row in payload:
            if not isinstance(row, dict):
                continue
            original = row.get("original") or row.get("source")
            translated = row.get("translated") or row.get("target") or row.get("text")
            if original and translated:
                yield str(original), str(translated)


def _load_runtime_override_file(path: Path) -> object | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if isinstance(data, dict) and "runtime_overrides" in data:
        return data.get("runtime_overrides")
    return data


def _apply_runtime_overrides(mapping: dict[str, str], checkpoint: Path | None, game_dir: Path) -> None:
    payloads: list[object] = []
    if checkpoint and checkpoint.exists():
        try:
            data = json.loads(checkpoint.read_text(encoding="utf-8"))
            payloads.append(data.get("runtime_overrides", {}))
        except Exception:
            pass
        for name in ("runtime_overrides.json", "translation_overrides.json"):
            payload = _load_runtime_override_file(checkpoint.parent / name)
            if payload is not None:
                payloads.append(payload)

    for name in (".game_translator_runtime_overrides.json", "runtime_overrides.json"):
        payload = _load_runtime_override_file(game_dir / name)
        if payload is not None:
            payloads.append(payload)

    for payload in payloads:
        for original, translated in _iter_runtime_override_pairs(payload):
            _add_translation_variants(mapping, original, translated, override=True)


def _make_script_source(
    base: str,
    translation_map: dict[str, str],
    cjk_font_path: Path | None,
    cjk_font_names: list[str] | None = None,
    debug_log: bool = False,
) -> str:
    options = {
        "max_read_chars": 512,
        "event_limit": 400,
        "cjk_font_path": str(cjk_font_path) if cjk_font_path else "",
        "cjk_font_names": cjk_font_names or [],
        "debug_log": debug_log,
    }
    prefix = [
        "var RPGMAKER_MKXP_TRANSLATION_MAP = "
        + json.dumps(translation_map, ensure_ascii=False, separators=(",", ":"))
        + ";",
        "var RPGMAKER_MKXP_OPTIONS = "
        + json.dumps(options, ensure_ascii=False, separators=(",", ":"))
        + ";",
    ]
    return "\n".join(prefix) + "\n" + base


def _find_cjk_font(game_dir: Path) -> Path | None:
    fonts_dir = game_dir / "Fonts"
    preferred = [
        "wqy-microhei.ttf",
        "SourceHanSansHWSC-Regular.otf",
        "NotoSansCJK-Regular.ttc",
        "NotoSansSC-Regular.otf",
    ]
    for name in preferred:
        path = fonts_dir / name
        if path.exists():
            return path
    if fonts_dir.is_dir():
        for pattern in ("*SourceHan*.*", "*Noto*SC*.*", "*wqy*.*", "*MicroHei*.*"):
            hits = sorted(fonts_dir.glob(pattern))
            if hits:
                return hits[0]
    return None


def _cjk_font_names(cjk_font_path: Path | None) -> list[str]:
    names = [
        "WenQuanYi Micro Hei",
        "Source Han Sans HW SC",
        "Source Han Sans SC",
        "Noto Sans CJK SC",
        "Noto Sans SC",
    ]
    if cjk_font_path:
        stem = cjk_font_path.stem
        names.insert(0, stem)
        if stem.lower() == "wqy-microhei":
            names.insert(0, "WenQuanYi Micro Hei")
        if "SourceHanSansHWSC" in stem:
            names.insert(0, "Source Han Sans HW SC")
    deduped: list[str] = []
    for name in names:
        if name and name not in deduped:
            deduped.append(name)
    return deduped


def _is_process_running(device, pid: int) -> bool:
    for proc in device.enumerate_processes():
        if proc.pid == pid:
            return True
    return False


def main() -> int:
    import frida

    parser = argparse.ArgumentParser(description="Run RPG Maker XP/mkxp SDL_ttf translation hook.")
    parser.add_argument("target", help="Game directory or executable.")
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--duration", type=float, default=0.0, help="Seconds to keep hook alive; 0 follows the game.")
    parser.add_argument("--output", type=Path, default=_default_output())
    parser.add_argument("--debug-log", action="store_true", help="Write Ruby Language.tr hits to gt_runtime_hook.log.")
    parser.add_argument("--target-locale", default="zh_CN", help="Preferred target PO locale when local catalogs exist.")
    parser.add_argument("--source-locale", default=None, help="Preferred source PO locale, e.g. ja.")
    args = parser.parse_args()

    game_dir, entry_exe = _resolve_target(args.target)
    translation_map = _load_translation_map(
        args.checkpoint,
        game_dir,
        target_locale=args.target_locale,
        source_locale=args.source_locale,
    )
    cjk_font_path = _find_cjk_font(game_dir)
    cjk_font_names = _cjk_font_names(cjk_font_path)

    hook_js = Path(__file__).with_name("rpgmaker_mkxp_ttf_hook.js").read_text(encoding="utf-8")
    source = _make_script_source(
        hook_js,
        translation_map,
        cjk_font_path,
        cjk_font_names=cjk_font_names,
        debug_log=args.debug_log,
    )
    device = frida.get_local_device()
    events: list[dict] = []
    child_pids: list[int] = []
    sessions = []
    lock = threading.Lock()

    def record(payload: dict) -> None:
        with lock:
            events.append(payload)
        kind = payload.get("type", "")
        if kind in {"ready", "hit", "stats", "child-added", "attached"}:
            print(f"[rpgmaker-hook] {json.dumps(payload, ensure_ascii=False)}", flush=True)

    def attach_hook(pid: int, label: str):
        session = device.attach(pid)
        script = session.create_script(source)

        def on_message(message, data):
            if message["type"] == "send":
                payload = dict(message.get("payload", {}))
                payload["label"] = label
                record(payload)
            else:
                record({"type": "frida-error", "label": label, "message": message})

        script.on("message", on_message)
        script.load()
        sessions.append(session)
        record({"type": "attached", "pid": pid, "label": label})
        return session

    def on_child_added(child):
        pid = int(getattr(child, "pid", 0) or 0)
        child_path = str(getattr(child, "path", "") or "")
        argv = list(getattr(child, "argv", []) or [])
        record({"type": "child-added", "pid": pid, "path": child_path, "argv": argv})
        if pid <= 0:
            return
        child_pids.append(pid)
        try:
            attach_hook(pid, "child")
        except Exception as exc:
            record({"type": "attach-error", "pid": pid, "message": repr(exc)})
        try:
            device.resume(pid)
        except Exception as exc:
            record({"type": "resume-error", "pid": pid, "message": repr(exc)})

    print(f"[rpgmaker-hook] spawning {entry_exe}", flush=True)
    pid = device.spawn([str(entry_exe)], cwd=str(entry_exe.parent))
    parent_session = device.attach(pid)
    sessions.append(parent_session)

    use_child_gating = entry_exe.name.lower() == "steamshim.exe"
    if use_child_gating:
        device.on("child-added", on_child_added)
        parent_session.enable_child_gating()
    else:
        attach_hook(pid, "main")

    device.resume(pid)
    started = time.time()
    try:
        while True:
            elapsed = time.time() - started
            if args.duration and args.duration > 0 and elapsed >= args.duration:
                break
            watched = child_pids if child_pids else [pid]
            if (not args.duration or args.duration <= 0) and watched and not any(_is_process_running(device, p) for p in watched):
                break
            time.sleep(0.25)
    finally:
        for session in reversed(sessions):
            try:
                session.detach()
            except Exception:
                pass
        if args.duration and args.duration > 0:
            for child_pid in child_pids:
                try:
                    device.kill(child_pid)
                except Exception:
                    pass
            try:
                device.kill(pid)
            except Exception:
                pass

    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "game_dir": str(game_dir),
        "entry_exe": str(entry_exe),
        "checkpoint": str(args.checkpoint) if args.checkpoint else "",
        "cjk_font_path": str(cjk_font_path) if cjk_font_path else "",
        "cjk_font_names": cjk_font_names,
        "map_size": len(translation_map),
        "duration_seconds": round(time.time() - started, 2),
        "child_pids": child_pids,
        "events": events,
    }
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[rpgmaker-hook] saved {args.output}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
