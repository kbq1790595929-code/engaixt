"""游戏目录可写性合同：只读介质/无权限必须在动手之前被发现。

真实问题（2026-10-02）：用户直接选中虚拟光驱里的游戏（CDFS 只读卷），
检查点镜像写入游戏目录 `_translation_meta` 时抛 PermissionError，整条流水线在
“导出检查点 37%”崩掉，而运行日志只显示“错误 0%”，没有任何原因。
"""
from __future__ import annotations

import contextlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import core.game_dir_access as game_dir_access
import core.pipeline as pipeline_mod
import core.preflight as preflight_mod
from core.pipeline import Pipeline
from core.pipeline_checkpoint_stage import save_checkpoint_json
from engines.base import EngineBase, EngineCapabilities, TextItem


class _StubEngine(EngineBase):
    name = "stub"
    label = "Stub Engine"
    capabilities = EngineCapabilities(
        extract=True,
        repack=True,
        static_patch=True,
        creates_launcher=True,
    )

    def detect(self, path: Path) -> bool:
        return True

    def unpack(self, path: Path, workspace: Path):
        return []

    def repack(self, items, workspace: Path) -> None:
        return None


class _StubDiagnostics:
    def __init__(self):
        self.facts: dict = {}

    def set(self, key, value):
        self.facts[key] = value


def _read_only_facts(game_dir: Path) -> dict:
    return {
        "dir": str(game_dir),
        "exists": True,
        "writable": False,
        "reason": "read_only_media",
        "detail": f"游戏目录位于只读介质（光盘或挂载镜像），无法写入译本和检查点: {game_dir}",
        "action": "请先把游戏安装或复制到本机硬盘的可写位置，再选择该目录重新翻译。",
        "drive_type": "cdrom",
        "volume_root": "F:\\",
        "read_only_volume": True,
        "free_bytes": 0,
        "probe_error": "PermissionError: [WinError 5] 拒绝访问。",
    }


def _make_pipeline(events: list) -> Pipeline:
    return Pipeline(meta_callback=lambda key, value: events.append((key, value)))


def _failures(events: list) -> list[dict]:
    return [value for key, value in events if key == "stage_failure"]


# --------------------------------------------------------------------------
# 探测本身
# --------------------------------------------------------------------------

def test_writable_directory_is_reported_writable_without_residue(tmp_path: Path):
    game = tmp_path / "game"
    game.mkdir()

    facts = game_dir_access.describe_game_dir_access(game)

    assert facts["writable"] is True
    assert facts["reason"] == ""
    assert facts["detail"] == ""
    assert list(game.iterdir()) == [], "写探针不能残留在游戏目录里"


def test_missing_directory_is_reported_before_engine_work(tmp_path: Path):
    facts = game_dir_access.describe_game_dir_access(tmp_path / "missing")

    assert facts["writable"] is False
    assert facts["reason"] == "missing_dir"


def test_read_only_media_is_detected_from_probe_and_volume(tmp_path: Path, monkeypatch):
    game = tmp_path / "game"
    game.mkdir()
    monkeypatch.setattr(game_dir_access, "_volume_root", lambda directory: "F:\\")
    monkeypatch.setattr(game_dir_access, "_drive_type", lambda root: game_dir_access.DRIVE_CDROM)
    monkeypatch.setattr(game_dir_access, "_volume_read_only", lambda root: True)
    monkeypatch.setattr(game_dir_access, "_free_bytes", lambda root: 0)
    monkeypatch.setattr(
        game_dir_access,
        "_write_probe_error",
        lambda directory: PermissionError(13, "拒绝访问。"),
    )

    facts = game_dir_access.describe_game_dir_access(game)

    assert facts["writable"] is False
    assert facts["reason"] == "read_only_media"
    assert facts["drive_type"] == "cdrom"
    assert "只读介质" in facts["detail"]
    assert "复制到本机硬盘" in facts["action"]


def test_permission_denied_on_writable_volume_gives_permission_advice(tmp_path: Path, monkeypatch):
    game = tmp_path / "game"
    game.mkdir()
    monkeypatch.setattr(game_dir_access, "_volume_root", lambda directory: "C:\\")
    monkeypatch.setattr(game_dir_access, "_drive_type", lambda root: game_dir_access.DRIVE_FIXED)
    monkeypatch.setattr(game_dir_access, "_volume_read_only", lambda root: False)
    monkeypatch.setattr(game_dir_access, "_free_bytes", lambda root: 10 * 1024 ** 3)
    monkeypatch.setattr(
        game_dir_access,
        "_write_probe_error",
        lambda directory: PermissionError(13, "拒绝访问。"),
    )

    facts = game_dir_access.describe_game_dir_access(game)

    assert facts["reason"] == "write_denied"
    assert "权限" in facts["action"]


# --------------------------------------------------------------------------
# 预检
# --------------------------------------------------------------------------

def _check(result: dict, name: str) -> dict:
    for check in result["checks"]:
        if check["name"] == name:
            return check
    raise AssertionError(f"preflight 缺少检查项: {name}")


def test_preflight_reports_writable_game_dir(tmp_path: Path):
    game = tmp_path / "game"
    game.mkdir()

    result = preflight_mod.run_preflight(game)

    check = _check(result, "game_dir_writable")
    assert check["status"] == "ok"
    assert check["required"] is True


def test_preflight_blocks_read_only_game_dir_with_action(tmp_path: Path, monkeypatch):
    game = tmp_path / "game"
    game.mkdir()
    monkeypatch.setattr(
        preflight_mod, "describe_game_dir_access", lambda path: _read_only_facts(game)
    )

    result = preflight_mod.run_preflight(game)

    check = _check(result, "game_dir_writable")
    assert check["status"] == "missing"
    assert "只读介质" in check["detail"]
    assert result["ok"] is False
    assert any("复制到本机硬盘" in item for item in result["suggestions"])


# --------------------------------------------------------------------------
# 检查点镜像必须是非致命写入
# --------------------------------------------------------------------------

def test_save_checkpoint_json_survives_unwritable_game_meta(tmp_path: Path, monkeypatch):
    game = tmp_path / "game"
    game.mkdir()
    blocker = game / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(
        pipeline_mod, "_game_meta_path", lambda game_path, filename: blocker / filename
    )

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    checkpoint = workspace_root / "translation_checkpoint.json"

    class _Workspace:
        root = workspace_root

    pipe = type("_Pipe", (), {})()
    pipe.workspace = _Workspace()
    pipe.diagnostics = _StubDiagnostics()

    items = [TextItem(file="scenario/a.ks", key="line_1", original="こんにちは", translated="你好")]

    save_checkpoint_json(pipe, items, game, "ja", "zh-CN", checkpoint)

    assert checkpoint.exists(), "游戏目录写不了时，工作区检查点仍必须写出"
    assert pipe.diagnostics.facts["checkpoint_game_mirror"]["ok"] is False


def test_save_checkpoint_json_falls_back_from_legacy_game_path(tmp_path: Path, monkeypatch):
    game = tmp_path / "game"
    game.mkdir()
    blocker = game / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(
        pipeline_mod, "_game_meta_path", lambda game_path, filename: blocker / filename
    )

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()

    class _Workspace:
        root = workspace_root

    pipe = type("_Pipe", (), {})()
    pipe.workspace = _Workspace()
    pipe.diagnostics = _StubDiagnostics()
    legacy_path = blocker / "translation_checkpoint.json"
    items = [TextItem(file="scenario/a.ks", key="line_1", original="こんにちは", translated="你好")]

    saved = save_checkpoint_json(pipe, items, game, "ja", "zh-CN", legacy_path)

    assert saved == workspace_root / "translation_checkpoint.json"
    assert saved.exists()
    assert not legacy_path.exists()


def test_sync_checkpoint_json_survives_unwritable_game_meta(tmp_path: Path, monkeypatch):
    game = tmp_path / "game"
    game.mkdir()
    blocker = game / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(
        pipeline_mod, "_game_meta_path", lambda game_path, filename: blocker / filename
    )

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    checkpoint = workspace_root / "translation_checkpoint.json"
    checkpoint.write_text(
        '{"source":"' + str(game).replace("\\", "\\\\") + '","items":['
        '{"file":"a.ks","key":"k","original":"こんにちは","translated":"","meta":{}}]}',
        encoding="utf-8",
    )

    pipe = type("_Pipe", (), {})()
    pipe.diagnostics = _StubDiagnostics()

    from core.pipeline_checkpoint_stage import sync_checkpoint_json

    sync_checkpoint_json(
        pipe,
        checkpoint,
        [TextItem(file="a.ks", key="k", original="こんにちは", translated="你好")],
    )

    assert "你好" in checkpoint.read_text(encoding="utf-8")
    assert pipe.diagnostics.facts["checkpoint_game_mirror"]["ok"] is False


def test_nested_write_probe_detects_child_directory_failure(tmp_path: Path, monkeypatch):
    game = tmp_path / "game"
    game.mkdir()
    real_open = open

    def deny_nested_probe(path, *args, **kwargs):
        if ".engaixt_meta_probe_" in str(path):
            raise PermissionError(13, "拒绝访问。")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(game_dir_access, "open", deny_nested_probe, raising=False)

    error = game_dir_access._write_probe_error(game)

    assert isinstance(error, PermissionError)
    assert list(game.iterdir()) == []


# --------------------------------------------------------------------------
# 流水线：提前停止 + 把原因送到运行日志
# --------------------------------------------------------------------------

def test_pipeline_stops_before_extraction_when_game_dir_is_read_only(
    tmp_path: Path, monkeypatch
):
    game = tmp_path / "game"
    game.mkdir()
    (game / "game.exe").write_bytes(b"MZ")

    events: list = []
    extract_calls: list = []

    monkeypatch.setattr(pipeline_mod, "describe_game_dir_access", lambda path: _read_only_facts(game))
    monkeypatch.setattr(pipeline_mod, "game_pipeline_lock", lambda path: contextlib.nullcontext())
    monkeypatch.setattr(
        Pipeline, "_run_detection_stage", lambda self, path, injector: (_StubEngine(), injector)
    )
    monkeypatch.setattr(
        Pipeline,
        "_run_extract_stage",
        lambda self, path, engine, file_filter=None: (extract_calls.append(path), ([], engine, 0))[1],
    )

    pipe = _make_pipeline(events)

    assert pipe.run_with_checkpoint(str(game), launch=False) is False

    assert extract_calls == [], "只读游戏目录必须在解包文本之前就停止"
    assert pipe.diagnostics.data["game_dir_access"]["writable"] is False
    failure = _failures(events)[-1]
    assert failure["code"] == "game_dir_read_only"
    assert "不可写" in failure["user_message"]
    assert failure["next_actions"], "结构化失败必须带上可执行的下一步"


def test_extract_only_still_allowed_on_read_only_game_dir(tmp_path: Path, monkeypatch):
    """仅提取只读游戏资源、只写工作区，是用于只读介质的合法用法。"""
    game = tmp_path / "game"
    game.mkdir()
    (game / "game.exe").write_bytes(b"MZ")

    events: list = []
    extract_calls: list = []

    monkeypatch.setattr(pipeline_mod, "describe_game_dir_access", lambda path: _read_only_facts(game))
    monkeypatch.setattr(pipeline_mod, "game_pipeline_lock", lambda path: contextlib.nullcontext())
    monkeypatch.setattr(
        Pipeline, "_run_detection_stage", lambda self, path, injector: (_StubEngine(), injector)
    )
    monkeypatch.setattr(
        Pipeline,
        "_run_extract_stage",
        lambda self, path, engine, file_filter=None: (extract_calls.append(path), ([], engine, 0))[1],
    )

    pipe = _make_pipeline(events)

    assert pipe.run_with_checkpoint(str(game), launch=False, extract_only=True) is False
    assert extract_calls == [game], "仅提取模式必须继续走解包"
    assert "game_dir_access" not in pipe.diagnostics.data


def test_unhandled_exception_is_published_as_structured_failure(tmp_path: Path, monkeypatch):
    """回归：未捕获异常以前只写日志，GUI 运行日志完全没有原因。"""
    game = tmp_path / "game"
    game.mkdir()

    events: list = []

    async def _boom(*args, **kwargs):
        raise PermissionError(5, "拒绝访问。", r"F:\王様恋愛\_translation_meta")

    monkeypatch.setattr(pipeline_mod, "game_pipeline_lock", lambda path: contextlib.nullcontext())
    monkeypatch.setattr(Pipeline, "_run_checkpoint_async", _boom)

    pipe = _make_pipeline(events)

    assert pipe.run_with_checkpoint(str(game), launch=False) is False

    failure = _failures(events)[-1]
    assert failure["code"] == "pipeline_exception"
    assert "拒绝访问" in failure["user_message"]
    assert "PermissionError" in failure["technical_detail"]
