"""游戏翻译工具 — 拖拽即翻译

用法:
  python main.py                    # 启动 GUI
  python main.py <路径>             # 命令行模式，直接翻译
  python main.py <路径> --injector xunity  # 使用 XUnity 注入器
"""

from __future__ import annotations

import sys
from pathlib import Path

from core.resources import resource_path
from core.pipeline import Pipeline
from utils.logger import setup_logger, info, error


def main():
    if len(sys.argv) <= 1:
        try:
            from core.app_update import exit_if_update_pending_at_startup

            if exit_if_update_pending_at_startup():
                return
        except Exception:
            pass

    if len(sys.argv) > 1:
        run_cli(sys.argv[1:])
    else:
        run_gui()


def run_overlay_window_cli(args: list[str]):
    """Internal entrypoint used by packaged builds to spawn the subtitle window."""
    from core.translation_overlay_window import run_overlay_window

    game_name = args[0] if len(args) > 0 else "游戏"
    game_exe = args[1] if len(args) > 1 else "GAME.exe"
    game_exe_path = args[2] if len(args) > 2 else ""
    shm_name = args[3] if len(args) > 3 else ""
    run_overlay_window(game_name, game_exe, game_exe_path, shm_name)


def _set_dpi_aware():
    """让窗口在不同 Windows DPI 缩放比例下保持一致。"""
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PerMonitorV2
    except Exception:
        try:
            import ctypes
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

def run_gui():
    """启动 pywebview 新 GUI。"""
    _set_dpi_aware()
    import webview
    sys.path.insert(0, str(Path(__file__).parent))

    from app import Api

    api = Api()
    window = webview.create_window(
        "EngAixt",
        str(resource_path("web", "index.html")),
        js_api=api,
        width=1100,
        height=740,
        resizable=True,
        min_size=(840, 600),
    )
    api.set_window(window)
    webview.start()


def run_realtime_cli(args: list[str]):
    """实时翻译模式（LunaTranslator 风格）。

    用法:
      python main.py <game.exe> --realtime
      python main.py --realtime --attach <PID>
      python main.py <game.exe> --realtime --preload workspace/translations.json
    """
    import argparse

    parser = argparse.ArgumentParser(description="实时翻译模式")
    parser.add_argument("exe", nargs="?", help="游戏可执行文件")
    parser.add_argument("--realtime", action="store_true", help="启用实时翻译")
    parser.add_argument("--attach", type=int, metavar="PID", help="附加到运行中的进程")
    parser.add_argument("--src", default="ja", help="源语言 (默认: ja)")
    parser.add_argument("--tgt", default="zh-CN", help="目标语言 (默认: zh-CN)")
    parser.add_argument("--preload", help="预加载 translations.json 以命中缓存")

    ns = parser.parse_args(args)
    setup_logger()

    # 复用 frida/run_realtime.py 的实现
    sys.path.insert(0, str(resource_path("frida")))
    runner_args = []
    if ns.attach:
        runner_args += ["--attach", str(ns.attach)]
    elif ns.exe:
        runner_args.append(ns.exe)
    else:
        error("实时翻译模式需要指定游戏可执行文件或 --attach PID")
        return
    runner_args += ["--src", ns.src, "--tgt", ns.tgt]
    if ns.preload:
        runner_args += ["--preload", ns.preload]

    import run_realtime
    old_argv = sys.argv
    sys.argv = ["run_realtime.py"] + runner_args
    try:
        run_realtime.main()
    finally:
        sys.argv = old_argv


def run_cli(args: list[str]):
    import argparse

    if args and args[0] == "--overlay-window":
        return run_overlay_window_cli(args[1:])

    # 实时翻译模式（LunaTranslator 风格）—— 独立分支，提前拦截
    if "--realtime" in args:
        return run_realtime_cli(args)

    parser = argparse.ArgumentParser(description="游戏翻译工具")
    parser.add_argument("path", nargs="?", help="游戏文件夹或压缩包路径")
    parser.add_argument("--injector", choices=["xunity", "frida"], help="使用运行时注入器")
    parser.add_argument("--no-launch", action="store_true", help="不启动游戏")
    try:
        from translators.factory import translator_choices

        translator_options = translator_choices()
    except Exception:
        translator_options = ["openai", "anthropic", "deepseek"]
    parser.add_argument("--translator", choices=translator_options, help="翻译器选择")
    parser.add_argument("--target-lang", default="zh-CN", help="目标语言 (默认: zh-CN)")
    parser.add_argument("--coverage", type=int, default=100, help="翻译覆盖百分比 10-100 (默认: 100)")
    parser.add_argument("--cjk-font", help="CJK 中文字体 TTF 路径")
    parser.add_argument("--files", default="", help="只翻译匹配的文件（逗号分隔，支持通配符）")
    parser.add_argument("--extract-only", action="store_true",
                        help="仅提取文本到 JSON 检查点，不翻译不启动")
    parser.add_argument("--from-json", default="",
                        help="从 JSON 检查点回填翻译，跳过提取和翻译")
    parser.add_argument("--polish", action="store_true",
                        help="polish existing checkpoint translations, then patch from JSON")
    parser.add_argument("--polish-budget", type=float, default=1.0,
                        help="polish budget in CNY, default 1.0")
    parser.add_argument("--checkpoint", action="store_true",
                        help="使用 JSON 检查点管线（提取→翻译→回填，支持断点续传）")

    ns = parser.parse_args(args)
    if not ns.path:
        parser.error("path is required")

    setup_logger()
    from core.path_resolver import resolve_game_path

    ns.path = resolve_game_path(ns.path)
    info(f"命令行模式: {ns.path}")

    from config import get_config, save_config
    config = get_config()
    if ns.translator:
        config.active_translator = ns.translator
    if ns.target_lang:
        config.target_lang = ns.target_lang
    if ns.coverage:
        config.translation_coverage = ns.coverage
    if ns.cjk_font:
        config.cjk_font_path = ns.cjk_font

    pipeline = Pipeline()

    # 检查点管线模式
    if ns.polish:
        success = pipeline.polish_checkpoint(
            ns.path,
            checkpoint_json=ns.from_json,
            budget_cny=ns.polish_budget,
            launch=not ns.no_launch,
            injector=ns.injector,
        )
    elif ns.checkpoint or ns.extract_only or ns.from_json:
        success = pipeline.run_with_checkpoint(
            ns.path,
            launch=not ns.no_launch,
            injector=ns.injector,
            file_filter=ns.files.split(",") if ns.files else None,
            resume_json=ns.from_json,
            extract_only=ns.extract_only,
            patch_only=bool(ns.from_json),
        )
    else:
        success = pipeline.run(
            ns.path,
            launch=not ns.no_launch,
            injector=ns.injector,
            file_filter=ns.files.split(",") if ns.files else None,
        )
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
