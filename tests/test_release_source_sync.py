import json
from pathlib import Path


def test_release_staging_preserves_dist_executable():
    root = Path(__file__).resolve().parents[1]
    script = (root / 'tools/release/release.ps1').read_text(encoding='utf-8')
    assert 'Move-Item -LiteralPath $distExe' not in script
    assert 'Move-Item -LiteralPath (Join-Path $pkgRoot (Split-Path $distExe -Leaf))' in script


def test_public_export_includes_branding_test_dependencies():
    root = Path(__file__).resolve().parents[1]
    manifest_path = root / 'tools/release/public-files.json'
    if not manifest_path.is_file():
        # Public checkouts already contain the exported helper files.
        assert (root / 'tools/release/branding.json').is_file()
        assert (root / 'tools/release/verify_branding.py').is_file()
        return
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    assert {'tools/release/branding.json', 'tools/release/verify_branding.py',
            'tools/release/release.ps1', 'tools/release/baseline-files.json'} <= set(manifest['selected_files'])
