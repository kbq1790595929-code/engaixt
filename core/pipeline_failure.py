"""Structured pipeline failures shared by the backend and the GUI."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


_MESSAGES = {
    "archive_extract_failed": "压缩包无法解压，翻译尚未开始。请确认压缩包完整，或先解压后选择游戏目录。",
    "engine_not_found": "没有识别出可用的游戏引擎，因此没有开始翻译。",
    "krkr_no_readable_script": "KRKR 的 XP3 封包可以读取，但没有提取出可解析的脚本，因此没有开始翻译。",
    "krkr_static_tools_missing": "KRKR 静态组件不完整，无法继续解包或回填。可以改用实时翻译，或重新安装完整版本。",
    "krkr_static_extract_failed": "KRKR 脚本提取失败，已尝试内置解析器和外部解包工具。可以改用实时翻译。",
    "krkr_static_repack_failed": "KRKR 译文回填验证失败，原始游戏文件没有被修改。可以改用实时翻译。",
    "no_readable_text": "没有发现可翻译的有效文本，因此没有继续翻译或修改游戏。",
    "extract_failed": "文本提取阶段失败，因此没有开始翻译。",
    "repack_failed": "回填阶段失败，原始游戏文件没有被修改。",
    "translation_key_missing": "没有填写当前翻译器所需的 API Key，文本提取已完成，但尚未开始 AI 翻译。",
    "translation_no_result": "翻译器没有返回有效译文，回填没有执行。请检查模型、API Key 和网络连接。",
    "translation_coverage_low": "有效译文数量低于安全覆盖率要求，回填没有执行。原始游戏文件保持不变。",
    "runtime_prepare_failed": "静态方案失败，实时翻译组件准备失败，暂时无法生成实时启动器。",
    "runtime_launch_failed": "实时翻译启动失败，游戏原文件没有被修改。",
}


_MESSAGES.update({
    "checkpoint_missing": "翻译检查点不存在，无法执行回填，请重新完成提取或选择有效检查点。",
    "patch_no_translation": "检查点中没有可用译文，回填没有执行。",
})


@dataclass(frozen=True)
class StageFailure:
    """A user-readable failure with machine-readable recovery data."""

    stage: str
    code: str
    user_message: str
    technical_detail: str = ""
    fallback: str = ""
    actions: tuple[str, ...] = ()
    tool_attempts: tuple[dict[str, Any], ...] = ()
    rollback: bool = False
    next_actions: tuple[str, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "stage": self.stage,
            "code": self.code,
            "user_message": self.user_message,
            "technical_detail": self.technical_detail,
            "fallback": self.fallback,
            "fallback_available": bool(self.fallback),
            "actions": list(self.actions),
            "tool_attempts": [dict(item) for item in self.tool_attempts],
            "rollback": bool(self.rollback),
            "next_actions": list(self.next_actions),
        }
        result.update(self.extra)
        return result


def make_stage_failure(
    stage: str,
    code: str,
    *,
    detail: str = "",
    user_message: str = "",
    fallback: str = "",
    actions: tuple[str, ...] = (),
    tool_attempts: tuple[dict[str, Any], ...] = (),
    rollback: bool = False,
    next_actions: tuple[str, ...] = (),
    **extra: Any,
) -> StageFailure:
    return StageFailure(
        stage=stage,
        code=code,
        user_message=user_message or _MESSAGES.get(
            code,
            "当前阶段执行失败，原始游戏文件没有被修改。",
        ),
        technical_detail=detail,
        fallback=fallback,
        actions=actions,
        tool_attempts=tool_attempts,
        rollback=rollback,
        next_actions=next_actions,
        extra=extra,
    )


def publish_stage_failure(
    diagnostics: Any,
    meta_callback: Callable[[str, object], None] | None,
    failure: StageFailure,
) -> dict[str, Any]:
    """Persist and forward one failure without exposing secrets."""
    payload = failure.to_dict()
    if diagnostics is not None:
        recorder = getattr(diagnostics, "record_stage_failure", None)
        if callable(recorder):
            recorder(payload)
        else:
            diagnostics.set("failure", payload)
            diagnostics.error(
                failure.user_message,
                stage=failure.stage,
                code=failure.code,
                technical_detail=failure.technical_detail,
                fallback=failure.fallback,
                actions=list(failure.actions),
                rollback=failure.rollback,
            )
    if meta_callback:
        try:
            meta_callback("stage_failure", payload)
        except Exception:
            pass
    return payload
