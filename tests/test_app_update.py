from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from core import app_update
from app import Api


ROOT = Path(__file__).resolve().parents[1]


def test_app_update_version_comparison():
    assert app_update.is_newer_version("1.0.1", "1.0.0")
    assert app_update.is_newer_version("1.1.0", "1.0.9")
    assert not app_update.is_newer_version("1.0.0", "1.0.0")
    assert not app_update.is_newer_version("0.9.9", "1.0.0")


def test_app_update_prefers_runtime_version_marker(tmp_path, monkeypatch):
    marker = tmp_path / "app_version.json"
    marker.write_text(json.dumps({"app": "EngAixt", "version": "1.0.8"}), encoding="utf-8")
    monkeypatch.delenv("ENGAIXT_APP_VERSION", raising=False)
    monkeypatch.setattr(app_update, "_app_version_marker_paths", lambda: [marker])

    assert app_update.current_app_info()["version"] == "1.0.8"


def test_app_update_check_uses_runtime_version_marker(tmp_path, monkeypatch):
    marker = tmp_path / "app_version.json"
    marker.write_text(json.dumps({"app": "EngAixt", "version": "1.0.8"}), encoding="utf-8")
    monkeypatch.delenv("ENGAIXT_APP_VERSION", raising=False)
    monkeypatch.setattr(app_update, "_app_version_marker_paths", lambda: [marker])
    monkeypatch.setattr(app_update, "_read_json_url", lambda url, timeout: {"version": "1.0.8"})

    status = app_update.check_update(timeout=1)

    assert status["version"] == "1.0.8"
    assert status["latest_version"] == "1.0.8"
    assert status["update_available"] is False
    assert status["manifest"] == {}


def test_app_update_does_not_offer_older_online_version(tmp_path, monkeypatch):
    marker = tmp_path / "app_version.json"
    marker.write_text(json.dumps({"app": "EngAixt", "version": "1.0.5"}), encoding="utf-8")
    monkeypatch.delenv("ENGAIXT_APP_VERSION", raising=False)
    monkeypatch.setattr(app_update, "_app_version_marker_paths", lambda: [marker])
    monkeypatch.setattr(app_update, "_read_json_url", lambda url, timeout: {"version": "1.0.4"})

    status = app_update.check_update(timeout=1)

    assert status["version"] == "1.0.5"
    assert status["latest_version"] == "1.0.4"
    assert status["update_available"] is False
    assert status["manifest"] == {}


def test_unified_update_read_error_is_not_hidden_as_manual_update(tmp_path, monkeypatch):
    marker = tmp_path / "app_version.json"
    marker.write_text(
        json.dumps({"app": "EngAixt", "version": "1.0.6", "edition": "unlimited"}),
        encoding="utf-8",
    )
    monkeypatch.delenv("ENGAIXT_APP_VERSION", raising=False)
    monkeypatch.setattr(app_update, "_app_version_marker_paths", lambda: [marker])

    def fake_read_json_url(url, timeout):
        raise app_update.AppUpdateError("update JSON is invalid")

    monkeypatch.setattr(app_update, "_read_json_url", fake_read_json_url)

    with pytest.raises(app_update.AppUpdateError, match="update JSON is invalid"):
        app_update.check_update(timeout=1)


def test_update_uses_unified_update_manifest(monkeypatch):
    captured = []
    monkeypatch.setattr(
        app_update,
        "_read_json_url",
        lambda url, timeout: captured.append(url) or {"version": app_update.APP_VERSION},
    )

    app_update.check_update(timeout=1)

    # One package, one update stream: no edition is part of the manifest path.
    assert captured == ["https://engaixt.com/updates/latest.json"]


def test_app_update_json_read_retries_empty_response(monkeypatch):
    calls = {"count": 0}
    responses = [b"", b'{"version":"1.0.9"}']

    def fake_read_bytes(url, timeout):
        calls["count"] += 1
        return responses.pop(0)

    monkeypatch.setattr(app_update, "_read_bytes_url", fake_read_bytes)
    monkeypatch.setattr(app_update.time, "sleep", lambda _seconds: None)

    assert app_update._read_json_url("https://example.test/latest.json", timeout=1) == {"version": "1.0.9"}
    assert calls["count"] == 2


def test_app_update_json_read_reports_clear_error_for_non_json(monkeypatch):
    monkeypatch.setattr(app_update, "_read_bytes_url", lambda url, timeout: b"<html>not json</html>")
    monkeypatch.setattr(app_update.time, "sleep", lambda _seconds: None)

    with pytest.raises(app_update.AppUpdateError) as exc:
        app_update._read_json_url("https://example.test/latest.json", timeout=1)

    message = str(exc.value)
    assert "update JSON is invalid" in message
    assert "https://example.test/latest.json" in message
    assert "Expecting value" not in message


def test_app_update_part_paths_are_resolved_from_site_root():
    url = app_update._resolve_package_part_url(
        "https://engaixt.com/downloads/EngAixt-Windows-Trial.zip.manifest.json",
        "downloads/EngAixt-Windows-Trial.zip.parts/part-000.bin",
    )

    assert url == "https://engaixt.com/downloads/EngAixt-Windows-Trial.zip.parts/part-000.bin"


def test_app_update_part_paths_can_be_relative_to_package_manifest():
    url = app_update._resolve_package_part_url(
        "https://engaixt.pages.dev/downloads/EngAixt-Windows-Trial.zip.manifest.json",
        "EngAixt-Windows-Trial.zip.parts/part-000.bin",
    )

    assert url == "https://engaixt.pages.dev/downloads/EngAixt-Windows-Trial.zip.parts/part-000.bin"


def test_app_update_download_retries_transient_timeout(monkeypatch):
    calls = {"count": 0}

    class FakeResponse:
        def __init__(self):
            self._sent = False

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self, size=-1):
            if self._sent:
                return b""
            self._sent = True
            return b"ok"

    def fake_urlopen(req, timeout):
        calls["count"] += 1
        if calls["count"] == 1:
            raise socket.timeout("timed out")
        return FakeResponse()

    monkeypatch.setattr(app_update.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(app_update.time, "sleep", lambda _seconds: None)

    assert app_update._read_bytes_url("https://example.test/file.bin", timeout=1) == b"ok"
    assert calls["count"] == 2


def test_app_update_reuses_verified_existing_package(tmp_path, monkeypatch):
    payload = b"existing update zip"
    expected_sha = hashlib.sha256(payload).hexdigest()
    dest = tmp_path / "EngAixt-Windows-Trial-1.1.0.zip"
    dest.write_bytes(payload)
    stale_fixed_part = tmp_path / "EngAixt-Windows-Trial-1.1.0.zip.part"
    stale_fixed_part.write_bytes(b"locked by previous run")

    monkeypatch.setattr(app_update, "_updates_dir", lambda: tmp_path)
    monkeypatch.setattr(
        app_update,
        "_read_json_url",
        lambda *args, **kwargs: {
            "filename": "EngAixt-Windows-Trial.zip",
            "size": len(payload),
            "sha256": expected_sha,
            "parts": [{"path": "downloads/pkg.parts/part-000.bin", "size": len(payload)}],
        },
    )

    def fail_download(*args, **kwargs):
        raise AssertionError("existing verified package should be reused")

    monkeypatch.setattr(app_update, "_download_part_to_path", fail_download)

    result = app_update.download_update_package(
        {
            "version": "1.1.0",
            "package_manifest_url": "https://engaixt.com/downloads/EngAixt-Windows-Trial.zip.manifest.json",
        }
    )

    assert result == dest
    assert dest.read_bytes() == payload
    assert stale_fixed_part.read_bytes() == b"locked by previous run"


def test_app_update_temp_path_is_unique_and_not_legacy_fixed_part(tmp_path):
    dest = tmp_path / "EngAixt-Windows-Trial-1.1.0.zip"

    first = app_update._make_update_temp_path(dest)
    second = app_update._make_update_temp_path(dest)

    assert first != dest.with_suffix(dest.suffix + ".part")
    assert first != second
    assert first.name.startswith(dest.name + ".")
    assert first.name.endswith(".part")


def test_pending_update_blocks_old_packaged_startup(tmp_path, monkeypatch):
    monkeypatch.setattr(app_update, "_updates_dir", lambda: tmp_path)
    monkeypatch.setattr(app_update, "_is_packaged_app", lambda: True)
    monkeypatch.setattr(app_update, "_current_app_version", lambda: "1.0.13")

    marker = app_update._write_pending_update_marker(
        tmp_path / "EngAixt-Windows-Trial-1.0.14.zip",
        tmp_path / "EngAixt",
        "EngAixt.exe",
        {"version": "1.0.14"},
        tmp_path / "apply-engaixt-update.ps1",
        tmp_path / "launch-engaixt-update.cmd",
    )

    status = app_update.pending_update_status_for_startup()

    assert marker.exists()
    assert status is not None
    assert status["current_version"] == "1.0.13"
    assert status["target_version"] == "1.0.14"
    assert status["restart_after_update"] is False


def test_pending_update_marker_is_cleared_after_target_version_applied(tmp_path, monkeypatch):
    monkeypatch.setattr(app_update, "_updates_dir", lambda: tmp_path)
    monkeypatch.setattr(app_update, "_is_packaged_app", lambda: True)
    monkeypatch.setattr(app_update, "_current_app_version", lambda: "1.0.14")
    marker = tmp_path / "pending-update.json"
    marker.write_text(json.dumps({"target_version": "1.0.14", "created_at": 100.0}), encoding="utf-8")

    assert app_update.pending_update_status_for_startup(now=101.0) is None
    assert not marker.exists()


def test_stale_pending_update_marker_does_not_lock_user_out(tmp_path, monkeypatch):
    monkeypatch.setattr(app_update, "_updates_dir", lambda: tmp_path)
    monkeypatch.setattr(app_update, "_is_packaged_app", lambda: True)
    monkeypatch.setattr(app_update, "_current_app_version", lambda: "1.0.13")
    monkeypatch.setattr(app_update, "_append_update_launch_log", lambda _message: None)
    marker = tmp_path / "pending-update.json"
    marker.write_text(json.dumps({"target_version": "1.0.14", "created_at": 100.0}), encoding="utf-8")

    status = app_update.pending_update_status_for_startup(
        now=100.0 + app_update.PENDING_UPDATE_TTL_SECONDS + 1
    )

    assert status is None
    assert not marker.exists()


def test_exit_if_update_pending_shows_message_and_exits(tmp_path, monkeypatch):
    monkeypatch.setattr(app_update, "_updates_dir", lambda: tmp_path)
    monkeypatch.setattr(app_update, "_is_packaged_app", lambda: True)
    monkeypatch.setattr(app_update, "_current_app_version", lambda: "1.0.13")
    monkeypatch.setattr(app_update, "_append_update_launch_log", lambda _message: None)
    shown = []
    launched = []
    monkeypatch.setattr(app_update, "_launch_pending_update", lambda pending: launched.append(pending))
    monkeypatch.setattr(app_update, "_show_pending_update_message", lambda pending: shown.append(pending))
    (tmp_path / "pending-update.json").write_text(
        json.dumps({"target_version": "1.0.14", "created_at": app_update.time.time()}),
        encoding="utf-8",
    )

    assert app_update.exit_if_update_pending_at_startup() is True
    assert launched and launched[0]["target_version"] == "1.0.14"
    assert shown and shown[0]["target_version"] == "1.0.14"


def test_gui_exposes_software_update_ui():
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

    assert "软件更新" in html
    assert "nav-app-update" in html
    assert "nav-app-update-badge" in html
    assert "app-version-badge" in html
    assert "app-version-badge" in js
    assert "manual_update_required" in js
    assert "暂无可用更新包" in js
    assert "update-available" in js
    assert "updateApp()" in html
    assert "function updateApp()" in js
    assert "get_app_update_info" in js


def test_gui_update_info_checks_online_manifest(monkeypatch):
    expected = {
        "app": "EngAixt",
        "version": "1.0.0",
        "edition": "trial",
        "can_apply_update": True,
        "update_available": True,
        "latest_version": "1.0.1",
    }

    def fake_check_update(*, timeout=45):
        assert timeout == 12
        return expected

    monkeypatch.setattr(app_update, "check_update", fake_check_update)

    assert Api().get_app_update_info() == expected


def test_update_script_is_ascii_and_powershell_parseable(tmp_path, monkeypatch):
    monkeypatch.setattr(app_update, "_updates_dir", lambda: tmp_path)

    script = app_update._write_update_script(
        tmp_path / "EngAixt-Windows-Trial-1.0.4.zip",
        tmp_path / "EngAixt发布包" / "本机测试版" / "EngAixt-Trial",
        "EngAixt.exe",
        {"version": "1.0.4"},
    )
    text = script.read_text(encoding="utf-8")

    text.encode("ascii")
    assert "FromB64" in text
    assert "\\u" not in text
    assert "EngAixt update failed" in text
    assert "app_version.json" in text
    assert "versionMarkerJson" in text
    assert "pendingPath" in text
    assert "Clear-PendingUpdate" in text
    assert "removed pending update marker" in text
    assert "waitDeadlineSeconds" in text
    assert "Stop-InstallProcesses" in text
    assert "Copy-UpdateFiles" in text
    assert "Assert-UpdatedExe" in text
    assert "updated executable hash mismatch" in text
    assert "$restartAfterUpdate = $false" in text
    assert "manual restart requested" in text

    launcher = app_update._write_update_cmd_launcher(script, 1234)
    launcher_text = launcher.read_text(encoding="ascii")
    assert "-EncodedCommand" in launcher_text
    assert str(script) not in launcher_text
    encoded_command = launcher_text.split("-EncodedCommand ", 1)[1].splitlines()[0].strip()
    decoded_command = base64.b64decode(encoded_command).decode("utf-16le")
    assert "$pidToWait = 1234" in decoded_command
    assert app_update._ps_b64(str(script)) in decoded_command
    assert "cmd entered pid=$pidToWait" in decoded_command
    assert "powershell start returned pid" in decoded_command

    if sys.platform == "win32":
        command = (
            "$errors=$null; "
            f"$null=[System.Management.Automation.PSParser]::Tokenize((Get-Content -LiteralPath {str(script)!r} -Raw), [ref]$errors); "
            "if($errors){$errors | Format-List; exit 1}"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            check=True,
            capture_output=True,
            text=True,
        )


def test_update_script_can_still_opt_into_auto_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(app_update, "_updates_dir", lambda: tmp_path)

    script = app_update._write_update_script(
        tmp_path / "EngAixt-Windows-Trial-1.0.4.zip",
        tmp_path / "EngAixt",
        "EngAixt.exe",
        {"version": "1.0.4"},
        restart_after_update=True,
    )

    text = script.read_text(encoding="utf-8")
    assert "$restartAfterUpdate = $true" in text
    assert 'Start-Process -FilePath $exePath -WorkingDirectory $installDir' in text


