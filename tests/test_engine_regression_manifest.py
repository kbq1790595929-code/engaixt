import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.detector import _ensure_engines_loaded
from engines import registry


MANIFEST = Path(__file__).parent / "fixtures" / "engine_regression_manifest.json"


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def test_regression_manifest_tracks_registered_engines():
    _ensure_engines_loaded()
    manifest = _manifest()
    declared = set(manifest["engines"])
    registered = {engine.name for engine in registry.list_engines() if engine.name != "base"}
    assert registered <= declared


def test_regression_manifest_has_release_contract_for_each_engine():
    manifest = _manifest()
    allowed_kinds = set(manifest["sample_schema"]["kinds"])
    allowed_covers = set(manifest["sample_schema"]["covers"])
    for name, entry in manifest["engines"].items():
        assert entry["status"] in {"stable", "beta", "experimental", "partial", "planned"}, name
        assert entry["automated_tests"], name
        assert entry["manual_smoke"], name
        assert entry["regression_samples"], name
        assert all(path.startswith("tests/") for path in entry["automated_tests"]), name
        for sample in entry["regression_samples"]:
            assert sample["id"], name
            assert sample["kind"] in allowed_kinds, (name, sample["id"])
            assert isinstance(sample["automated"], bool), (name, sample["id"])
            assert sample["covers"], (name, sample["id"])
            assert set(sample["covers"]) <= allowed_covers, (name, sample["id"])
            if sample["automated"]:
                assert sample["tests"], (name, sample["id"])
                assert set(sample["tests"]) <= set(entry["automated_tests"]), (name, sample["id"])


def test_regression_manifest_status_matches_engine_support_level():
    _ensure_engines_loaded()
    manifest = _manifest()
    by_name = {engine.name: engine for engine in registry.list_engines()}
    for name, entry in manifest["engines"].items():
        if name not in by_name:
            continue
        assert entry["status"] == getattr(by_name[name], "support_level", "stable")


def test_regression_manifest_capabilities_match_engine_declarations():
    _ensure_engines_loaded()
    manifest = _manifest()
    by_name = {engine.name: engine for engine in registry.list_engines()}
    keys = [
        "extract",
        "repack",
        "static_patch",
        "runtime_patch",
        "creates_launcher",
        "portable_after_patch",
        "requires_python",
        "requires_frida",
        "needs_external_tool",
    ]
    for name, entry in manifest["engines"].items():
        if name not in by_name:
            continue
        runtime_caps = by_name[name].get_capabilities().to_dict()
        declared_caps = entry["capabilities"]
        assert set(declared_caps) == set(keys), name
        for key in keys:
            assert declared_caps[key] is runtime_caps[key], (name, key)


def test_regression_manifest_referenced_automated_tests_exist():
    manifest = _manifest()
    root = Path(__file__).parent.parent
    for name, entry in manifest["engines"].items():
        for test_ref in entry["automated_tests"]:
            path_part = test_ref.split("::", 1)[0]
            assert (root / path_part).exists(), (name, test_ref)
