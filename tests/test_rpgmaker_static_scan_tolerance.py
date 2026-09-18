"""RPGMaker 静态扫描容错：加密/损坏的数据文件跳过，不炸整条管线。

真实案例：带保护插件的游戏只加密 CommonEvents.json，其余明文。
静态扫描必须从可读文件继续提取，而不是 JSONDecodeError 中断翻译。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.rpgmaker_runtime import _load_data_json, _scan_static


def _make_game(tmp_path: Path) -> Path:
    game = tmp_path / "game"
    data = game / "data"
    data.mkdir(parents=True)
    # 加密的 CommonEvents（base64 样式密文，非 JSON）
    (data / "CommonEvents.json").write_bytes(b"px52tJzMreWGUI45bu2D306DWiA9JEHz")
    # 带 BOM 的 System.json（合法 JSON + UTF-8 BOM）
    (data / "System.json").write_bytes(
        b"\xef\xbb\xbf" + json.dumps({"gameTitle": "テストゲーム"}).encode("utf-8")
    )
    # 明文地图与数据库
    (data / "Map001.json").write_text(json.dumps({
        "events": [None, {
            "id": 1, "name": "EV001",
            "pages": [{"list": [
                {"code": 401, "parameters": ["こんにちは、世界"]},
            ]}],
        }],
    }), encoding="utf-8")
    (data / "Actors.json").write_text(json.dumps(
        [None, {"id": 1, "name": "サキュバス", "profile": "テスト"}]
    ), encoding="utf-8")
    return game


def test_scan_static_survives_encrypted_common_events(tmp_path: Path):
    items = _scan_static(_make_game(tmp_path))
    texts = [item.original for item in items]
    assert any("こんにちは、世界" in t for t in texts), "明文地图文本必须照常提取"
    assert any("サキュバス" in t for t in texts), "明文数据库文本必须照常提取"


def test_scan_static_reads_bom_json(tmp_path: Path):
    items = _scan_static(_make_game(tmp_path))
    texts = [item.original for item in items]
    assert any("テストゲーム" in t for t in texts), "带 BOM 的 System.json 必须能解析"


def test_load_data_json_returns_none_for_garbage(tmp_path: Path):
    bad = tmp_path / "CommonEvents.json"
    bad.write_bytes(b"px52tJzMreWGUI45")
    assert _load_data_json(bad) is None


def test_load_data_json_accepts_bom(tmp_path: Path):
    good = tmp_path / "System.json"
    good.write_bytes(b"\xef\xbb\xbf" + b'{"gameTitle": "ok"}')
    assert _load_data_json(good) == {"gameTitle": "ok"}
