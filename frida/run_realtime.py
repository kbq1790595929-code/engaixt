"""Real-time translation runner.

Launches a game, injects realtime_hook.js, and pipes captured text
through the translation pipeline (LunaTranslator-style queue + cache).

Usage:
    python frida/run_realtime.py game.exe [--attach PID] [--src ja] [--tgt zh-CN]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import frida

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import get_config
from core.realtime_translator import RealtimeTranslator
from translators.cache import get_cache
from utils.logger import info, warning


def _build_translate_fn(api_key: str, src: str, tgt: str):
    """Return a synchronous text→text function using DeepSeek."""
    import httpx

    def translate_one(text: str) -> str:
        lang_map = {
            "ja": "Japanese", "en": "English", "ko": "Korean",
            "ru": "Russian", "fr": "French", "de": "German",
            "es": "Spanish", "pt": "Portuguese", "it": "Italian",
            "zh-CN": "Simplified Chinese", "zh-TW": "Traditional Chinese",
            "vi": "Vietnamese", "th": "Thai", "ar": "Arabic",
        }
        src_name = lang_map.get(src, src)
        tgt_name = lang_map.get(tgt, tgt)

        resp = httpx.post(
            "https://api.deepseek.com/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": "deepseek-chat",
                "messages": [
                    {"role": "system", "content":
                        f"You are a game translator. Translate {src_name} to {tgt_name}. "
                        "Output only the translation, no explanations."},
                    {"role": "user", "content": text},
                ],
                "temperature": 0.3,
                "max_tokens": 512,
            },
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()

    return translate_one


def main() -> int:
    parser = argparse.ArgumentParser(description="Real-time game translation (LunaTranslator-style)")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("exe", nargs="?", help="Game executable to launch")
    group.add_argument("--attach", type=int, metavar="PID", help="Attach to running process")
    parser.add_argument("--src", default="ja", help="Source language (default: ja)")
    parser.add_argument("--tgt", default="zh-CN", help="Target language (default: zh-CN)")
    parser.add_argument("--preload", type=Path, help="Pre-load translations.json for instant cache hits")
    args = parser.parse_args()

    # Build translation function from config
    try:
        api_key = get_config().deepseek_api_key
    except Exception:
        api_key = ""
    if not api_key:
        print("ERROR: deepseek_api_key not set in config.py", file=sys.stderr)
        return 1

    translate_fn = _build_translate_fn(api_key, args.src, args.tgt)
    pipeline = RealtimeTranslator(translate_fn, source_lang=args.src, target_lang=args.tgt)

    # Pre-load existing translations.json into JS cache
    preload_map: dict = {}
    if args.preload and args.preload.exists():
        try:
            preload_map = json.loads(args.preload.read_text(encoding="utf-8"))
            info(f"Pre-loaded {len(preload_map)} translations from {args.preload}")
        except Exception as e:
            warning(f"Could not load preload file: {e}")

    # Also pull from SQLite cache
    cache = get_cache()

    device = frida.get_local_device()

    # Launch or attach
    if args.attach:
        pid = args.attach
        info(f"Attaching to PID {pid}")
    else:
        exe = Path(args.exe)
        if not exe.exists():
            print(f"Executable not found: {exe}", file=sys.stderr)
            return 1
        proc = subprocess.Popen([str(exe)], cwd=str(exe.parent))
        pid = proc.pid
        time.sleep(1.5)  # let the process initialize
        info(f"Launched {exe.name} (PID {pid})")

    session = device.attach(pid)

    script_src = Path(__file__).with_name("realtime_hook.js").read_text(encoding="utf-8")
    # Inject config + pre-loaded map so JS can filter and replace without Python roundtrip
    script_src = (
        f"var GM_SRC_LANG = '{args.src}';\n"
        f"var GM_TRANSLATION_MAP = {json.dumps(preload_map, ensure_ascii=False)};\n"
        + script_src
    )

    script = session.create_script(script_src)

    def push_result(original: str, translated: str):
        """Send translation result back into the JS script."""
        try:
            script.post({"type": "translations", "map": {original: translated}})
        except Exception:
            pass

    def on_message(message, _data):
        if message["type"] != "send":
            return
        payload = message.get("payload", {})
        kind = payload.get("type")

        if kind == "new_text":
            text = payload.get("text", "")
            if not text:
                return
            # High-priority if short (likely UI); normal otherwise
            priority = 1 if len(text) < 20 else 0
            pipeline.submit(text, on_result=push_result, priority=priority)

        elif kind == "log":
            info(f"[hook] {payload.get('msg')}")

        elif kind == "ready":
            info(f"[hook] ready — hooks: {payload.get('hooks')}")

    script.on("message", on_message)
    script.load()
    info("Realtime translation active. Press Ctrl+C to stop.")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        info("Stopping...")
    finally:
        session.detach()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
