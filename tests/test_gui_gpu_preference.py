from core.gui_gpu_preference import _configure_webview2_d3d11, _version_key


def test_webview2_gpu_flags_request_d3d11_without_overriding_existing_renderer(monkeypatch):
    monkeypatch.delenv("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", raising=False)

    _configure_webview2_d3d11()

    assert "--use-angle=d3d11" in __import__("os").environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"]

    monkeypatch.setenv("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", "--use-angle=vulkan --foo")
    _configure_webview2_d3d11()

    assert __import__("os").environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] == (
        "--use-angle=vulkan --foo --ignore-gpu-blocklist"
    )


def test_webview2_gpu_flag_reports_when_it_was_added(monkeypatch):
    monkeypatch.delenv("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", raising=False)

    assert _configure_webview2_d3d11() is True
    assert _configure_webview2_d3d11() is False
    assert __import__("os").environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"].split().count(
        "--ignore-gpu-blocklist"
    ) == 1


def test_version_key_orders_webview2_versions_numerically():
    assert _version_key("151.0.4129.72") > _version_key("99.0.1.2")
