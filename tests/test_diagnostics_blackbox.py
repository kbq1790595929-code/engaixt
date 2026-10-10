"""diagnostics 黑匣子测试：阶段耗时、异常栈、游戏 exe 快照、secret 禁入。"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.diagnostics import Diagnostics
from core.pipeline import Pipeline
from core.pipeline_context import _game_exe_snapshot
from core.pipeline_stages import STAGES


def test_stage_metrics_record_duration_per_stage(tmp_path: Path):
    diag = Diagnostics(tmp_path)
    diag.mark_stage("detect", "检测引擎", 5)
    time.sleep(0.01)
    diag.mark_stage("extract", "提取文本", 20)
    diag.finish(True)

    metrics = diag.data["stage_metrics"]
    assert [m["key"] for m in metrics] == ["detect", "extract"]
    assert all(m["duration_ms"] >= 0 for m in metrics)
    assert all(m["started_at"] for m in metrics)
    assert metrics[0]["status"] == "ok"


def test_stage_metrics_same_stage_repeated_mark_is_one_record(tmp_path: Path):
    diag = Diagnostics(tmp_path)
    diag.mark_stage("detect", "检测引擎", 5)
    diag.mark_stage("detect", "检测引擎", 5)
    diag.finish(True)
    assert [m["key"] for m in diag.data["stage_metrics"]] == ["detect"]


def test_stage_metrics_failed_run_marks_last_stage_aborted(tmp_path: Path):
    diag = Diagnostics(tmp_path)
    diag.mark_stage("translate", "翻译", 40)
    diag.finish(False)
    assert diag.data["stage_metrics"][-1]["status"] == "aborted"


def test_stage_io_attaches_counts_to_current_stage(tmp_path: Path):
    diag = Diagnostics(tmp_path)
    diag.mark_stage("extract", "提取文本", 20)
    diag.stage_io(items_out=123, skipped=4, skip_reason="binary")
    diag.finish(True)
    io = diag.data["stage_metrics"][0]["io"]
    assert io == {"items_out": 123, "skipped": 4, "skip_reason": "binary"}


def test_record_exception_captures_type_and_traceback(tmp_path: Path):
    diag = Diagnostics(tmp_path)
    try:
        raise ValueError("boom for diagnostics")
    except ValueError as exc:
        diag.record_exception("流水线异常", exc)

    entry = diag.data["errors"][-1]
    assert entry["message"] == "流水线异常"
    assert entry["details"]["type"] == "ValueError"
    assert "boom for diagnostics" in entry["details"]["error"]
    assert "ValueError" in entry["details"]["traceback"]
    assert "test_diagnostics_blackbox" in entry["details"]["traceback"]


def test_pipeline_progress_feeds_stage_metrics(tmp_path: Path):
    pipeline = Pipeline()
    pipeline.diagnostics = Diagnostics(tmp_path)
    first, second = STAGES[0].key, STAGES[1].key
    pipeline._update_progress(first)
    pipeline._update_progress(second)
    pipeline.diagnostics.finish(True)

    keys = [m["key"] for m in pipeline.diagnostics.data["stage_metrics"]]
    assert keys == [first, second]


def test_game_exe_snapshot_records_path_mtime_size(tmp_path: Path):
    game = tmp_path / "game"
    game.mkdir()
    exe = game / "game.exe"
    exe.write_bytes(b"MZ" + b"\0" * 128)

    snapshot = _game_exe_snapshot(game)
    assert snapshot["path"].endswith("game.exe")
    assert snapshot["size"] == 130
    assert snapshot["mtime"] > 0


def test_game_exe_snapshot_empty_dir_is_empty_fact(tmp_path: Path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert _game_exe_snapshot(empty) == {}


def test_diagnostics_data_never_contains_secret_keys(tmp_path: Path):
    """无论谁往 diagnostics 塞了什么，键名不得出现 secret 词根。"""
    diag = Diagnostics(tmp_path)
    diag.mark_stage("detect", "检测", 5)
    diag.finish(True)

    banned = ("api_key", "apikey", "secret", "token", "password")

    def _walk(value):
        if isinstance(value, dict):
            for key, val in value.items():
                lowered = str(key).lower()
                assert not any(tok in lowered for tok in banned), key
                _walk(val)
        elif isinstance(value, list):
            for val in value:
                _walk(val)

    _walk(diag.data)


def test_diagnostics_redacts_secret_values_even_when_inserted_by_other_caller(tmp_path: Path):
    """黑匣子强制脱敏：任何调用方误塞的 secret 值都会被掩码。

    这是"用户说流程成功但游戏不对"场景里的最后一道防线——如果异常
    details 或中间数据不小心带了 key/token，落盘时必须脱敏。
    """
    diag = Diagnostics(tmp_path)

    # set() 直接塞
    diag.set("deepseek_api_key", "sk-test-1234567890abcdef")
    diag.set("auth_token", "eyJhbGciOiJIUzI1NiJ9.example")

    # error() 塞
    diag.error("流水线异常", api_key="sk-leaked", token="abc123xyz")

    # 嵌套 dict
    diag.set("nested", {"password": "hunter2", "normal_key": "safe_value", "sub": {"secret": "top_secret"}})

    # 异常栈里塞
    try:
        raise RuntimeError("boom", {"detail": "deepseek_api_key=sk-leaked"})
    except RuntimeError as e:
        diag.record_exception("异常捕获", e, extra="deepseek_api_key=sk-leaked")

    # 验证：所有敏感 key 的值都被掩码
    banned = ("api_key", "apikey", "secret", "token", "password")

    def _walk(value, path="root"):
        if isinstance(value, dict):
            for key, val in value.items():
                lowered = str(key).lower()
                if any(tok in lowered for tok in banned):
                    assert isinstance(val, str), f"{path}.{key} 应为字符串（掩码），实际: {val!r}"
                    assert len(val) < 15, f"{path}.{key} 不应暴露完整值: {val!r}"
                    assert "***" in val or val[:2] != val, f"{path}.{key} 掩码不完整: {val!r}"
                else:
                    _walk(val, f"{path}.{key}")
        elif isinstance(value, list):
            for i, val in enumerate(value):
                _walk(val, f"{path}[{i}]")

    _walk(diag.data)
    serialized = diag.path.read_text(encoding="utf-8")
    assert "sk-test-1234567890abcdef" not in serialized
    assert "eyJhbGciOiJIUzI1NiJ9.example" not in serialized
    assert "sk-leaked" not in serialized
    assert "hunter2" not in serialized
    assert "top_secret" not in serialized
    assert "deepseek_api_key=sk...ed" in serialized


def test_diagnostics_redacts_labeled_secrets_inside_plain_strings(tmp_path: Path):
    """异常 message/traceback 这类普通 key 下也可能夹带 secret=...。"""
    diag = Diagnostics(tmp_path)
    try:
        raise RuntimeError("boom deepseek_api_key=sk-real-secret auth_token=abc123 password=hunter2")
    except RuntimeError as exc:
        diag.record_exception(
            "异常捕获",
            exc,
            extra="deepseek_api_key=sk-extra-secret",
            header="Authorization: Bearer sk-bearer-secret",
        )

    serialized = diag.path.read_text(encoding="utf-8")
    assert "sk-real-secret" not in serialized
    assert "abc123" not in serialized
    assert "hunter2" not in serialized
    assert "sk-extra-secret" not in serialized
    assert "sk-bearer-secret" not in serialized
    assert "deepseek_api_key=sk...et" in serialized
    assert "auth_token=***REDACTED***" in serialized
    assert "password=hu...r2" in serialized
    assert "Bearer sk...et" in serialized


def test_diagnostics_preserves_non_sensitive_long_strings(tmp_path: Path):
    """脱敏不碰非敏感字段：sha256/git head/gameId 等排查关键字段完整保留。"""
    diag = Diagnostics(tmp_path)
    diag.set("executable_sha256", "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789")
    diag.set("git_head", "a2b5490e6479")
    diag.set("gameId", "80556134")
    diag.set("long_text", "这是一个很长的文本，不会被误认为是密钥因为它不包含敏感词根")
    diag.finish(True)

    assert diag.data["executable_sha256"] == "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789"
    assert diag.data["git_head"] == "a2b5490e6479"
    assert diag.data["gameId"] == "80556134"
    assert diag.data["long_text"] == "这是一个很长的文本，不会被误认为是密钥因为它不包含敏感词根"


def test_diagnostics_preserves_token_usage_metrics_but_redacts_credentials(tmp_path: Path):
    diag = Diagnostics(tmp_path)
    diag.set("api_cache_stats", {
        "actual_input_tokens": 888541,
        "actual_output_tokens": 585230,
        "auth_token": "secret-auth-token",
    })

    stats = diag.data["api_cache_stats"]
    assert stats["actual_input_tokens"] == 888541
    assert stats["actual_output_tokens"] == 585230
    assert stats["auth_token"] != "secret-auth-token"
