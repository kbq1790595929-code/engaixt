from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from engines.base import TextItem
from engines.kirikiri import external_tools


class _Result:
    returncode = 0
    stdout = b""
    stderr = b""


def test_msg_tool_zero_exit_without_output_is_not_success(tmp_path: Path, monkeypatch):
    archive = tmp_path / "data.xp3"
    archive.write_bytes(b"XP3")
    output = tmp_path / "candidate"
    output.mkdir()
    (output / "stale.ks").write_text("stale", encoding="utf-8")
    tool = tmp_path / "msg-tool.exe"
    tool.write_bytes(b"MZ")

    monkeypatch.setattr(external_tools, "find_tool", lambda _name: tool)
    monkeypatch.setattr(external_tools.subprocess, "run", lambda *args, **kwargs: _Result())

    result = external_tools.run_msg_tool_unpack(archive, output)

    assert result.status == "no_output"
    assert result.ok is False
    assert not (output / "stale.ks").exists()


def test_msg_tool_timeout_is_reported(tmp_path: Path, monkeypatch):
    archive = tmp_path / "data.xp3"
    archive.write_bytes(b"XP3")
    tool = tmp_path / "msg-tool.exe"
    tool.write_bytes(b"MZ")

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("msg-tool", 1)

    monkeypatch.setattr(external_tools, "find_tool", lambda _name: tool)
    monkeypatch.setattr(external_tools.subprocess, "run", timeout)

    result = external_tools.run_msg_tool_unpack(archive, tmp_path / "candidate")

    assert result.status == "timeout"
    assert result.ok is False


def test_msg_tool_invalid_scn_output_does_not_satisfy_script_contract(tmp_path: Path, monkeypatch):
    archive = tmp_path / "data.xp3"
    archive.write_bytes(b"XP3")
    tool = tmp_path / "msg-tool.exe"
    tool.write_bytes(b"MZ")

    def fake_run(_args, cwd=None, **_kwargs):
        (Path(cwd) / "broken.scn").write_bytes(b"not a PSB script")
        return _Result()

    monkeypatch.setattr(external_tools, "find_tool", lambda _name: tool)
    monkeypatch.setattr(external_tools.subprocess, "run", fake_run)

    result = external_tools.run_msg_tool_unpack(archive, tmp_path / "candidate")

    assert result.status == "invalid_script_output"
    assert result.ok is False


def test_vntextpatch_json_conversion_preserves_roles_and_source_file(tmp_path: Path):
    source_root = tmp_path / "source"
    source_root.mkdir()
    (source_root / "scenario.ks").write_text("#name\nmessage", encoding="utf-8")
    export = tmp_path / "export"
    export.mkdir()
    (export / "scenario.json").write_text(
        json.dumps([{"name": "桜", "message": "こんにちは"}], ensure_ascii=False),
        encoding="utf-8",
    )

    rows = external_tools.load_vntextpatch_json_items(export, source_root)

    assert [(row["context"], row["original"], row["file"]) for row in rows] == [
        ("speaker", "桜", "scenario.ks"),
        ("message", "こんにちは", "scenario.ks"),
    ]


def test_vntextpatch_translation_json_replaces_name_and_message(tmp_path: Path):
    source = tmp_path / "scenario.json"
    source.write_text(
        json.dumps([{"name": "桜", "message": "こんにちは"}], ensure_ascii=False),
        encoding="utf-8",
    )
    output = tmp_path / "translations" / "scenario.json"
    items = [
        TextItem(file="scenario.ks", original="桜", translated="樱", context="speaker"),
        TextItem(file="scenario.ks", original="こんにちは", translated="你好", context="message"),
    ]

    changed = external_tools.write_vntextpatch_translation_json(source, output, items)

    assert changed == 2
    assert json.loads(output.read_text(encoding="utf-8")) == [{"name": "樱", "message": "你好"}]


def test_external_script_promotion_rejects_path_traversal(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "good.ks").write_text("good", encoding="utf-8")

    promoted = external_tools.promote_script_files(
        source,
        target,
        ["../escape.ks", "C:/escape.ks", "good.ks"],
    )

    assert promoted == 1
    assert (target / "good.ks").exists()
    assert not (tmp_path / "escape.ks").exists()


def test_xp3pack_requires_archive_output_and_records_command(tmp_path: Path, monkeypatch):
    input_dir = tmp_path / "patch_input"
    input_dir.mkdir()
    (input_dir / "intro.ks").write_text("hello", encoding="utf-8")
    output = tmp_path / "out" / "patch.xp3"
    tool = tmp_path / "Xp3Pack.exe"
    tool.write_bytes(b"MZ")

    calls = []

    def fake_run(args, cwd=None, **_kwargs):
        calls.append((args, cwd))
        (Path(cwd) / "patch_input.xp3").write_bytes(b"XP3")
        return _Result()

    monkeypatch.setattr(external_tools, "find_tool", lambda _name: tool)
    monkeypatch.setattr(external_tools.subprocess, "run", fake_run)

    result = external_tools.run_xp3pack(input_dir, output)

    assert result.status == "success"
    assert result.ok is True
    assert output.exists()
    assert calls[0][0] == [str(tool), "patch_input"]
    assert Path(calls[0][1]) == input_dir.parent


def test_kirikiri_component_manifest_has_release_provenance_and_contracts():
    root = Path(__file__).parent.parent
    manifest = json.loads(
        (root / "_internal" / "tools" / "kirikiri" / "components.json").read_text(encoding="utf-8")
    )

    assert manifest["policy"] == "static_first_realtime_fallback"
    assert {entry["name"] for entry in manifest["components"]} == {
        "garbro_console",
        "kirikiri_xp3pack",
        "kirikiri_unencrypted_version",
        "vntextpatch",
        "vntextproxy",
        "msg_tool",
    }

    # Bundled sidecars are assembled at packaging time: game-translator.spec
    # copies them into the distribution and core.tool_manager downloads whatever
    # is missing. A source checkout therefore legitimately carries none of them
    # -- only the small textual provenance records are tracked. Enforce the
    # declared paths once at least one sidecar is actually present, so this
    # still catches a manifest that claims something which was never assembled.
    if not any((root / entry["bundled_relative_path"]).exists()
               for entry in manifest["components"] if entry["bundled"]):
        pytest.skip("bundled KiriKiri sidecars are not assembled in this source checkout")

    for entry in manifest["components"]:
        assert entry["engine_scope"] == ["kirikiri"]
        assert entry["source_url"].startswith("https://")
        assert entry["license"]
        assert entry["license_url"].startswith("https://")
        assert "sha256" in entry
        assert entry["output_contract"]
        if entry["bundled"]:
            bundled = root / entry["bundled_relative_path"]
            assert bundled.is_dir(), bundled
        else:
            assert entry["bundled_relative_path"]
