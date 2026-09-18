from pathlib import Path

from core.manifest import GameManifest


def test_xunity_tmp_font_asset_is_tracked_as_a_tool_artifact(tmp_path: Path):
    asset = tmp_path / "NotoSansSC_sdf32_optimized_12k_lz4_2020"
    asset.write_bytes(b"tmp-font-asset")

    artifacts = GameManifest.for_game(tmp_path).snapshot_tool_artifacts()

    assert str(asset.resolve()) in artifacts
