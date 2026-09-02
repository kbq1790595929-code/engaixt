from __future__ import annotations

import json
import platform
import re
import sys
import time
import traceback
from pathlib import Path
from typing import Any

from utils.logger import info, warning


_SENSITIVE_FIELD_TOKENS = ("api_key", "apikey", "secret", "token", "password")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b("
    r"[a-z0-9_-]*api[_-]?key|apikey|"
    r"[a-z0-9_-]*secret|"
    r"[a-z0-9_-]*token|"
    r"password"
    r")(\s*[:=]\s*)([^\s,;\"'<>)}\]]+)"
)
_BEARER_RE = re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9._~+/=-]{6,})")


class Diagnostics:
    """Per-run diagnostics written to workspace/diagnostics.json.

    This intentionally stays lightweight: it records facts and suggestions
    without coupling the pipeline to any specific engine implementation.
    """

    def __init__(self, workspace: Path):
        self.workspace = Path(workspace)
        self.path = self.workspace / "diagnostics.json"
        self.data: dict[str, Any] = {
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "steps": [],
            "warnings": [],
            "errors": [],
            "suggestions": [],
            "engine_candidates": [],
            "stage_metrics": [],
        }
        self._open_stage: dict[str, Any] | None = None

    def set(self, key: str, value: Any):
        self.data[key] = _jsonable(value, key=key)
        self.save()

    def step(self, name: str, status: str = "ok", **details: Any):
        self.data.setdefault("steps", []).append({
            "name": name,
            "status": status,
            "details": _jsonable(details, key="details"),
        })
        self.save()

    def warn(self, message: str, **details: Any):
        entry = {"message": message, "details": _jsonable(details, key="details")}
        self.data.setdefault("warnings", []).append(entry)
        warning(message)
        self.save()

    def error(self, message: str, **details: Any):
        entry = {"message": message, "details": _jsonable(details, key="details")}
        self.data.setdefault("errors", []).append(entry)
        self.save()

    def record_exception(self, message: str, exc: BaseException, **details: Any):
        """记录异常事实：类型、消息、完整调用栈（黑匣子核心字段之一）。"""
        try:
            stack = "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            )
        except Exception:
            stack = ""
        self.error(
            message,
            error=str(exc),
            type=type(exc).__name__,
            traceback=stack,
            **details,
        )

    def mark_stage(self, key: str, label: str = "", progress: float | None = None):
        """阶段切换打点：自动结算上一阶段耗时，累积到 stage_metrics。

        同一阶段重复打点视为仍在该阶段，不产生新记录。
        """
        if self._open_stage is not None:
            if self._open_stage["key"] == key:
                return
            self._close_open_stage()
        self._open_stage = {
            "key": key,
            "label": label,
            "progress": progress,
            "started_monotonic": time.monotonic(),
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    def stage_io(self, **counts: Any):
        """给当前阶段附加输入/输出数量或跳过原因等事实。"""
        if self._open_stage is None:
            return
        self._open_stage.setdefault("io", {}).update(_jsonable(counts))

    def _close_open_stage(self, status: str = "ok"):
        stage = self._open_stage
        self._open_stage = None
        if stage is None:
            return
        record = {
            "key": stage["key"],
            "label": stage["label"],
            "started_at": stage["started_at"],
            "duration_ms": int((time.monotonic() - stage["started_monotonic"]) * 1000),
            "status": status,
        }
        if stage.get("io"):
            record["io"] = stage["io"]
        self.data.setdefault("stage_metrics", []).append(record)
        self.save()

    def suggest(self, message: str):
        suggestions = self.data.setdefault("suggestions", [])
        if message not in suggestions:
            suggestions.append(message)
        self.save()

    def save(self):
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def finish(self, success: bool):
        self._close_open_stage(status="ok" if success else "aborted")
        self.data["success"] = bool(success)
        self.data["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self.save()
        try:
            from core.usage_statistics import finish_usage_run

            finish_usage_run(self.data.get("usage_run_id", ""), self.data)
        except Exception as exc:
            warning(f"使用统计收尾失败，已保留诊断报告: {exc}")
        latest = self.workspace.parent / "latest_diagnostics.json"
        latest.write_text(
            json.dumps(self.data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        info(f"诊断报告已保存: {self.path}")
        info(f"最新诊断报告副本: {latest}")


def _jsonable(value: Any, key: str = "") -> Any:
    """递归转换为可 JSON 序列化的值，并按 key 名强制脱敏。

    脱敏是黑匣子的硬边界，不依赖调用方自觉：任何嵌套层级里 key 名命中
    敏感词根（api_key/secret/token/password）的值都会被掩码，哪怕是异常
    details 里误塞进来的 secret。

    普通字符串只替换带标签的 secret 片段（如 api_key=...、Bearer ...），
    不按裸值启发式匹配，避免误伤 executable_sha256、git head、gameId。
    """
    # 敏感 key 名直接替换值（任意嵌套层级生效）
    if key and _is_sensitive_field(key):
        return _mask_secret(value)

    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        return _redact_secret_fragments(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v, key=key) for v in value]
    return _redact_secret_fragments(str(value))


def _is_sensitive_field(name: str) -> bool:
    lowered = str(name or "").lower()
    if lowered.endswith("_tokens") and any(
        metric in lowered
        for metric in ("input", "output", "prompt", "completion", "translation")
    ):
        return False
    if lowered in {"total_tokens", "max_tokens"}:
        return False
    return any(token in lowered for token in _SENSITIVE_FIELD_TOKENS)


def _mask_secret(value: Any) -> str:
    text = str(value or "")
    if len(text) > 6:
        return f"{text[:2]}...{text[-2:]}"
    return "***REDACTED***"


def _redact_secret_fragments(text: str) -> str:
    def assignment_repl(match: re.Match[str]) -> str:
        return f"{match.group(1)}{match.group(2)}{_mask_secret(match.group(3))}"

    redacted = _SECRET_ASSIGNMENT_RE.sub(assignment_repl, text)
    return _BEARER_RE.sub(lambda m: f"{m.group(1)}{_mask_secret(m.group(2))}", redacted)
