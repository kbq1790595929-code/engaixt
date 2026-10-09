from pathlib import Path

from core.kirikiri_runtime_diagnostics import KirikiriRuntimeDiagnostics


def test_runtime_diagnostics_reports_child_hook_and_display(tmp_path: Path) -> None:
    messages: list[tuple[int, str]] = []
    diagnostics = KirikiriRuntimeDiagnostics(tmp_path, lambda level, text: messages.append((level, text)))

    diagnostics.consume("child_detected pid=42 parent=7 exe=C:\\game\\Nemophilia.exe")
    diagnostics.consume("hook_ready pid=42 exe=C:\\game\\Nemophilia.exe")
    diagnostics.consume("render replaced=0 text_captures=3 overlay_writes=3")
    diagnostics.consume("text_displayed count=1 original='日本語' translated='中文'")

    text = "\n".join(message for _level, message in messages)
    assert "跟踪游戏子进程" in text
    assert "DLL 注入与初始化成功" in text
    assert "已捕获游戏文本" in text
    assert "已显示中文译文" in text


def test_runtime_diagnostics_explains_signature_miss_instead_of_generic_finish(tmp_path: Path) -> None:
    messages: list[tuple[int, str]] = []
    diagnostics = KirikiriRuntimeDiagnostics(tmp_path, lambda level, text: messages.append((level, text)))

    diagnostics.consume("internal text hook signatures not found yet")
    diagnostics.consume("hook_ready pid=42 exe=C:\\game\\Nemophilia.exe")
    assert diagnostics.finish(0) is True

    text = "\n".join(message for _level, message in messages)
    assert "未匹配文本 hook 签名" in text
    assert "实时翻译会话正常结束" not in text


def test_runtime_diagnostics_reports_injection_error_and_code(tmp_path: Path) -> None:
    messages = []
    diagnostics = KirikiriRuntimeDiagnostics(tmp_path, lambda level, text: messages.append((level, text)))
    diagnostics.consume("injection_failed pid=42 win32=5")

    assert diagnostics.finish(2) is False
    assert any("pid=42 win32=5" in text and level >= 40 for level, text in messages)


def test_runtime_diagnostics_only_reads_new_complete_session_lines(tmp_path: Path) -> None:
    path = tmp_path / "kirikiri_overlay.log"
    path.write_text("text_displayed count=99\n", encoding="utf-8")
    messages = []
    diagnostics = KirikiriRuntimeDiagnostics(tmp_path, lambda level, text: messages.append((level, text)))
    diagnostics.poll()
    assert diagnostics.displays == 0
    with path.open("ab") as stream:
        stream.write("text_displayed count=1 translated='中文'".encode("utf-8")[:-1])
    diagnostics.poll()
    assert diagnostics.displays == 0
    with path.open("ab") as stream:
        stream.write(b"'\n")
    diagnostics.poll()
    assert diagnostics.displays == 1


def test_runtime_diagnostics_distinguishes_capture_without_translation(tmp_path: Path) -> None:
    messages = []
    diagnostics = KirikiriRuntimeDiagnostics(tmp_path, lambda level, text: messages.append((level, text)))
    diagnostics.consume("hook_ready pid=42 exe=C:\\game\\game.exe")
    diagnostics.consume("internal_hooks=2 text_captures=12")
    assert diagnostics.finish(0) is True
    assert any("未确认字幕显示译文" in text for level, text in messages)


def test_runtime_diagnostics_reports_game_crash_even_if_launcher_returns_zero(tmp_path: Path) -> None:
    messages = []
    diagnostics = KirikiriRuntimeDiagnostics(tmp_path, lambda level, text: messages.append((level, text)))
    diagnostics.consume("hook_ready pid=42 exe=C:\\game\\game.exe")
    diagnostics.consume("process_exit pid=42 exit_code=3221225477")
    assert diagnostics.finish(0) is False
    assert any("3221225477" in text and "异常退出" in text for level, text in messages)


def test_runtime_diagnostics_relays_overlay_python_startup_traceback(tmp_path: Path) -> None:
    messages = []
    diagnostics = KirikiriRuntimeDiagnostics(tmp_path, lambda level, text: messages.append((level, text)))
    (tmp_path / "kirikiri_overlay_stderr.log").write_text(
        "Traceback (most recent call last):\nModuleNotFoundError: No module named 'tkinter'\n",
        encoding="utf-8",
    )
    diagnostics.poll()
    assert any("ModuleNotFoundError" in text and level >= 40 for level, text in messages)
    assert diagnostics.finish(0) is False


def test_runtime_diagnostics_does_not_mark_overlay_info_as_error(tmp_path: Path) -> None:
    messages = []
    diagnostics = KirikiriRuntimeDiagnostics(tmp_path, lambda level, text: messages.append((level, text)))
    (tmp_path / "kirikiri_overlay_stderr.log").write_text(
        "[02:00:00] INFO: overlay started\n",
        encoding="utf-8",
    )
    diagnostics.poll()
    assert diagnostics.errors == []
    assert any(level == 20 and "overlay started" in text for level, text in messages)


def test_runtime_diagnostics_reports_waiting_capture_while_game_still_runs(tmp_path, monkeypatch) -> None:
    messages = []
    monkeypatch.setattr("core.kirikiri_runtime_diagnostics.time.monotonic", lambda: 0.0)
    diagnostics = KirikiriRuntimeDiagnostics(tmp_path, lambda level, text: messages.append((level, text)))
    diagnostics.consume("hook_ready pid=42 exe=C:\\game\\game.exe")
    monkeypatch.setattr("core.kirikiri_runtime_diagnostics.time.monotonic", lambda: 46.0)
    diagnostics.check_waiting()
    diagnostics.check_waiting()
    assert sum("尚未捕获文本" in text for level, text in messages) == 1
