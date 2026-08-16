from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path
from typing import Any

from utils.logger import info, warning


BASE_DIR = Path.home() / "Downloads" / ".game_translator"
BACKUP_DIR = BASE_DIR / "backups"
MANIFEST_DIR = BASE_DIR / "manifests"
WORKSPACES_DIR = BASE_DIR / "workspaces"
MANIFEST_NAME = ".game_translator_manifest.json"


def game_id_for(game_dir: Path) -> str:
    return hashlib.sha256(str(game_dir.resolve()).encode("utf-8", errors="replace")).hexdigest()[:16]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _norm_rel(path: str) -> str:
    return str(path).replace("\\", "/").lstrip("/").casefold()


class GameManifest:
    """Tracks files owned or modified by this tool for one game directory."""

    def __init__(self, game_dir: Path):
        self.game_dir = (game_dir if game_dir.is_dir() else game_dir.parent).resolve()
        self.game_id = game_id_for(self.game_dir)
        self.local_path = self.game_dir / MANIFEST_NAME
        self.global_path = MANIFEST_DIR / f"{self.game_id}.json"
        self.backup_root = BACKUP_DIR / self.game_id
        self.data = self._load()

    @classmethod
    def for_game(cls, game_path: Path) -> "GameManifest":
        return cls(game_path)

    def _load(self) -> dict[str, Any]:
        for path in (self.local_path, self.global_path):
            if path.exists():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    data["game_id"] = self.game_id
                    data["game_dir"] = str(self.game_dir)
                    data.setdefault("modified_files", [])
                    data.setdefault("created_files", [])
                    data.setdefault("notes", [])
                    return data
                except Exception:
                    pass
        return {
            "version": 1,
            "game_id": self.game_id,
            "game_dir": str(self.game_dir),
            "created_at": _now(),
            "updated_at": _now(),
            "engine": "",
            "modified_files": [],
            "created_files": [],
            "notes": [],
        }

    def save(self):
        self.data["updated_at"] = _now()
        self.data["game_dir"] = str(self.game_dir)
        self.data["game_id"] = self.game_id
        MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
        self.global_path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
        try:
            self.local_path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            warning(f"写入游戏目录 manifest 失败: {e}")

    def set_engine(self, engine: object):
        self.data["engine"] = getattr(engine, "name", str(engine))
        self.save()

    def backup_file(self, target: Path) -> Path | None:
        target = target.resolve()
        if not target.exists() or not target.is_file():
            return None
        rel = self._relative(target)
        backup = self.backup_root / rel
        backup.parent.mkdir(parents=True, exist_ok=True)
        if not backup.exists():
            shutil.copy2(target, backup)
            info(f"已集中备份: {target.name} -> {backup}")
        record = {
            "path": str(target),
            "rel": rel,
            "backup": str(backup),
            "original_sha256": sha256_file(backup),
            "first_seen": _now(),
        }
        self._upsert("modified_files", "path", record)
        self.save()
        return backup

    def record_modified(self, target: Path):
        target = target.resolve()
        rel = self._relative(target)
        record = {
            "path": str(target),
            "rel": rel,
            "last_modified_sha256": sha256_file(target) if target.exists() and target.is_file() else "",
            "updated_at": _now(),
        }
        existing = self._find("modified_files", "path", str(target))
        if existing:
            existing.update(record)
        else:
            self.data.setdefault("modified_files", []).append(record)
        self.save()

    def forget_modified_rels(self, rels: set[str]) -> int:
        normalized = {_norm_rel(rel) for rel in rels if rel}
        if not normalized:
            return 0
        before = len(self.data.get("modified_files", []))
        self.data["modified_files"] = [
            item for item in self.data.get("modified_files", [])
            if _norm_rel(str(item.get("rel") or "")) not in normalized
        ]
        removed = before - len(self.data["modified_files"])
        if removed:
            self.save()
        return removed

    def record_created(self, target: Path, kind: str = "tool_artifact",
                       runtime_required: bool = False):
        target = target.resolve()
        if not target.exists():
            return
        record = {
            "path": str(target),
            "rel": self._relative(target),
            "kind": kind,
            "runtime_required": bool(runtime_required),
            "is_dir": target.is_dir(),
            "created_at": _now(),
        }
        self._upsert("created_files", "path", record)
        self.save()

    def snapshot_tool_artifacts(self) -> set[str]:
        return {str(p.resolve()) for p in self._iter_known_artifacts()}

    def record_new_tool_artifacts(self, before: set[str], engine: object | None = None):
        engine_name = getattr(engine, "name", "") if engine else ""
        after = {str(p.resolve()): p for p in self._iter_known_artifacts()}
        for path_str, path in after.items():
            if path_str in before:
                continue
            runtime_required = _is_runtime_required(path, engine_name)
            self.record_created(path, kind="runtime" if runtime_required else "backup_or_temp",
                                runtime_required=runtime_required)

    def restore_and_uninstall(self) -> dict[str, int]:
        stats = {"restored": 0, "deleted": 0, "missing_backups": 0, "errors": 0}

        # ── 前置: 撤销 HTML/JS hook 注入和 package.json 修改 ──
        stats["hook_entries_removed"] = self._undo_rpgmaker_hook()

        # ── 前置: 恢复外部工具（MTool 等）创建的备份 ──
        stats = self._restore_external_backups(stats)

        for item in self.data.get("modified_files", []):
            target = self._manifest_item_path(item)
            backup = Path(item.get("backup", ""))
            if not backup.exists():
                stats["missing_backups"] += 1
                continue
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup, target)
                stats["restored"] += 1
            except Exception as e:
                warning(f"恢复失败: {target} - {e}")
                stats["errors"] += 1

        # Restore .pre_tool backups before deleting tool-owned artifacts.
        for item in self.data.get("created_files", []):
            path = self._manifest_item_path(item)
            if path.suffix == ".pre_tool" and path.exists():
                target = Path(str(path).replace(".pre_tool", ""))
                try:
                    shutil.copy2(path, target)
                    stats["restored"] += 1
                except Exception as e:
                    warning(f"恢复 .pre_tool 失败: {path} - {e}")
                    stats["errors"] += 1

        for item in sorted(self.data.get("created_files", []),
                           key=lambda r: len(Path(r.get("path", "")).parts),
                           reverse=True):
            path = self._manifest_item_path(item)
            if not path.exists():
                continue
            try:
                if path.is_dir():
                    # Only remove empty directories recorded by the manifest.
                    path.rmdir()
                else:
                    path.unlink()
                stats["deleted"] += 1
            except OSError:
                # Non-empty runtime directories are intentionally left alone.
                continue
            except Exception as e:
                warning(f"删除工具文件失败: {path} - {e}")
                stats["errors"] += 1

        # ── 兜底: 清理已知 artifact 路径（处理旧版翻译未记录 created_files 的情况）──
        for rel in ("js/rpgmaker_hook.js", "www/js/rpgmaker_hook.js",
                     "save/hook_translation_map.json", "www/save/hook_translation_map.json",
                     "save/hook_text.json", "www/save/hook_text.json"):
            p = self.game_dir / rel
            if p.exists():
                try:
                    if p.is_dir():
                        shutil.rmtree(p)
                    else:
                        p.unlink()
                    stats["deleted"] += 1
                except Exception:
                    pass

        for path in (self.local_path, self.global_path):
            try:
                if path.exists():
                    path.unlink()
            except Exception:
                pass

        return stats

    def _undo_rpgmaker_hook(self) -> int:
        """撤销 RPG Maker/runtime 注入：还原 index.html 和 package.json。"""
        removed = 0
        # 1) 从 index.html 移除 hook script 标签
        for rel in ("index.html", "www/index.html"):
            html = self.game_dir / rel
            if not html.exists():
                continue
            try:
                content = html.read_text(encoding="utf-8")
                new_content = _remove_hook_injection(content)
                if new_content != content:
                    html.write_text(new_content, encoding="utf-8")
                    removed += 1
                    info(f"已移除 RPG Maker hook 注入: {html}")
            except Exception as e:
                warning(f"还原 {rel} 失败: {e}")

        # 1b) RPG Maker MV fallback injection writes into js/main.js scriptUrls.
        for rel in ("js/main.js", "www/js/main.js"):
            main_js = self.game_dir / rel
            if not main_js.exists():
                continue
            try:
                content = main_js.read_text(encoding="utf-8")
                new_content = _remove_hook_from_main_js(content)
                if new_content != content:
                    main_js.write_text(new_content, encoding="utf-8")
                    removed += 1
                    info(f"已移除 RPG Maker main.js hook 注入: {main_js}")
            except Exception as e:
                warning(f"还原 {rel} 失败: {e}")

        # 2) 从 package.json 移除 nodejs 标记（工具注入的，不是游戏自带的）
        for rel in ("package.json", "www/package.json"):
            pkg = self.game_dir / rel
            if not pkg.exists():
                continue
            try:
                data = json.loads(pkg.read_text(encoding="utf-8"))
                if data.get("nodejs") is True:
                    # 检查是否原本就有 nodejs 标记：查看备份中是否有这个文件
                    has_backup = any(
                        Path(m.get("backup", "")).exists()
                        for m in self.data.get("modified_files", [])
                        if m.get("path", "").replace("\\", "/").endswith(rel.replace("\\", "/"))
                    )
                    if not has_backup:
                        del data["nodejs"]
                        # 如果 nodejs 还原后变成空对象，不要删
                        pkg.write_text(json.dumps(data, indent=4, ensure_ascii=False), encoding="utf-8")
                        info(f"已还原 package.json（移除 nodejs 标记）: {pkg}")
            except Exception:
                pass
        return removed

    def _manifest_item_path(self, item: dict) -> Path:
        rel = str(item.get("rel") or "").strip()
        if rel:
            return self.game_dir / rel
        raw = str(item.get("path") or "").strip()
        return Path(raw) if raw else self.game_dir

    def _restore_external_backups(self, stats: dict) -> dict:
        """恢复外部工具（MTool 等）创建的数据备份文件。

        MTool 的 'hook mode repair' 会修改 www/data/*.json 和 www/js/plugins.js，
        同时创建 .pre_hook_mode_repair_bak / .repair_bak 等备份。
        卸载时需要从这些备份还原原始游戏文件，否则游戏可能无法正常加载。

        优先使用 .pre_hook_mode_repair_bak（最早的备份），避免多重备份互相覆盖。
        """
        # 按优先级排序：.pre_hook_mode_repair_bak 最早最原始
        backup_suffixes = [
            ".pre_hook_mode_repair_bak",
            ".resource_repair_bak",
            ".repair_bak",
            ".hook_rebuild_bak",
        ]
        # 只恢复游戏数据目录下的备份，跳过 save/ 等工具产物目录
        game_roots = ["www/data", "www/js", "data", "js"]

        for root_rel in game_roots:
            root = self.game_dir / root_rel
            if not root.exists():
                continue
            # 收集这个 root 下所有备份文件，按原始文件分组
            backups_by_orig: dict[str, list[Path]] = {}
            for bak in root.rglob("*"):
                if not bak.is_file():
                    continue
                matched_suffix = None
                for suffix in backup_suffixes:
                    if bak.name.endswith(suffix):
                        matched_suffix = suffix
                        break
                if not matched_suffix:
                    continue
                orig_name = bak.name.replace(matched_suffix, "")
                backups_by_orig.setdefault(orig_name, []).append(bak)

            # 对每个原始文件，只取优先级最高的备份（排最前的 suffix）
            for orig_name, baks in backups_by_orig.items():
                orig = root / orig_name
                if not orig.exists():
                    continue
                # 按优先级取最早备份
                best_bak = None
                for suffix in backup_suffixes:
                    for bak in baks:
                        if bak.name.endswith(suffix):
                            best_bak = bak
                            break
                    if best_bak:
                        break
                if not best_bak:
                    continue
                try:
                    if best_bak.read_bytes() == orig.read_bytes():
                        continue  # 已一致，跳过
                    shutil.copy2(str(best_bak), str(orig))
                    stats["restored"] += 1
                    info(f"从外部备份恢复: {best_bak.relative_to(self.game_dir)} -> {orig.relative_to(self.game_dir)}")
                except Exception as e:
                    warning(f"恢复外部备份失败: {best_bak} - {e}")
                    stats["errors"] += 1
        return stats

    def _iter_known_artifacts(self):
        roots = [
            self.game_dir / "BepInEx",
            self.game_dir / "trans",
            self.game_dir / ".godot",
        ]
        files = []
        for root in roots:
            if root.exists():
                if root.is_file():
                    files.append(root)
                else:
                    files.extend([p for p in root.rglob("*") if p.exists()])
        for name in [
            "doorstop_config.ini", "version.dll", "winhttp.dll", "xinput9_1_0.dll",
            "UnityPlayer_.dll", "patch.xp3",
            "NotoSansSC_sdf32_optimized_12k_lz4_2020",
        ]:
            p = self.game_dir / name
            if p.exists():
                files.append(p)
        # RPG Maker runtime hook artifacts
        for rel in ("js/rpgmaker_hook.js", "www/js/rpgmaker_hook.js",
                     "save/hook_translation_map.json", "www/save/hook_translation_map.json",
                     "save/hook_text.json", "www/save/hook_text.json"):
            p = self.game_dir / rel
            if p.exists():
                files.append(p)
        # RPG Maker MZ nodejs flag in package.json
        for rel in ("package.json", "www/package.json"):
            p = self.game_dir / rel
            if p.exists():
                files.append(p)
        files.extend(self.game_dir.rglob("*.pre_tool"))
        return files

    def _relative(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.game_dir))
        except ValueError:
            return path.name

    def _find(self, collection: str, key: str, value: str):
        for item in self.data.setdefault(collection, []):
            if item.get(key) == value:
                return item
        return None

    def _upsert(self, collection: str, key: str, record: dict):
        existing = self._find(collection, key, record[key])
        if existing:
            existing.update(record)
        else:
            self.data.setdefault(collection, []).append(record)


def cleanup_temporary_files(keep_recent_failed: int = 5) -> dict[str, int]:
    stats = {
        "deleted_jobs": 0,
        "deleted_files": 0,
        "deleted_dirs": 0,
        "bytes_freed": 0,
        "kept_jobs": 0,
        "pruned_games": 0,
        "deleted_manifests": 0,
        "deleted_backup_dirs": 0,
        "deleted_cache_files": 0,
        "deleted_orphan_workspaces": 0,
        "errors": 0,
    }
    _merge_stats(stats, prune_missing_games(remove_workspaces=True))
    if not WORKSPACES_DIR.exists():
        return stats

    job_dirs = [p for p in WORKSPACES_DIR.iterdir() if p.is_dir() and p.name.startswith("job_")]
    failed_or_extract = []
    deletable = []
    for job in job_dirs:
        diag = _read_json(job / "diagnostics.json")
        mode = diag.get("mode", {}) if diag else {}
        success = bool(diag.get("success")) if diag else False
        if mode.get("extract_only") or not success:
            failed_or_extract.append(job)
        else:
            deletable.append(job)

    failed_or_extract.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    deletable.extend(failed_or_extract[keep_recent_failed:])
    stats["kept_jobs"] = min(len(failed_or_extract), keep_recent_failed)

    for job in deletable:
        size = _dir_size(job)
        try:
            shutil.rmtree(job)
            stats["deleted_jobs"] += 1
            stats["bytes_freed"] += size
        except Exception as e:
            warning(f"删除工作区失败: {job} - {e}")

    for p in WORKSPACES_DIR.glob("*.tmp"):
        try:
            size = p.stat().st_size
            p.unlink()
            stats["deleted_files"] += 1
            stats["bytes_freed"] += size
        except Exception:
            pass

    # Old versions sometimes left runtime files directly in workspaces/.
    # These are not game directories and are safe to remove as temporary residue.
    residue_names = {
        "BepInEx", "dotnet", "_test_prefetch",
        "version.dll", "winhttp.dll", "xinput9_1_0.dll",
        "doorstop_config.ini", ".doorstop_version", "changelog.txt",
    }
    for p in WORKSPACES_DIR.iterdir():
        if p.name not in residue_names:
            continue
        try:
            size = _dir_size(p) if p.is_dir() else p.stat().st_size
            if p.is_dir():
                shutil.rmtree(p)
                stats["deleted_dirs"] += 1
            else:
                p.unlink()
                stats["deleted_files"] += 1
            stats["bytes_freed"] += size
        except Exception as e:
            warning(f"删除工作区残留失败: {p} - {e}")

    return stats


def prune_missing_games(remove_workspaces: bool = False) -> dict[str, int]:
    """Remove tool-side state for manifests whose game directory no longer exists."""
    stats = {
        "pruned_games": 0,
        "deleted_manifests": 0,
        "deleted_backup_dirs": 0,
        "deleted_cache_files": 0,
        "deleted_orphan_workspaces": 0,
        "deleted_files": 0,
        "deleted_dirs": 0,
        "bytes_freed": 0,
        "errors": 0,
    }
    if not MANIFEST_DIR.exists():
        return stats

    missing_workspace_targets: set[str] = set()
    for manifest_file in list(MANIFEST_DIR.glob("*.json")):
        data = _read_json(manifest_file)
        game_dir_text = str(data.get("game_dir") or "").strip()
        game_path = Path(game_dir_text) if game_dir_text else None
        if game_path and game_path.exists():
            continue

        game_id = str(data.get("game_id") or manifest_file.stem)
        if game_dir_text:
            stats["pruned_games"] += 1
            info(f"Pruning deleted game from translator state: {game_dir_text}")

        _delete_file(manifest_file, stats, "deleted_manifests")

        backup_root = BACKUP_DIR / game_id
        _delete_tree(backup_root, stats, "deleted_backup_dirs")

        if game_dir_text:
            try:
                from core.translation_cache_db import delete_cache_for_game
                cache_stats = delete_cache_for_game(Path(game_dir_text))
                stats["deleted_cache_files"] += cache_stats.get("deleted_files", 0)
                stats["bytes_freed"] += cache_stats.get("bytes_freed", 0)
                stats["errors"] += cache_stats.get("errors", 0)
            except Exception as e:
                stats["errors"] += 1
                warning(f"Failed to prune translation cache for {game_dir_text}: {e}")

            if remove_workspaces:
                expected = _norm_path(Path(game_dir_text))
                if expected:
                    missing_workspace_targets.add(expected)

    if remove_workspaces and missing_workspace_targets:
        _prune_workspaces_for_games(missing_workspace_targets, stats)

    return stats


def _remove_hook_injection(content: str) -> str:
    """移除 index.html 中的 RPG Maker hook 注入脚本标签。

    只去掉工具注入的标签，保留其他第三方脚本。
    匹配: <script ... src="js/rpgmaker_hook.js"></script>
    """
    import re
    # 匹配完整的 <script> 标签（含 defer/async/type 属性）
    content, _ = re.subn(
        r'\s*<script\b(?=[^>]*\bsrc\s*=\s*["\'](?:\./)?(?:www/)?js/rpgmaker_hook\.js["\'])[^>]*>\s*</script\s*>',
        '',
        content,
        flags=re.IGNORECASE,
    )
    # 去掉注入后在 main.js 前插入时的多余空格/换行
    content = content.replace('</script>\n        <script', '</script>\n    <script')
    # 去掉尾部的空行
    content = content.rstrip() + '\n'
    return content


def _remove_hook_from_main_js(content: str) -> str:
    """Remove RPGMaker hook entries from MV-style js/main.js script arrays."""
    import re

    content, _ = re.subn(
        r'^[ \t]*["\'](?:\./)?js/rpgmaker_hook\.js["\'][ \t]*,?[ \t]*(?:\r?\n|$)',
        '',
        content,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    content = re.sub(
        r'["\'](?:\./)?js/rpgmaker_hook\.js["\']\s*,\s*',
        '',
        content,
        flags=re.IGNORECASE,
    )
    content = re.sub(
        r'\s*,\s*["\'](?:\./)?js/rpgmaker_hook\.js["\']',
        '',
        content,
        flags=re.IGNORECASE,
    )
    content = re.sub(
        r',(\s*[\]\)])',
        r'\1',
        content,
    )
    content = content.rstrip() + '\n'
    return content


def uninstall_game_translation(game_path: Path) -> dict[str, int]:
    manifest = GameManifest.for_game(game_path)
    result = manifest.restore_and_uninstall()

    # ── 清理翻译缓存 ──
    _merge_stats(result, _clear_translation_cache_for_game(manifest.game_dir))

    return result


def _clear_translation_cache_for_game(game_dir: Path) -> dict[str, int]:
    try:
        from core.translation_cache_db import delete_cache_for_game
        return delete_cache_for_game(game_dir)
    except Exception as e:
        warning(f"娓呴櫎娓告垙缂撳瓨澶辫触: {e}")
        return {"deleted_files": 0, "bytes_freed": 0, "errors": 1}


def _clear_translation_cache_legacy_unused():
    raise RuntimeError("Use _clear_translation_cache_for_game(game_dir) instead.")
    """清空所有翻译缓存。"""
    import os

    # 1) 翻译代理持久缓存
    proxy_cache = Path.home() / "Downloads" / ".game_translator" / "proxy_cache.json"
    if proxy_cache.exists():
        try:
            proxy_cache.unlink()
            info("已清除翻译代理缓存")
        except Exception as e:
            warning(f"清除代理缓存失败: {e}")

    # 2) 游戏维度 SQLite 缓存
    for base in [
        Path.home() / ".game_translator" / "cache",
        Path.home() / "Downloads" / ".game_translator",
    ]:
        if not base.is_dir():
            continue
        for f in list(base.glob("translations_*.db")) + list(base.glob("translation_cache.db")):
            try:
                size = f.stat().st_size
                os.remove(str(f))
                info(f"已清除翻译缓存: {f.name} ({size // 1024} KB)")
            except Exception as e:
                warning(f"清除缓存失败 {f.name}: {e}")

    # 3) 内存缓存
    try:
        from utils import translation_server as ts
        if hasattr(ts, '_translation_cache') and ts._translation_cache:
            with ts._cache_lock:
                ts._translation_cache.clear()
                ts._cache_access_order.clear()
    except ImportError:
        pass


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _merge_stats(target: dict[str, int], source: dict[str, int]) -> dict[str, int]:
    for key, value in source.items():
        if isinstance(value, int):
            target[key] = int(target.get(key, 0)) + value
    return target


def _delete_file(path: Path, stats: dict[str, int], counter: str = "deleted_files") -> bool:
    if not path.exists() or not path.is_file():
        return False
    try:
        size = path.stat().st_size
        path.unlink()
        stats[counter] = stats.get(counter, 0) + 1
        if counter != "deleted_files":
            stats["deleted_files"] = stats.get("deleted_files", 0) + 1
        stats["bytes_freed"] = stats.get("bytes_freed", 0) + size
        return True
    except Exception as e:
        stats["errors"] = stats.get("errors", 0) + 1
        warning(f"Delete file failed: {path} - {e}")
        return False


def _delete_tree(path: Path, stats: dict[str, int], counter: str = "deleted_dirs") -> bool:
    if not path.exists() or not path.is_dir():
        return False
    try:
        size = _dir_size(path)
        shutil.rmtree(path)
        stats[counter] = stats.get(counter, 0) + 1
        if counter != "deleted_dirs":
            stats["deleted_dirs"] = stats.get("deleted_dirs", 0) + 1
        stats["bytes_freed"] = stats.get("bytes_freed", 0) + size
        return True
    except Exception as e:
        stats["errors"] = stats.get("errors", 0) + 1
        warning(f"Delete directory failed: {path} - {e}")
        return False


def _prune_workspaces_for_game(game_dir: Path, stats: dict[str, int]) -> None:
    if not WORKSPACES_DIR.exists():
        return
    expected = _norm_path(game_dir)
    if not expected:
        return
    _prune_workspaces_for_games({expected}, stats)


def _prune_workspaces_for_games(expected_paths: set[str], stats: dict[str, int]) -> None:
    if not WORKSPACES_DIR.exists() or not expected_paths:
        return
    for job in WORKSPACES_DIR.glob("job_*"):
        if not job.is_dir():
            continue
        if _workspace_belongs_to_any_game(job, expected_paths):
            _delete_tree(job, stats, "deleted_orphan_workspaces")


def _workspace_belongs_to_game(job: Path, expected: str) -> bool:
    return _workspace_belongs_to_any_game(job, {expected})


def _workspace_belongs_to_any_game(job: Path, expected_paths: set[str]) -> bool:
    candidates: list[str] = []
    diag = _read_json(job / "diagnostics.json")
    for key in ("resolved_game_path", "resolved_input_path"):
        value = diag.get(key)
        if value:
            candidates.append(str(value))

    checkpoint = _read_json(job / "translation_checkpoint.json")
    source = checkpoint.get("source")
    if source:
        candidates.append(str(source))

    for value in candidates:
        normalized = _norm_path(Path(value))
        if normalized in expected_paths:
            return True
    return False


def _norm_path(path: Path) -> str:
    return str(Path(path)).replace("/", "\\").rstrip("\\").casefold()


def _dir_size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except Exception:
            pass
    return total


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _is_runtime_required(path: Path, engine_name: str) -> bool:
    parts = {part.lower() for part in path.parts}
    name = path.name.lower()
    if "bepinex" in parts or "trans" in parts:
        return True
    if name in {"version.dll", "winhttp.dll", "xinput9_1_0.dll", "doorstop_config.ini",
                "unityplayer_.dll"}:
        return True
    if engine_name == "kirikiri" and name == "patch.xp3":
        return True
    if engine_name == "kirikiri" and name in {"kirikiri_native_launcher.exe", "kirikiri_native_hook.dll"}:
        return True
    return engine_name in {"xunity_realtime"} and path.suffix.lower() == ".dll"
