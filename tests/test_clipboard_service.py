from core import clipboard_service


def test_clipboard_service_uses_windows_backend(monkeypatch):
    monkeypatch.setattr(clipboard_service.sys, "platform", "win32")
    monkeypatch.setattr(clipboard_service, "_get_windows_clipboard_text", lambda: "sample")
    writes = []
    monkeypatch.setattr(
        clipboard_service,
        "_set_windows_clipboard_text",
        lambda text: writes.append(text) or True,
    )

    assert clipboard_service.get_clipboard_text() == "sample"
    assert clipboard_service.set_clipboard_text("translated") is True
    assert writes == ["translated"]


def test_clipboard_service_returns_safe_defaults_on_failure(monkeypatch):
    monkeypatch.setattr(clipboard_service.sys, "platform", "win32")
    monkeypatch.setattr(
        clipboard_service,
        "_get_windows_clipboard_text",
        lambda: (_ for _ in ()).throw(OSError("clipboard busy")),
    )
    monkeypatch.setattr(
        clipboard_service,
        "_set_windows_clipboard_text",
        lambda _text: (_ for _ in ()).throw(OSError("clipboard busy")),
    )

    assert clipboard_service.get_clipboard_text() == ""
    assert clipboard_service.set_clipboard_text("text") is False
