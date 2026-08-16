"""KiriKiri runtime capture/display runner.

The default mode is deliberately offline: it preloads local translations and
captures misses to JSONL, but does not call any API unless --live-translate is
explicitly passed.
"""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from pathlib import Path

try:
    import frida
except Exception:
    frida = None

repo_root = Path(__file__).resolve().parent.parent
if (repo_root / "config.py").exists():
    sys.path.insert(0, str(repo_root))

try:
    from config import get_config
    from core.realtime_translator import RealtimeTranslator
    from utils.logger import get_logger, info, setup_logger, warning
except Exception:
    get_config = None
    RealtimeTranslator = None
    _fallback_logger = logging.getLogger("kirikiri_realtime")

    def setup_logger() -> None:
        logging.basicConfig(
            level=logging.INFO,
            format="[%(asctime)s] %(levelname)s: %(message)s",
            datefmt="%H:%M:%S",
        )

    def get_logger() -> logging.Logger:
        return _fallback_logger

    def info(message: str) -> None:
        _fallback_logger.info(message)

    def warning(message: str) -> None:
        _fallback_logger.warning(message)


def _build_translate_fn(src: str, tgt: str):
    import httpx

    if get_config is None:
        raise RuntimeError("live translation requires the full project")

    api_key = get_config().deepseek_api_key or get_config().openai_api_key
    if not api_key:
        raise RuntimeError("DeepSeek API Key is not configured")

    lang = {
        "ja": "Japanese",
        "zh-CN": "Simplified Chinese",
        "zh-TW": "Traditional Chinese",
    }

    def translate_one(text: str) -> str:
        response = httpx.post(
            "https://api.deepseek.com/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": "deepseek-chat",
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            f"Translate Japanese visual novel text to {lang.get(tgt, tgt)}. "
                            "Keep KAG/KiriKiri control tags unchanged. Output only the translation."
                        ),
                    },
                    {"role": "user", "content": text},
                ],
                "temperature": 0.1,
                "max_tokens": 512,
            },
            timeout=30,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"].strip()

    return translate_one


def _load_preload_map(path: Path | None) -> dict[str, str]:
    if not path or not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8-sig"))

    raw_map: dict[str, object] = {}
    if isinstance(data, dict) and isinstance(data.get("translations"), dict):
        raw_map = data["translations"]
    elif isinstance(data, dict) and isinstance(data.get("items"), list):
        for item in data["items"]:
            if not isinstance(item, dict):
                continue
            original = str(item.get("original") or "")
            translated = str(item.get("translated") or "")
            if original and translated:
                raw_map[original] = translated
    elif isinstance(data, dict):
        raw_map = data
    elif isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            original = str(item.get("original") or "")
            translated = str(item.get("translated") or "")
            if original and translated:
                raw_map[original] = translated

    return {str(k): str(v) for k, v in raw_map.items() if k and v and str(k) != str(v)}


def _append_capture(path: Path | None, text: str, source: str) -> None:
    if not path or not text:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "text": text,
                "source": source,
                "ts": time.time(),
            }, ensure_ascii=False) + "\n")
    except Exception as exc:
        warning(f"KiriKiri capture write failed: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description="KiriKiri runtime capture/display hook")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("exe", nargs="?", help="Game executable to launch")
    group.add_argument("--attach", type=int, metavar="PID", help="Attach to an existing process")
    parser.add_argument("--preload", type=Path, help="JSON checkpoint/map of original text to translation")
    parser.add_argument("--capture", type=Path, help="Write missed runtime text to JSONL")
    parser.add_argument("--live-translate", action="store_true", help="Allow API calls for misses")
    parser.add_argument("--no-live-translate", action="store_false", dest="live_translate", help="Compatibility flag; this is the default")
    parser.add_argument("--ansi-render-replace", action="store_true", default=True, help="Rewrite ANSI draw calls through Wide GDI calls")
    parser.add_argument("--no-ansi-render-replace", action="store_false", dest="ansi_render_replace")
    parser.add_argument("--font-create-hook", action="store_true", default=True, help="Rewrite CreateFont* calls to a CJK font")
    parser.add_argument("--no-font-create-hook", action="store_false", dest="font_create_hook")
    parser.add_argument("--font-height-scale", type=float, default=1.0)
    parser.add_argument("--log-file", type=Path, help="Write hook diagnostics to this UTF-8 log file")
    parser.add_argument("--src", default="ja")
    parser.add_argument("--tgt", default="zh-CN")
    args = parser.parse_args()

    setup_logger()
    if frida is None:
        print("Python package 'frida' is not installed. Install it with: pip install frida", file=sys.stderr)
        return 1

    if args.log_file:
        args.log_file.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(args.log_file, encoding="utf-8-sig", mode="w")
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s", datefmt="%H:%M:%S"))
        get_logger().addHandler(handler)
        info(f"KiriKiri hook log: {args.log_file}")

    preload = _load_preload_map(args.preload)
    if preload:
        info(f"预加载 KiriKiri 译文 {len(preload)} 条")
    else:
        info("未发现 KiriKiri 预加载译文；本次将主要捕获运行时文本")

    realtime = None
    if args.live_translate:
        if RealtimeTranslator is None:
            print("Live translation requires the full game-translator project.", file=sys.stderr)
            return 1
        realtime = RealtimeTranslator(_build_translate_fn(args.src, args.tgt), source_lang=args.src, target_lang=args.tgt)
        info("KiriKiri live translation enabled")
    else:
        info("KiriKiri live translation disabled; misses will be captured for offline batch translation")

    device = frida.get_local_device()
    proc = None
    spawned_pid = None
    if args.attach:
        pid = args.attach
        info(f"附加到 PID {pid}")
    else:
        exe = Path(args.exe)
        if not exe.exists():
            print(f"Executable not found: {exe}", file=sys.stderr)
            return 1
        spawned_pid = device.spawn([str(exe)], cwd=str(exe.parent))
        pid = spawned_pid
        info(f"已创建 {exe.name} (PID {pid})，正在安装 KiriKiri hook")

    session = device.attach(pid)
    script_src = Path(__file__).with_name("kirikiri_realtime_hook.js").read_text(encoding="utf-8")
    script_src = (
        "var KIRIKIRI_TRANSLATION_MAP = " + json.dumps(preload, ensure_ascii=False) + ";\n"
        "var KIRIKIRI_ANSI_RENDER_REPLACE = " + json.dumps(args.ansi_render_replace) + ";\n"
        "var KIRIKIRI_FONT_CREATE_HOOK = " + json.dumps(args.font_create_hook) + ";\n"
        "var KIRIKIRI_FONT_HEIGHT_SCALE = " + json.dumps(args.font_height_scale) + ";\n"
        + script_src
    )
    script = session.create_script(script_src)
    captured_seen: set[str] = set()

    def push_result(original: str, translated: str) -> None:
        try:
            script.post({"type": "translations", "map": {original: translated}})
        except Exception:
            pass

    def on_message(message, _data) -> None:
        if message["type"] == "error":
            warning(f"[kirikiri hook] {message}")
            return
        if message["type"] != "send":
            return
        payload = message.get("payload", {})
        kind = payload.get("type")
        if kind == "ready":
            info(f"[kirikiri hook] ready, preload={payload.get('count')}")
        elif kind == "log":
            info(f"[kirikiri hook] {payload.get('msg')}")
        elif kind == "stats":
            info(
                f"[kirikiri hook] replaced={payload.get('replaced')} "
                f"missed={payload.get('missed')} conversions={payload.get('conversions')} "
                f"measure_runs={payload.get('measure_runs', 0)}"
            )
        elif kind == "new_text":
            text = str(payload.get("text", "") or "")
            source = str(payload.get("source", "") or "")
            if not text:
                return
            if text not in captured_seen:
                captured_seen.add(text)
                _append_capture(args.capture, text, source)
            if realtime is not None:
                priority = 1 if len(text) < 30 else 0
                realtime.submit(text, on_result=push_result, priority=priority)

    script.on("message", on_message)
    script.load()
    if spawned_pid is not None:
        device.resume(spawned_pid)
        info(f"已启动 {Path(args.exe).name} (PID {spawned_pid})")
    info("KiriKiri hook 已启动；关闭游戏窗口或 Ctrl+C 停止")

    try:
        while True:
            time.sleep(1)
            if proc is not None and proc.poll() is not None:
                break
            if spawned_pid is not None:
                try:
                    if not any(p.pid == spawned_pid for p in device.enumerate_processes()):
                        break
                except Exception:
                    break
    except KeyboardInterrupt:
        pass
    finally:
        try:
            session.detach()
        except Exception:
            pass

    if args.capture:
        info(f"KiriKiri 本次捕获新文本 {len(captured_seen)} 条: {args.capture}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
