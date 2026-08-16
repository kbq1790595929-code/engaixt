"""Unity asset repack via UnityPy — proper deserialize/modify/reserialize."""
from __future__ import annotations

import shutil
from pathlib import Path


def repack_assets(asset_dir: Path, translations: dict[str, str]) -> int:
    """Replace Japanese text with Chinese in Unity .assets files using UnityPy.
    Returns number of strings replaced.
    """
    try:
        import UnityPy
    except ImportError:
        from utils.logger import warning
        warning("UnityPy 未安装，无法回填")
        return 0

    asset_files = (list(asset_dir.glob("*.assets")) +
                   list(asset_dir.glob("sharedassets*")) +
                   list(asset_dir.glob("level*")))

    total = 0
    for af in asset_files:
        if af.suffix not in (".assets", ""):
            continue
        if not af.exists():
            continue

        count = _repack_one(af, translations)
        if count > 0:
            total += count

    return total


def _repack_one(asset_file: Path, translations: dict[str, str]) -> int:
    """Repack a single Unity asset file."""
    import UnityPy

    backup = asset_file.with_suffix(asset_file.suffix + ".pre_tool")
    if not backup.exists():
        shutil.copy2(asset_file, backup)

    try:
        env = UnityPy.load(str(asset_file))
    except Exception:
        return 0

    replaced = 0
    for obj in env.objects:
        try:
            data = obj.read()
            count = _replace_in_object(data, translations)
            if count > 0:
                obj.save(data)
                replaced += count
        except Exception:
            continue

    if replaced > 0:
        try:
            env.save()
        except Exception:
            pass

    return replaced


def _replace_in_object(data, translations: dict[str, str]) -> int:
    """Recursively replace strings in a UnityPy object's data tree."""
    count = 0

    # Walk the object tree (UnityPy types are complex)
    for attr_name in dir(data):
        if attr_name.startswith("_"):
            continue
        try:
            val = getattr(data, attr_name)
        except Exception:
            continue

        if isinstance(val, str) and val in translations:
            new_val = translations[val]
            if new_val != val:
                try:
                    setattr(data, attr_name, new_val)
                    count += 1
                except Exception:
                    pass
        elif isinstance(val, list):
            for i, item in enumerate(val):
                if isinstance(item, str) and item in translations:
                    new_val = translations[item]
                    if new_val != item:
                        try:
                            val[i] = new_val
                            count += 1
                        except Exception:
                            pass
                elif hasattr(item, "__dict__"):
                    count += _replace_in_object(item, translations)
        elif hasattr(val, "__dict__"):
            count += _replace_in_object(val, translations)

    return count
