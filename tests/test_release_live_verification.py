from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("scenario", ["redirect", "empty_manifest", "bad_hash", "wrong_manifest", "local_mismatch"])
def test_live_verifier_follows_redirects_and_rejects_invalid_assets(tmp_path, scenario):
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if not shell or os.name != "nt":
        pytest.skip("The release verifier uses Windows curl and PowerShell")
    payload = b"release package test payload"
    digest = hashlib.sha256(payload).hexdigest()
    hits = []
    feed = {"version": "1.1.18", "size": len(payload), "sha256": digest, "package_name": "package.zip"}
    manifest = {**feed, "parts": [{"path": "package.zip", "size": len(payload), "sha256": digest}]}
    if scenario == "wrong_manifest":
        manifest["sha256"] = "f" * 64

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            if self.path == "/manifest-redirect":
                self.send_response(302)
                self.send_header("Location", "/manifest.json")
                self.end_headers()
                return
            if self.path == "/updates/latest.json":
                body = json.dumps(feed).encode()
            elif self.path == "/manifest.json":
                body = b"" if scenario == "empty_manifest" else json.dumps(manifest).encode()
            elif self.path == "/package.zip":
                body = b"broken" if scenario == "bad_hash" else payload
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    base = f"http://127.0.0.1:{server.server_port}"
    feed["package_manifest_url"] = base + "/manifest-redirect"
    deploy = tmp_path / "site"
    (deploy / "updates").mkdir(parents=True)
    local_feed = {**feed, "version": "old"} if scenario == "local_mismatch" else feed
    (deploy / "updates/latest.json").write_text(json.dumps(local_feed), encoding="utf-8")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = subprocess.run(
            [shell, "-NoProfile", "-NonInteractive", "-File", str(ROOT / "tools/release/verify-live.ps1"),
             "-Site", base, "-DeployDir", str(deploy), "-Local"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert "/manifest.json" in hits
    if scenario == "redirect":
        assert result.returncode == 0, result.stdout + result.stderr
        assert hits.count("/package.zip") == 1
        assert "LIVE OK" in result.stdout
    else:
        assert result.returncode != 0, result.stdout + result.stderr
        assert "LIVE OK" not in result.stdout
