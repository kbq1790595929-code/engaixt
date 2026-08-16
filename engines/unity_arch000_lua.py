"""Unity @ARCH000 Lua visual novel archive engine.

Handles a reusable Unity archive family:
- @ARCH000 prefix (23 bytes) on UnityFS asset bundles
- Main bundle with AssetBundleManifest
- Concatenated UnityFS sub-bundles
- Lua scenario scripts stored as TextAsset in sub-bundles
"""
from __future__ import annotations

import io
import shutil
from pathlib import Path

from engines.base import EngineBase, EngineCapabilities, TextItem, registry
from utils.logger import info, debug, warning
from utils.lua_extract import extract_texts, apply_translations
from utils.unityfs_direct import parse_unityfs, rebuild_unityfs, apply_translations_direct, apply_trailing_shifts


class UnityArch000LuaEngine(EngineBase):
    name = "unity_arch000_lua"
    label = "Unity @ARCH000 Lua VN"
    support_level = "beta"
    capabilities = EngineCapabilities(
        extract=True,
        repack=True,
        static_patch=True,
        runtime_patch=False,
        creates_launcher=False,
        portable_after_patch=True,
        requires_python=False,
        requires_frida=False,
        notes=(
            "处理 @ARCH000 归档中的 Lua 视觉小说脚本文本。",
            "保持归档结构与脚本入口，适合特定 Unity VN 家族。",
        ),
    )
    detect_priority = 89

    def detect(self, path: Path) -> bool:
        """Check for @ARCH000 .arc archives with UnityFS payloads."""
        if path.is_file():
            path = path.parent

        # Check for Archives directory with .arc files
        arc_dir = path / "Archives"
        if not arc_dir.is_dir():
            # Also check parent for nested structure
            for d in path.iterdir():
                if d.is_dir() and (d / "Archives").is_dir():
                    arc_dir = d / "Archives"
                    break

        if not arc_dir.is_dir():
            return False

        arc_files = list(arc_dir.glob("*.arc"))
        if not arc_files:
            return False

        # Check for @ARCH000 signature
        for af in arc_files[:3]:
            try:
                header = af.read_bytes()[:8]
                if header == b"@ARCH000":
                    return True
            except Exception:
                continue

        return False

    def unpack(self, path: Path, workspace: Path) -> list[TextItem]:
        """Extract Lua scripts and parse translatable text."""
        game_dir = path if path.is_dir() else path.parent
        arc_dir = self._find_arc_dir(game_dir)
        if not arc_dir:
            warning("找不到 Archives 目录")
            return []

        # Find the main arc file (largest one, contains manifest)
        arc_files = sorted(arc_dir.glob("*.arc"), key=lambda f: f.stat().st_size, reverse=True)
        if not arc_files:
            warning("找不到 .arc 文件")
            return []

        main_arc = arc_files[0]
        info(f"主资源包: {main_arc.name} ({main_arc.stat().st_size:,} bytes)")

        # 记录游戏根目录和 arc 文件的相对路径，供 repack 使用
        self._setup_arc_path(path, arc_dir, main_arc)

        # Backup
        self.backup(main_arc, workspace)

        # Extract scripts
        lua_scripts = self._extract_lua_scripts(main_arc)
        info(f"提取到 {len(lua_scripts)} 个 Lua 脚本")

        # Parse translatable text from each script
        items: list[TextItem] = []
        for script_name, lua_code in lua_scripts:
            texts = extract_texts(lua_code)
            for t in texts:
                items.append(TextItem(
                    file=script_name,
                    key=f"L{t['line']}_{t['type']}",
                    original=t['original'],
                    context=t.get('context', ''),
                    line=t['line'],
                    meta={
                        'type': t['type'],
                        '_raw': t.get('_raw', t['original']),
                        '_start': t.get('_start', 0),
                        '_end': t.get('_end', 0),
                    },
                ))

        info(f"提取到 {len(items)} 条可翻译文本")
        return items

    def repack(self, items: list[TextItem], workspace: Path) -> None:
        """Apply translations back to Lua scripts and repack the archive."""
        if not items:
            return

        # Group items by file, filtering translated ones
        file_items: dict[str, list[TextItem]] = {}
        for item in items:
            if item.translated and item.translated != item.original:
                file_items.setdefault(item.file, []).append(item)

        if not file_items:
            info("没有已翻译的文本，跳过回填")
            return

        # Find the original arc file — 优先从备份找，其次从游戏目录
        backup_dir = workspace / "backup"
        arc_files = list(backup_dir.glob("*.arc"))
        main_arc = None
        if arc_files:
            main_arc = arc_files[0]
        elif hasattr(self, '_game_root') and hasattr(self, '_arc_rel_path'):
            game_arc = self._game_root / self._arc_rel_path
            if game_arc.exists():
                main_arc = game_arc
                self.backup(main_arc, workspace)
        if not main_arc:
            warning("找不到 .arc 文件（备份和游戏目录都不存在）")
            return
        info(f"回填翻译到: {main_arc.name} ({len(file_items)} 个脚本)")

        try:
            import UnityPy
        except ImportError:
            warning("UnityPy 未安装，无法回填")
            return

        # Phase 1: Build asset_name → (sub_position, sub_start, sub_end) mapping
        data = bytearray(main_arc.read_bytes())
        positions = self._find_unityfs_positions(data)
        unityfs_data = bytes(data[0x17:])

        env = UnityPy.load(io.BytesIO(unityfs_data))
        manifest = None
        for obj in env.objects:
            if obj.type.name == 'AssetBundleManifest':
                manifest = obj.read()
                break
        if not manifest:
            warning("找不到 AssetBundleManifest")
            return

        asset_to_sub: dict[str, int] = {}
        for i, (idx, name) in enumerate(manifest.AssetBundleNames):
            if not name.lower().startswith('src/'):
                continue
            sb_pos = i + 2
            if sb_pos >= len(positions):
                continue
            sub_start = positions[sb_pos]
            sub_end = positions[sb_pos + 1] if sb_pos + 1 < len(positions) else len(data)
            sub_data = bytes(data[sub_start:sub_end])
            try:
                env_sub = UnityPy.load(io.BytesIO(sub_data))
                for obj in env_sub.objects:
                    if obj.type.name == 'TextAsset':
                        d = obj.read()
                        aname = d.m_Name if hasattr(d, 'm_Name') else ''
                        if aname and aname in file_items and aname not in asset_to_sub:
                            asset_to_sub[aname] = sb_pos  # 取首次出现，避免重名覆盖
            except Exception:
                pass

        # Phase 2: Modify each translated asset's sub-bundle
        # Uses direct byte manipulation instead of UnityPy's bf.save()
        # to avoid LZ4HC compression incompatibility and serialization differences.
        modifications: list[tuple[int, int, bytes]] = []  # (start, end, new_data)
        for asset_name, sb_pos in asset_to_sub.items():
            sub_start = positions[sb_pos]
            sub_end = positions[sb_pos + 1] if sb_pos + 1 < len(positions) else len(data)
            sub_data = bytes(data[sub_start:sub_end])

            try:
                # Parse sub-bundle and get decompressed serialized data
                unityfs_info = parse_unityfs(sub_data)
                decompressed = unityfs_info['decompressed']

                # Use UnityPy to read the TextAsset and get Lua code
                env_sub = UnityPy.load(io.BytesIO(sub_data))
                for obj in env_sub.objects:
                    if obj.type.name == 'TextAsset':
                        d = obj.read()
                        if d.m_Name != asset_name:
                            continue
                        lua_code = d.m_Script
                        if not isinstance(lua_code, str) or len(lua_code) < 10:
                            continue

                        items_for_file = file_items[asset_name]
                        items_dict = [{
                            '_start': it.meta['_start'],
                            '_end': it.meta['_end'],
                            'original': it.meta.get('_raw', it.original),
                            'translated': it.translated,
                            'type': it.meta.get('type', 'text'),
                        } for it in items_for_file]

                        new_code = apply_translations(lua_code, items_dict)

                        # Direct byte replacement in serialized data
                        if not apply_translations_direct(decompressed, lua_code, new_code, unityfs_info):
                            warning(f"  {asset_name}: 无法在序列化数据中找到 Lua 代码，跳过")
                            break

                        # Rebuild UnityFS sub-bundle (try LZ4 in-place patching first for delta=0)
                        new_sub_bytes = rebuild_unityfs(unityfs_info, arc_offset=sub_start,
                                                        patch_original=lua_code, patch_translated=new_code)
                        modifications.append((sub_start, sub_end, new_sub_bytes))
                        break
            except Exception as e:
                warning(f"回填 {asset_name} 失败: {e}")

        # Phase 3: Apply modifications, handling size changes
        modifications.sort(key=lambda m: m[0])
        offset_shift = 0

        # Build list of (old_offset, old_size, new_size) for trailing index update
        trailing_mods = []

        for old_start, old_end, new_bytes in modifications:
            adj_start = old_start + offset_shift
            adj_end = old_end + offset_shift
            old_len = adj_end - adj_start
            new_len = len(new_bytes)
            delta = new_len - old_len

            data[adj_start:adj_end] = new_bytes
            trailing_mods.append((old_start, old_len, new_len))
            offset_shift += delta

        # Update trailing index with batch shifts
        if trailing_mods and offset_shift != 0:
            # Find trailing data (after last UnityFS block)
            positions_all = self._find_unityfs_positions(data)
            if positions_all:
                last_pos = positions_all[-1]
                # Parse UnityFS header to get total_size for last sub-bundle
                reader = __import__('UnityPy').streams.EndianBinaryReader(
                    io.BytesIO(bytes(data[last_pos:last_pos + 128])))
                reader.read_string_to_null()  # sig
                reader.read_u_int()  # version
                reader.read_string_to_null()  # player
                reader.read_string_to_null()  # engine
                last_total = reader.read_long()
                trailing_start = last_pos + last_total
                if trailing_start < len(data):
                    trailing = bytes(data[trailing_start:])
                    updated_trailing = apply_trailing_shifts(trailing, trailing_mods)
                    data[trailing_start:] = updated_trailing

        # Write back — 保留相对路径结构
        rel_path = getattr(self, '_arc_rel_path', None)
        if rel_path:
            output_path = workspace / "original" / rel_path
        else:
            output_path = workspace / "original" / main_arc.name
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(bytes(data))
        info(f"已保存修改后的资源包: {output_path} ({len(modifications)} 个脚本已修改)")

    # ---- Internal helpers ----

    def _setup_arc_path(self, input_path: Path, arc_dir: Path, main_arc: Path):
        """计算并存储游戏根目录和 arc 相对路径，供 repack 使用。"""
        game_dir = input_path if input_path.is_dir() else input_path.parent
        if arc_dir == game_dir:
            self._game_root = game_dir.parent.resolve()
        else:
            self._game_root = game_dir.resolve()
        self._arc_rel_path = main_arc.resolve().relative_to(self._game_root)

    def _find_arc_dir(self, game_dir: Path) -> Path | None:
        """Find the Archives directory in the game folder."""
        # Direct hit: passed the Archives directory itself
        if any(game_dir.glob("*.arc")):
            return game_dir
        arc_dir = game_dir / "Archives"
        if arc_dir.is_dir():
            return arc_dir
        for d in game_dir.iterdir():
            if d.is_dir() and (d / "Archives").is_dir():
                return d / "Archives"
        return None

    def _extract_lua_scripts(self, arc_path: Path) -> list[tuple[str, str]]:
        """Extract all Lua scripts from the archive. Returns [(name, code)]."""
        data = arc_path.read_bytes()
        positions = self._find_unityfs_positions(data)

        # Load main manifest
        unityfs_data = data[0x17:]  # skip @ARCH000 prefix
        try:
            import UnityPy
        except ImportError:
            warning("UnityPy 未安装")
            return []

        env = UnityPy.load(io.BytesIO(unityfs_data))
        manifest = None
        for obj in env.objects:
            if obj.type.name == 'AssetBundleManifest':
                manifest = obj.read()
                break

        if not manifest:
            warning("找不到 AssetBundleManifest")
            return []

        names = manifest.AssetBundleNames

        # Build lookup: manifest position -> (index, name)
        manifest_entries = list(names)

        scripts = []
        for entry_pos, (idx, entry_name) in enumerate(manifest_entries):
            if not entry_name.lower().startswith('src/'):
                continue

            sb_pos = entry_pos + 2  # sub-bundle position offset
            if sb_pos >= len(positions):
                continue

            # Extract sub-bundle
            sub_start = positions[sb_pos]
            sub_end = positions[sb_pos + 1] if sb_pos + 1 < len(positions) else len(data)
            sub_data = data[sub_start:sub_end]

            try:
                env_sub = UnityPy.load(io.BytesIO(sub_data))
                for obj in env_sub.objects:
                    if obj.type.name == 'TextAsset':
                        d = obj.read()
                        script = d.m_Script if hasattr(d, 'm_Script') else None
                        asset_name = d.m_Name if hasattr(d, 'm_Name') else entry_name

                        if script and isinstance(script, str) and len(script) > 100:
                            scripts.append((asset_name, script))
                        elif script and isinstance(script, bytes) and len(script) > 100:
                            scripts.append((asset_name, script.decode('utf-8', errors='replace')))
            except Exception as e:
                debug(f"跳过子包 {sb_pos}: {e}")

        return scripts

    @staticmethod
    def _find_unityfs_positions(data: bytes) -> list[int]:
        """Find all UnityFS signature positions in the data."""
        positions = []
        start = 0
        while True:
            pos = data.find(b'UnityFS', start)
            if pos < 0:
                break
            positions.append(pos)
            start = pos + 1
        return positions


registry.register(UnityArch000LuaEngine())
