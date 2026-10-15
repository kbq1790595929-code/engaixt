"""Local Chrome DevTools protocol transport for the optional MTool oracle."""
from __future__ import annotations

import json
import urllib.request

import websocket


def endpoint(port: int, path: str):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3) as response:
        return json.load(response)


class CDP:
    def __init__(self, url: str):
        self.socket = websocket.create_connection(url, timeout=45, suppress_origin=True)
        self.sequence = 0

    def call(self, method: str, **params):
        self.sequence += 1
        self.socket.send(json.dumps({"id": self.sequence, "method": method, "params": params}))
        while True:
            reply = json.loads(self.socket.recv())
            if reply.get("id") != self.sequence:
                continue
            if "error" in reply:
                raise RuntimeError(f"{method}: {reply['error']}")
            return reply.get("result", {})

    def evaluate(self, expression: str):
        result = self.call("Runtime.evaluate", expression=expression,
                           awaitPromise=True, returnByValue=True)
        if "exceptionDetails" in result:
            detail = result["exceptionDetails"]
            raise RuntimeError(detail.get("exception", {}).get("description") or detail["text"])
        return result.get("result", {}).get("value")

    def close(self):
        self.socket.close()
