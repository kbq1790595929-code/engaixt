"""Launch the user's original MTool code with a dedicated test profile."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import socket
import subprocess
import time

import websocket

from .cdp import CDP, endpoint


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class MToolSession:
    def __init__(self, installation: Path, output: Path):
        self.installation = installation.resolve()
        self.output = output.resolve()
        self.process = None
        self.client = None
        self.port = None
        self.provenance = {}
        self.source_hashes = {}

    def __enter__(self):
        try:
            self._launch()
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def _launch(self):
        root = self.installation
        if not (root / "nw.exe").is_file():
            raise FileNotFoundError(root / "nw.exe")
        self.source_hashes = {
            str(path.relative_to(root)): digest(path)
            for path in [root / "package.json", root / "nw.exe", root / "www/loader.js",
                         *sorted((root / "www").glob("loader.bin.*"))]
            if path.is_file()
        }
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        command = [str(root / "nw.exe"), f"--remote-debugging-port={self.port}",
                   "--remote-debugging-address=127.0.0.1",
                   f"--user-data-dir={self.output / 'profile'}"]
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
        command += ["--enable-logging=stderr"]
        with (self.output / "nw-startup.log").open("wb") as log:
            self.process = subprocess.Popen(command, cwd=root, startupinfo=startup,
                                            stdout=log, stderr=log)
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"NW.js exited before export: {self.process.returncode}")
            try:
                pages = endpoint(self.port, "/json/list")
                page = next((p for p in pages if p.get("url", "").endswith("/www/index.html")), None)
                if page:
                    self.client = CDP(page["webSocketDebuggerUrl"])
                    if self.client.evaluate("typeof tEnv !== 'undefined' && typeof tEnv.RGSSWorker==='function' && Number.isInteger(tEnv.port)"):
                        break
                    self.client.close()
                    self.client = None
            except (OSError, ValueError, RuntimeError, websocket.WebSocketException):
                if self.client:
                    self.client.close()
                    self.client = None
            time.sleep(0.25)
        else:
            raise TimeoutError("MTool RGSSWorker did not initialize within 45 seconds")
        versions = self.client.evaluate("process.versions")
        loader = self.installation / "www" / f"loader.bin.{versions['nw']}"
        self.provenance = {"installation": str(self.installation), "versions": versions,
                           "loader": str(loader), "loader_sha256": digest(loader),
                           "nw_exe_sha256": digest(self.installation / "nw.exe"),
                           "transport": "loopback CDP; existing NW.js binary; separate profile",
                           "source_hashes_before": self.source_hashes}
        self.client.evaluate("require('nw.gui').Window.get().hide(); true")
        self.client.evaluate("(async()=>{globalThis.engaixtOracleWorker=new tEnv.RGSSWorker();"
                             "await engaixtOracleWorker.init();return true})()")

    def resources(self):
        output = {}
        for name in ("CLoader.rb", "rgssad.rb"):
            encoded = self.client.evaluate(
                "tEnv.readFileInBaseAsString(" + json.dumps("ruby\\" + name) + ")")
            values = json.loads(encoded)
            if not isinstance(values, list) or not all(isinstance(v, int) and 0 <= v <= 255 for v in values):
                raise ValueError(f"Unsupported MTool Ruby resource layout: {name}")
            raw = bytes(v ^ 123 for v in values)
            raw.decode("utf-8")
            (self.output / name).write_bytes(raw)
            output[name] = hashlib.sha256(raw).hexdigest()
        return output

    def script_strings(self, source: str):
        result = self.client.evaluate(
            "engaixtOracleWorker.getStringFromRubyScript(" + json.dumps(source) + ")")
        if not isinstance(result, list) or not all(isinstance(item, str) for item in result):
            raise ValueError("MTool RGSSWorker returned an unexpected result")
        return result

    def __exit__(self, *_):
        if self.client:
            self.client.close()
        if self.port:
            try:
                browser = CDP(endpoint(self.port, "/json/version")["webSocketDebuggerUrl"])
                try:
                    browser.call("Browser.close")
                finally:
                    browser.close()
            except Exception:
                pass
        if self.process:
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                # Only this disposable host process tree is owned by the comparator.
                subprocess.run(["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                               capture_output=True, timeout=15)
        if self.source_hashes:
            after = {name: digest(self.installation / name) for name in self.source_hashes}
            status = {"before": self.source_hashes, "after": after,
                      "unchanged": after == self.source_hashes}
            (self.output / "mtool-integrity.json").write_text(json.dumps(status, indent=2), encoding="utf-8")
            if after != self.source_hashes:
                raise RuntimeError("MTool executable/loader/package changed during comparison; see mtool-integrity.json")
