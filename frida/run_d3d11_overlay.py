from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import datetime
from pathlib import Path

import frida

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _default_output() -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path.home() / "Downloads" / ".game_translator" / "hook_captures" / f"d3d11_overlay_{stamp}.json"


def _load_menu_items(checkpoint: Path | None) -> list[dict]:
    """Build hardcoded fallback items from checkpoint translations."""
    translations: dict[str, str] = {}
    if checkpoint and checkpoint.exists():
        data = json.loads(checkpoint.read_text(encoding="utf-8"))
        for raw in data.get("items", []):
            original = str(raw.get("original", "")).strip()
            translated = str(raw.get("translated", "")).strip()
            if original and translated and translated != original:
                translations.setdefault(original, translated)

    def t(original: str, fallback: str) -> str:
        return translations.get(original, fallback)

    return [
        {"key": "PLAY", "text": t("PLAY", "开始游戏"), "x": 0.735, "y": 0.875, "size": 32, "width": 230, "height": 60},
        {"key": "SETTINGS", "text": t("SETTINGS", "设置"), "x": 0.748, "y": 0.955, "size": 28, "width": 240, "height": 58},
        {"key": "CREDITS", "text": t("CREDITS", "制作人员"), "x": 0.748, "y": 1.035, "size": 25, "width": 240, "height": 52},
        {"key": "PATCH NOTES", "text": t("PATCH NOTES", "更新日志"), "x": 0.748, "y": 1.115, "size": 25, "width": 250, "height": 52},
        {"key": "QUIT", "text": t("QUIT", "退出"), "x": 0.748, "y": 1.195, "size": 25, "width": 210, "height": 52},
    ]


def _load_translation_map(checkpoint: Path | None) -> dict[str, str]:
    """Load full translation map from checkpoint JSON for runtime text lookup."""
    mp: dict[str, str] = {}
    if checkpoint and checkpoint.exists():
        data = json.loads(checkpoint.read_text(encoding="utf-8"))
        for raw in data.get("items", []):
            original = str(raw.get("original", "")).strip()
            translated = str(raw.get("translated", "")).strip()
            if original and translated and translated != original:
                mp[original] = translated
    return mp


def _is_process_running(device, pid: int) -> bool:
    for proc in device.enumerate_processes():
        if proc.pid == pid:
            return True
    return False


def _make_script_source(base: str, items: list[dict], title: str,
                        translation_map: dict[str, str] | None = None,
                        internal_draw_hooks: list[dict] | None = None) -> str:
    lines = [
        f"var GM_OVERLAY_ITEMS = {json.dumps(items, ensure_ascii=False)};",
        f"var GM_WINDOW_TITLE = {json.dumps(title, ensure_ascii=False)};",
        f"var GM_TRANSLATION_MAP = {json.dumps(translation_map or {}, ensure_ascii=False)};",
    ]
    if internal_draw_hooks:
        lines.append(f"var GM_INTERNAL_DRAW_HOOKS = {json.dumps(internal_draw_hooks, ensure_ascii=False)};")
    return "\n".join(lines) + "\n" + base


def main() -> int:
    parser = argparse.ArgumentParser(description="Inject a D3D11 DirectWrite CJK overlay.")
    parser.add_argument("exe", type=Path)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--window-title", default="Voidigo")
    parser.add_argument(
        "--duration",
        type=float,
        default=0,
        help="Seconds to keep the hook alive. Use 0 to follow the game process.",
    )
    parser.add_argument("--output", type=Path, default=_default_output())
    args = parser.parse_args()

    if not args.exe.exists():
        print(f"[d3d11-overlay] executable not found: {args.exe}", file=sys.stderr)
        return 2

    script_path = Path(__file__).with_name("d3d11_gdi_overlay.js")
    source = _make_script_source(
        script_path.read_text(encoding="utf-8"),
        _load_menu_items(args.checkpoint),
        args.window_title,
        translation_map=_load_translation_map(args.checkpoint),
    )

    device = frida.get_local_device()
    print(f"[d3d11-overlay] spawning: {args.exe}")
    pid = device.spawn([str(args.exe)], cwd=str(args.exe.parent))
    session = frida.attach(pid)
    script = session.create_script(source)
    events: list[dict] = []

    def on_message(message, data):
        if message["type"] == "send":
            payload = message.get("payload", {})
            events.append(payload)
            kind = payload.get("type")
            if kind in {"stats"}:
                print(f"[d3d11-overlay] stats present={payload.get('present_frames')} draw={payload.get('draw_frames')} capture={payload.get('capture_frames')}/{payload.get('capture_texts',0)} hooks={payload.get('present_hooks')}")
            else:
                print(f"[d3d11-overlay] {payload}")
        elif message["type"] == "error":
            events.append({"type": "frida-error", "message": message})
            print(f"[d3d11-overlay:error] {message.get('description', message)}")

    script.on("message", on_message)
    script.load()
    device.resume(pid)
    print(f"[d3d11-overlay] resumed pid={pid}")
    started = time.time()
    try:
        while True:
            if args.duration and args.duration > 0 and time.time() - started >= args.duration:
                break
            if (not args.duration or args.duration <= 0 or math.isinf(args.duration)) and not _is_process_running(device, pid):
                break
            time.sleep(0.25)
    finally:
        try:
            session.detach()
        except Exception:
            pass
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "exe": str(args.exe),
        "duration_seconds": round(time.time() - started, 2),
        "events": events,
    }
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[d3d11-overlay] saved: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
