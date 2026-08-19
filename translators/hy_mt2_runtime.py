from __future__ import annotations

import atexit
import json
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from config import get_config
from translators.hy_mt2_component import (
    component_status,
    model_path,
    runtime_executable,
    selected_model_name,
)
from utils.logger import info, warning


class HyMt2ComponentMissingError(RuntimeError):
    pass


@dataclass(frozen=True)
class LocalCompletion:
    content: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    elapsed_seconds: float = 0.0


class HyMt2Runtime:
    def __init__(self):
        self._lock = threading.RLock()
        self._request_lock = threading.Lock()
        self._process: subprocess.Popen | None = None
        self._log_handle = None
        self._port = 0
        self._active_backend = ""
        self._active_model = ""
        self._idle_timer: threading.Timer | None = None
        self._inflight = 0

    @property
    def active_backend(self) -> str:
        with self._lock:
            return self._active_backend

    @property
    def active_model(self) -> str:
        with self._lock:
            return self._active_model

    def complete(self, prompt: str, max_tokens: int) -> LocalCompletion:
        return self.complete_messages(
            [{"role": "user", "content": str(prompt or "")}],
            max_tokens,
        )

    def complete_messages(
        self,
        messages: list[dict[str, str]],
        max_tokens: int,
    ) -> LocalCompletion:
        normalized = [
            {
                "role": str(message.get("role") or "user"),
                "content": str(message.get("content") or ""),
            }
            for message in messages
            if str(message.get("content") or "").strip()
        ]
        if not normalized:
            raise ValueError("Hy-MT2 messages cannot be empty")
        with self._request_lock:
            self._cancel_idle_timer()
            with self._lock:
                self._inflight += 1
            try:
                self.ensure_started()
                try:
                    return self._request(normalized, max_tokens)
                except Exception as exc:
                    if self.active_backend == "cpu":
                        raise
                    warning(f"Hy-MT2 {self.active_backend} 推理失败，回退 CPU: {exc}")
                    self.stop()
                    self._start_backend("cpu", selected_model_name())
                    return self._request(normalized, max_tokens)
            finally:
                with self._lock:
                    self._inflight = max(0, self._inflight - 1)
                self._schedule_idle_stop()

    def ensure_started(self) -> None:
        with self._lock:
            selected_model = selected_model_name()
            if (
                self._process
                and self._process.poll() is None
                and self._active_model == selected_model
                and self._health_ready()
            ):
                return
            self.stop()
            status = component_status(selected_model)
            if not status.get("ready"):
                raise HyMt2ComponentMissingError(
                    "Hy-MT2 离线组件未就绪，请在设置中点击“下载/修复离线组件”"
                )
            preferred = "cuda" if str(status.get("runner", "")).startswith("cuda") else "vulkan"
            try:
                self._start_backend(preferred, selected_model)
            except Exception as exc:
                warning(f"Hy-MT2 {preferred} 后端启动失败，回退 CPU: {exc}")
                self.stop()
                self._start_backend("cpu", selected_model)

    def verify_selected_model(self) -> dict[str, str | int | float]:
        """Boot the chosen backend and prove it can generate before install succeeds."""
        selected_model = selected_model_name()
        info(f"[Hy-MT2 部署] 自动启动后端验证: model={selected_model}")
        with self._request_lock:
            self._cancel_idle_timer()
            try:
                self.ensure_started()
                completion = self._request(
                    [{"role": "user", "content": "Reply with READY only."}],
                    16,
                )
                if not completion.content:
                    raise RuntimeError("本地模型没有返回验证文本")
                result = {
                    "model": self.active_model,
                    "backend": self.active_backend,
                    "port": self._port,
                    "prompt_tokens": completion.prompt_tokens,
                    "completion_tokens": completion.completion_tokens,
                    "elapsed_seconds": round(completion.elapsed_seconds, 3),
                }
                info(
                    "[Hy-MT2 部署] 后端验证通过: "
                    f"model={result['model']}, backend={result['backend']}, "
                    f"生成={result['completion_tokens']} tokens, "
                    f"耗时={result['elapsed_seconds']}s"
                )
                return result
            finally:
                # Installation verifies the full path once, then releases memory.
                # Translation later starts the chosen backend automatically.
                self.stop()
                info("[Hy-MT2 部署] 验证完成，已释放本地模型内存")

    def stop(self) -> None:
        with self._lock:
            self._cancel_idle_timer()
            process = self._process
            self._process = None
            self._port = 0
            self._active_backend = ""
            self._active_model = ""
            if process and process.poll() is None:
                try:
                    process.terminate()
                    process.wait(timeout=5)
                except Exception:
                    try:
                        process.kill()
                    except Exception:
                        pass
            if self._log_handle:
                try:
                    self._log_handle.close()
                except Exception:
                    pass
                self._log_handle = None

    def _start_backend(self, backend: str, selected_model: str) -> None:
        executable = runtime_executable()
        selected_path = model_path(selected_model)
        if not executable.is_file() or not selected_path.is_file():
            raise HyMt2ComponentMissingError("Hy-MT2 模型或运行器缺失")
        config = get_config()
        context_size = max(1024, min(16384, int(getattr(config, "hy_mt2_context_size", 4096) or 4096)))
        port = _free_port()
        args = [
            str(executable),
            "-m", str(selected_path),
            "-c", str(context_size),
            "--host", "127.0.0.1",
            "--port", str(port),
            "--parallel", "1",
            "--alias", selected_model.lower(),
            "--no-webui",
            "--jinja",
        ]
        if backend == "cpu":
            args.extend(["-ngl", "0"])
        elif backend == "vulkan":
            args.extend(["-ngl", "999", "-dev", "Vulkan0"])
        else:
            args.extend(["-ngl", "999"])

        log_path = selected_path.parent.parent / "runtime.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_handle = open(log_path, "w", encoding="utf-8", errors="replace")
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._process = subprocess.Popen(
            args,
            cwd=str(executable.parent),
            stdin=subprocess.DEVNULL,
            stdout=self._log_handle,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
        self._port = port
        self._active_backend = backend
        self._active_model = selected_model
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                tail = _read_log_tail(log_path)
                raise RuntimeError(f"llama-server 提前退出: {tail}")
            if self._health_ready():
                info(
                    f"Hy-MT2 本地运行时已启动: model={selected_model}, "
                    f"backend={backend}, context={context_size}"
                )
                return
            time.sleep(0.25)
        raise TimeoutError(f"Hy-MT2 {backend} 后端启动超时")

    def _health_ready(self) -> bool:
        if not self._port:
            return False
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self._port}/health", timeout=1) as response:
                return response.status == 200
        except Exception:
            return False

    def _request(self, messages: list[dict[str, str]], max_tokens: int) -> LocalCompletion:
        payload = json.dumps({
            "model": self.active_model or selected_model_name(),
            "messages": messages,
            "temperature": 0.2,
            "top_p": 0.6,
            "top_k": 20,
            "repeat_penalty": 1.05,
            "max_tokens": max(64, min(4096, int(max_tokens))),
            "stream": False,
        }, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{self._port}/v1/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=1200) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:1000]
            raise RuntimeError(f"本地推理 HTTP {exc.code}: {body}") from exc
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError("Hy-MT2 没有返回译文")
        content = str((choices[0].get("message") or {}).get("content") or "").strip()
        usage = data.get("usage") or {}
        return LocalCompletion(
            content=content,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            elapsed_seconds=time.perf_counter() - started,
        )

    def _schedule_idle_stop(self) -> None:
        config = get_config()
        seconds = max(30, int(getattr(config, "hy_mt2_idle_timeout_seconds", 180) or 180))

        def stop_if_idle():
            with self._lock:
                if self._inflight == 0:
                    info("Hy-MT2 空闲超时，释放本地模型内存")
                    self.stop()

        with self._lock:
            self._cancel_idle_timer()
            self._idle_timer = threading.Timer(seconds, stop_if_idle)
            self._idle_timer.daemon = True
            self._idle_timer.start()

    def _cancel_idle_timer(self) -> None:
        if self._idle_timer:
            self._idle_timer.cancel()
            self._idle_timer = None


_RUNTIME = HyMt2Runtime()
atexit.register(_RUNTIME.stop)


def get_runtime() -> HyMt2Runtime:
    return _RUNTIME


def shutdown_runtime() -> None:
    _RUNTIME.stop()


def verify_selected_model() -> dict[str, str | int | float]:
    return _RUNTIME.verify_selected_model()


def runtime_status() -> dict[str, str | bool]:
    return {
        "running": bool(_RUNTIME._process and _RUNTIME._process.poll() is None),
        "active_backend": _RUNTIME.active_backend,
        "active_model": _RUNTIME.active_model,
    }


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _read_log_tail(path: Path, limit: int = 1500) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        return text[-limit:].strip()
    except Exception:
        return "无法读取运行日志"
