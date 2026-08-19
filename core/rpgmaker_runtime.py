"""RPG Maker MV/MZ Runtime Translation Pipeline.

Architecture:
  1. SCAN (优先静态): 静态解析 data/*.json / www/data/*.json
     ↓ 降级: 启动游戏 + rpgmaker_hook.js 内存扫描 → HTTP 收集
  2. FILTER: Python 去重（按 safe_text）
  3. TRANSLATE: DeepSeek 批量翻译（控制符保护为 __CTRLn__）
  4. CACHE: 翻译映射表存入服务器内存 + 本地 JSON（双通道）
  5. REPLACE: hook 替换模式 → Game_Message.add 拦截 → HTTP 查表替换
     ↓ 降级: HTTP 不可用时读取本地 JSON 文件

游戏数据文件永不被修改。所有翻译在显示层完成。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

from core.resources import resource_path
from core.rpgmaker_event_extraction import (
    RPGMAKER_MESSAGE_CONTRACT,
    _make_ctx,
    _records_with_adjacency,
    _scan_event_list,
    _scan_event_records,
)
from core.rpgmaker_legacy_translation import translate_scanned_items
from core.rpgmaker_runtime_map import (
    build_runtime_translation_map as _build_runtime_translation_map,
    sanitize_translation_map as _sanitize_runtime_translation_map,
)
from engines.base import TextItem
from utils.logger import info, warning, debug
from utils.text_extract import contains_kana, is_punctuation_only


# ---- Constants ----
_HOOK_SCRIPT_NAME = "rpgmaker_hook.js"
_HOOK_DATA_NAME = "hook_text.json"
_HOOK_SRC = resource_path("engines", "assets", _HOOK_SCRIPT_NAME)
_SCAN_TIMEOUT = 30  # seconds to wait for hook scan completion
_DISPLAY_COVERAGE_FILE = "rpgmaker_display_coverage.json"
_TRANSLATION_MAP_FILE = "hook_translation_map.json"  # 本地 JSON 备用通道


# ---- Control code protection (matches rpgmaker_hook.js protectControls) ----

_CTRL_RE = re.compile(
    r'\\[A-Za-z]{1,3}\s*\[([^\]]*)\]'  # \C[2], \N[1], \V[25] 等
    r'|\\[\.\|!>\^<>{}]'                # \. \| \! \> \^ \<
    r'|\\FS\[\d+\]'                      # \FS[数字]
    r'|\\fr\b|\\fb\b|\\fi\b|\\g\b',     # \fr \fb \fi \g
    re.IGNORECASE
)

_CTRL_TYPE_MAP = {
    'C': 'COLOR', 'N': 'NAME', 'V': 'VAR', 'I': 'ICON',
    'SE': 'SE', 'SP': 'SPRITE', 'SA': 'SA', 'FS': 'FONT',
    '.': 'DOT', '|': 'WAIT', '!': 'SHAKE', '>': 'INSTANT', '^': 'PAUSE',
    'fr': 'FRESET', 'fb': 'FBOLD', 'fi': 'FITAL', 'g': 'CURRENCY',
}

# 纯数字/标点/HTML标签 — 不值得翻译
_SKIP_PATTERNS = [
    re.compile(r'^[\d０-９\s]+$'),
    re.compile(r'^[!-/:-@[-`{-~\s]+$'),
    re.compile(r'^<[a-z]+>$', re.I),
    re.compile(r'^\\[a-z]+$', re.I),
]
_LITERAL_SKIP_TOKENS = {
    "hp", "mp", "tp", "exp", "lv", "atk", "def", "mat", "mdf", "agi", "luk",
    "png", "jpg", "jpeg", "webp", "gif", "ogg", "m4a", "wav", "blob",
    "true", "false", "null", "undefined", "on", "off",
}

# 假名 Unicode 范围 — 用于拦截纯假名碎片（粒子/词片，翻译必乱码）
_KANA_PATTERN = re.compile(r'^[぀-ゟ゠-ヿ\s、。！？…「」『』（）…―・]+$')


def _is_kana_only(text: str) -> bool:
    """文本是否全是假名（无汉字、无拉丁字母、无数字）。"""
    return bool(_KANA_PATTERN.match(text))


def _protect_controls(text: str) -> tuple[str, list[dict]]:
    """替换控制符为 __TOKEN__ 占位符。返回 (safe_text, token_types)。"""
    tokens = []
    types = []

    def replacer(match):
        idx = len(tokens)
        full = match.group(0)
        tokens.append(full)
        code_match = re.match(r'\\([A-Za-z]+)', full)
        code = code_match.group(1).upper() if code_match else ''
        inner_match = re.search(r'\[([^\]]*)\]', full)
        param = re.sub(r'[^a-zA-Z0-9]', '_', inner_match.group(1)) if inner_match else ''
        base_type = _CTRL_TYPE_MAP.get(code, code)
        token = f'__{base_type}_{param}__' if param else f'__{base_type}__'
        types.append({'type': base_type, 'param': param, 'token': token, 'original': full})
        return token

    safe = _CTRL_RE.sub(replacer, text)
    return safe, types


def _should_skip(text: str) -> bool:
    """纯数字/标点/HTML标签/单控制符/系统 token → 不值得翻译。"""
    stripped = (text or "").strip()
    if len(stripped) <= 1:
        return True
    if is_punctuation_only(stripped):
        return True
    if stripped.lower() in _LITERAL_SKIP_TOKENS:
        return True
    if re.fullmatch(r"[A-Z][A-Z0-9_]{0,5}", stripped):
        return True
    for pat in _SKIP_PATTERNS:
        if pat.match(text):
            return True
    return False


# ---- Helpers ----

def _game_dir(path: Path) -> Path:
    return path if path.is_dir() else path.parent


def _find_js_dir(game_dir: Path) -> Path | None:
    """Find the js/ directory containing main.js.

    RPG Maker MV: js/ at game root
    RPG Maker MZ: www/js/ (NW.js deployment with package.json)
    """
    candidates = [
        game_dir / "js",
        game_dir / "www" / "js",
    ]
    for d in candidates:
        if (d / "main.js").exists():
            return d
    return None


def _find_data_dir(game_dir: Path) -> Path | None:
    """Find the data/ directory containing RPG Maker JSON files.

    RPG Maker MV: data/*.json at game root
    RPG Maker MZ: www/data/*.json (NW.js deployment)
    """
    candidates = [
        game_dir / "www" / "data",
        game_dir / "data",
    ]
    for d in candidates:
        if d.is_dir() and list(d.glob("*.json")):
            return d
    return None


def _inject_hook(game_dir: Path, mode: str, manifest: object | None = None) -> Path:
    """Copy hook script to game js/ dir. Inject into main.js scriptUrls. Return hook script path."""
    js_dir = _find_js_dir(game_dir)
    if js_dir is None:
        js_dir = game_dir / "js"
    hook_dst = js_dir / _HOOK_SCRIPT_NAME
    hook_dst.parent.mkdir(parents=True, exist_ok=True)

    _backup_rpgmaker_runtime_targets(game_dir, js_dir, manifest)

    # Read hook template and set mode
    hook_src = _HOOK_SRC.read_text(encoding="utf-8")
    hook_src = hook_src.replace('var MODE = "scan"', f'var MODE = "{mode}"')
    hook_src = hook_src.replace('var MODE = "replace"', f'var MODE = "{mode}"')
    hook_dst.write_text(hook_src, encoding="utf-8")
    _record_manifest_created(manifest, hook_dst, kind="runtime", runtime_required=True)

    # 启用 NW.js node 权限：RPG Maker MZ 默认禁用 require('fs')，hook 需要用到
    for pkg_path in [game_dir / "package.json", game_dir / "www" / "package.json"]:
        if pkg_path.exists():
            try:
                pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
                if not pkg.get("nodejs"):
                    pkg["nodejs"] = True
                    pkg_path.write_text(json.dumps(pkg, indent=4, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass

    # 注入策略：
    # 1. 优先注入 index.html（RPG Maker MZ 标准部署，<script> 标签方式加载）
    # 2. 回退注入 main.js（RPG Maker MV 或自定义部署）
    index_html = js_dir.parent / "index.html"
    if index_html.exists():
        injected = _inject_into_html(index_html)
    else:
        injected = _inject_into_main_js(js_dir / "main.js")

    if not injected:
        warning(f"无法注入 hook 脚本（{game_dir.name}），hook 不会被加载")

    return hook_dst


def _backup_rpgmaker_runtime_targets(game_dir: Path, js_dir: Path, manifest: object | None) -> None:
    """Back up files that the RPGMaker runtime hook can mutate."""
    if manifest is None:
        return
    candidates = [
        js_dir.parent / "index.html",
        js_dir / "main.js",
        game_dir / "package.json",
        game_dir / "www" / "package.json",
    ]
    seen: set[Path] = set()
    for path in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            resolved = path
        if resolved in seen or not path.exists() or not path.is_file():
            continue
        seen.add(resolved)
        try:
            manifest.backup_file(path)
        except Exception as exc:
            debug(f"RPGMaker manifest backup skipped for {path}: {exc}")


def _record_manifest_created(
    manifest: object | None,
    path: Path,
    *,
    kind: str = "runtime",
    runtime_required: bool = True,
) -> None:
    if manifest is None:
        return
    try:
        manifest.record_created(path, kind=kind, runtime_required=runtime_required)
    except Exception as exc:
        debug(f"RPGMaker manifest create record skipped for {path}: {exc}")


def _inject_into_html(index_html: Path) -> bool:
    """在 index.html 中注入 hook <script> 标签（main.js 之前）。"""
    if not index_html.exists():
        return False
    content = index_html.read_text(encoding="utf-8")
    if _HOOK_SCRIPT_NAME in content:
        return True  # 已注入

    # 在 main.js 之前插入 hook 脚本
    marker = '<script type="text/javascript" src="js/main.js">'
    if marker in content:
        hook_tag = f'<script type="text/javascript" src="js/{_HOOK_SCRIPT_NAME}"></script>\n        {marker}'
        content = content.replace(marker, hook_tag)
        index_html.write_text(content, encoding="utf-8")
        return True
    # 兜底：在 </body> 之前插入
    if '</body>' in content:
        hook_tag = f'    <script type="text/javascript" src="js/{_HOOK_SCRIPT_NAME}"></script>\n</body>'
        content = content.replace('</body>', hook_tag)
        index_html.write_text(content, encoding="utf-8")
        return True
    return False


def _inject_into_main_js(main_js: Path) -> bool:
    """在 main.js 中注入 hook 脚本引用（MV 风格 scriptUrls）。"""
    if not main_js.exists():
        return False
    content = main_js.read_text(encoding="utf-8")
    if _HOOK_SCRIPT_NAME in content:
        return True

    if '"js/plugins.js"' in content:
        content = content.replace(
            '"js/plugins.js"',
            f'"js/{_HOOK_SCRIPT_NAME}",\n    "js/plugins.js"'
        )
        main_js.write_text(content, encoding="utf-8")
        return True

    return False


def _launch_game(
    game_dir: Path,
    exe_path: Path | None = None,
    manifest: object | None = None,
) -> subprocess.Popen | None:
    """Launch the game and return the process handle.

    NW.js 用户数据隔离：避免 MTool 等工具的新版 NW.js 与游戏自带的旧版 NW.js
    产生 profile 版本冲突（"您的个人资料来自更高版本的 NW.js"）。
    """
    if exe_path is None:
        exes = [e for e in game_dir.glob("*.exe") if "CrashHandler" not in e.name]
        if not exes:
            return None
        exe_path = exes[0]

    # 独立 NW.js 用户数据目录，绕过 MTool 的 profile 污染
    isolated_data = game_dir / ".nwjs_profile"
    profile_existed = isolated_data.exists()
    isolated_data.mkdir(parents=True, exist_ok=True)
    if not profile_existed:
        _record_manifest_created(
            manifest,
            isolated_data,
            kind="runtime_profile",
            runtime_required=False,
        )

    try:
        proc = subprocess.Popen(
            f'"{exe_path}" --user-data-dir="{isolated_data}"',
            cwd=str(game_dir),
            shell=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return proc
    except Exception:
        return None


def _kill_game(proc: subprocess.Popen | None = None):
    """Kill the game process."""
    if proc:
        try:
            proc.kill()
        except Exception:
            pass
    os.system("taskkill /f /im Game.exe >nul 2>&1")


# ---- Static scan (JSON files, no game launch required) ----

def _load_data_json(path: Path):
    """容错读取 RPGMaker 数据 JSON。

    兼容 UTF-8 BOM；加密/混淆/损坏的数据文件（常见于带保护插件的游戏）
    返回 None 并告警跳过，不允许单个坏文件炸掉整条静态扫描。
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as e:
        warning(f"[静态扫描] 读取失败，跳过 {path.name}: {e}")
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        if text.lstrip()[:1] not in ("[", "{"):
            warning(f"[静态扫描] {path.name} 内容非 JSON（疑似加密/混淆保护），跳过该文件")
        else:
            warning(f"[静态扫描] {path.name} JSON 解析失败，跳过该文件")
        return None


def _scan_static(game_dir: Path) -> list[TextItem]:
    """静态扫描：直接从 www/data/*.json 提取所有文本，无需启动游戏。

    解析逻辑与 rpgmaker_hook.js doScan() 一致，覆盖：
    - CommonEvents.json
    - Map*.json（所有地图的 events → pages → list）
    - MapInfos.json（地图名称）
    - Actors/Items/Weapons/Armors/Skills/States/Enemies/Troops/Classes.json
    - System.json
    """
    data_dir = _find_data_dir(game_dir)
    if not data_dir:
        warning("找不到 RPG Maker 数据目录（www/data 或 data）")
        return []

    debug(f"[静态扫描] 数据目录: {data_dir}")
    raw_items = []  # [{text, context}]

    def _add(text, context, prev_text="", next_text="", meta=None):
        if not text or not isinstance(text, str):
            return
        s = text.strip()
        if len(s) < 1 or len(s) > 500:
            return
        if re.match(r'^\\[A-Za-z]+\s*\[[^\]]*\]$', s):
            return
        raw_items.append({
            'text': s, 'context': context,
            'prev_text': prev_text.strip() if prev_text else "",
            'next_text': next_text.strip() if next_text else "",
            'meta': dict(meta or {}),
        })

    # 1. CommonEvents
    ce_path = data_dir / "CommonEvents.json"
    if ce_path.exists():
        data = _load_data_json(ce_path) or []
        for ev in (data or []):
            if not ev or not ev.get('list'):
                continue
            ctx = _make_ctx('CmEv', ev.get('id'), ev.get('name', ''), 'dialogue')
            extracted = _scan_event_records(ev['list'], ctx)
            for item in _records_with_adjacency(extracted):
                _add(
                    item['text'], item['context'], item['prev_text'], item['next_text'],
                    item.get('meta'),
                )
        debug(f"[静态扫描] CommonEvents: {len(data)} 事件")

    # 2. MapInfos（地图名称）
    mi_path = data_dir / "MapInfos.json"
    if mi_path.exists():
        data = _load_data_json(mi_path) or []
        items = data if isinstance(data, list) else list(data.values())
        for m in (items or []):
            if isinstance(m, dict) and m.get('name'):
                _add(m['name'], _make_ctx('MapInfo', m.get('id'), m.get('name', ''), 'menu'))

    # 3. All Map*.json（每个地图的事件文本）
    map_count = 0
    for fp in sorted(data_dir.glob("Map*.json")):
        if fp.name == "MapInfos.json":
            continue
        try:
            map_data = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not map_data or not map_data.get('events'):
            continue
        map_id = map_data.get('mapId', fp.stem)
        for ev in (map_data['events'] or []):
            if not ev or not ev.get('pages'):
                continue
            for p_idx, page in enumerate(ev['pages']):
                if not page or not page.get('list'):
                    continue
                ctx = _make_ctx('Map', f'{map_id}.Ev{ev.get("id", "")}.P{p_idx}', ev.get('name', ''), 'dialogue')
                extracted = _scan_event_records(page['list'], ctx)
                for item in _records_with_adjacency(extracted):
                    _add(
                        item['text'], item['context'], item['prev_text'], item['next_text'],
                        item.get('meta'),
                    )
        map_count += 1
    debug(f"[静态扫描] {map_count} 个地图")

    # 4. Database: Actors, Items, Weapons, Armors, Skills, States, Enemies, Troops, Classes
    db_defs = {
        'Actors.json': ('Actor', 'status'),
        'Items.json': ('Item', 'menu'),
        'Weapons.json': ('Weapon', 'menu'),
        'Armors.json': ('Armor', 'menu'),
        'Skills.json': ('Skill', 'battle'),
        'States.json': ('State', 'battle'),
        'Enemies.json': ('Enemy', 'battle'),
        'Troops.json': ('Troop', 'battle'),
        'Classes.json': ('Class', 'status'),
    }
    db_fields = ['name', 'description', 'message', 'displayName', 'title', 'profile']

    for fname, (cat, role) in db_defs.items():
        fp = data_dir / fname
        if not fp.exists():
            continue
        arr = _load_data_json(fp)
        if not isinstance(arr, list):
            continue
        for entry in (arr or []):
            if not entry:
                continue
            ctx = _make_ctx(cat, entry.get('id'), entry.get('name', ''), role)
            for field in db_fields:
                val = entry.get(field)
                if val:
                    _add(str(val), f'{ctx}.{field}')
        debug(f"[静态扫描] {fname}: {len(arr)} 条")

    # 5. System
    sys_path = data_dir / "System.json"
    if sys_path.exists():
        sys_data = _load_data_json(sys_path) or {}
        _add(sys_data.get('gameTitle', ''), _make_ctx('Sys', '', '', 'menu'))
        _add(sys_data.get('currencyUnit', ''), _make_ctx('Sys', '', '', 'menu'))
        terms = sys_data.get('terms', {})
        if terms:
            for group_name, group in terms.items():
                if isinstance(group, list):
                    for i, t in enumerate(group):
                        if t:
                            _add(str(t), _make_ctx('Sys', group_name, str(i), 'menu'))
                elif isinstance(group, dict):
                    for k, v in group.items():
                        if v:
                            _add(str(v), _make_ctx('Sys', group_name, k, 'menu'))
        for key in ['elements', 'skillTypes', 'weaponTypes', 'armorTypes']:
            arr = sys_data.get(key, [])
            if arr:
                for i, item in enumerate(arr):
                    if item:
                        _add(str(item), _make_ctx('Sys', key.rstrip('s'), str(i), 'menu'))
        debug(f"[静态扫描] System: 已载入")

    # 去重（按 safe_text），保留 group_key 用于批量翻译
    seen: dict[str, dict] = {}
    for item in raw_items:
        safe, ctrl_types = _protect_controls(item['text'])
        item_meta = dict(item.get('meta') or {})
        if item_meta.get('rpgmaker_segments'):
            item_meta['rpgmaker_segments'] = [
                _protect_controls(str(segment))[0]
                for segment in item_meta['rpgmaker_segments']
            ]
        # 构建分组键：事件级上下文（去掉 .line/.choice/.scroll 后缀）
        ctx = item.get('context', '')
        group_key = re.sub(r'\.(line|choice|scroll)$', '', ctx) if '.' in ctx else ctx

        if safe in seen:
            seen[safe]['count'] += 1
        else:
            seen[safe] = {
                'safe': safe,
                'text': item['text'],
                'context': item['context'],
                'count': 1,
                'types': ctrl_types,
                'prev_text': item.get('prev_text', ''),
                'next_text': item.get('next_text', ''),
                'group_key': group_key,
                'meta': item_meta,
            }

    # 过滤纯标点/数字（不翻译）
    filtered = {k: v for k, v in seen.items() if not _should_skip(k)}

    info(f"[静态扫描] {len(raw_items)} 条文本 → 去重 {len(seen)} → 过滤后 {len(filtered)} 条")

    return [
        TextItem(
            file="hook",
            key=it["safe"],
            original=it["safe"],
            context=it.get("context", ""),
            meta={
                "text": it.get("text", ""),
                "count": it.get("count", 0),
                "prev_text": it.get("prev_text", ""),
                "next_text": it.get("next_text", ""),
                "group_key": it.get("group_key", ""),
                **dict(it.get("meta") or {}),
            },
        )
        for it in filtered.values()
    ]


# ---- Hook-based scan (launch game + HTTP collect) ----

def _scan_via_hook(
    game_dir: Path,
    timeout: int = _SCAN_TIMEOUT,
    on_progress: object = None,
    manifest: object | None = None,
) -> list[TextItem]:
    """启动游戏 + hook 扫描，通过 HTTP 收集文本。

    返回 TextItem 列表；如果游戏无法启动或扫描超时，返回空列表。
    """
    def _prog(msg, pct):
        if on_progress and callable(on_progress):
            try: on_progress(msg, pct)
            except Exception: pass

    _prog("启动游戏", 21)
    _kill_game()
    time.sleep(1)

    _inject_hook(game_dir, "scan", manifest=manifest)

    # 启动翻译服务器（hook 通过 HTTP POST 发送扫描数据）
    from utils.translation_server import start_server, _hook_scan_data
    _hook_scan_data.clear()
    server = start_server(port=5120, provider="deepseek")
    if server:
        info("翻译服务器已启动，等待 hook 扫描数据...")
    else:
        warning("翻译服务器启动失败，无法接收 hook 数据")

    _prog("等待扫描", 22)

    # Launch game
    proc = _launch_game(game_dir, manifest=manifest)
    if not proc:
        warning("无法启动游戏，回退到静态扫描")
        if server:
            server.stop()
        return []

    _prog("扫描中", 23)

    # 快速探测：如果 15 秒内无数据到达，多半是 headless 环境或游戏无法渲染
    # 直接降级到静态扫描，不浪费 30 秒等待
    _QUICK_PROBE = 15
    info("游戏已启动，等待扫描完成...")
    deadline = time.time() + timeout
    quick_deadline = time.time() + _QUICK_PROBE
    while time.time() < deadline:
        if _hook_scan_data:
            break
        if time.time() > quick_deadline and not _hook_scan_data:
            # 快速探测超时 — 游戏可能无法渲染，杀掉进程降级
            debug(f"Hook 扫描 {_QUICK_PROBE}s 内无数据 → 改从文件静态提取（仍走 hook 显示层替换）")
            _kill_game(proc)
            if server:
                server.stop()
            return []
        time.sleep(2)

    _kill_game(proc)
    if server:
        server.stop()

    if not _hook_scan_data:
        warning(f"Hook 扫描超时 ({timeout}s)")
        return []

    info(f"Hook 收集到 {len(_hook_scan_data)} 条文本")

    # 过滤 + 去重
    seen: dict[str, dict] = {}
    for it in _hook_scan_data:
        safe = it.get("safe", "")
        if not safe or _should_skip(safe):
            continue
        if safe in seen:
            seen[safe]["count"] += it.get("count", 1)
        else:
            seen[safe] = it

    return [
        TextItem(
            file="hook",
            key=it["safe"],
            original=it["safe"],
            context=it.get("context", ""),
            meta={
                "text": it.get("text", ""),
                "count": it.get("count", 0),
                **({
                    "rpgmaker_segments": list(it.get("segments") or []),
                    "rpgmaker_speaker": str(it.get("speaker") or ""),
                    "translation_cache_scope": RPGMAKER_MESSAGE_CONTRACT,
                    "translation_contract": RPGMAKER_MESSAGE_CONTRACT,
                } if it.get("segments") else {}),
                **({
                    "rpgmaker_speaker_name": True,
                    "translation_cache_scope": "rpgmaker_speaker_v1",
                    "translation_contract": "rpgmaker_speaker_v1",
                } if it.get("speaker_name") else {}),
            },
        )
        for it in seen.values()
    ]


# ---- Deploy ----

def _deploy_replace(
    game_dir: Path,
    trans_map: dict[str, str],
    manifest: object | None = None,
) -> dict[str, str]:
    """部署翻译映射表：服务器内存 + 本地 JSON 双通道。

    Hook 优先通过 HTTP GET /_hook_map 获取翻译表；
    HTTP 不可用时降级读取本地 save/hook_translation_map.json。
    """
    trans_map = _sanitize_translation_map(trans_map)

    # 通道 1：服务器内存（/_hook_map 端点）
    try:
        from utils.translation_server import _hook_translation_map
        _hook_translation_map.clear()
        _hook_translation_map.update(trans_map)
    except ImportError:
        pass

    # 通道 2：本地 JSON 文件（hook 的 require('fs') 备用通道）
    save_dir = game_dir / "save"
    save_dir.mkdir(parents=True, exist_ok=True)
    map_file = save_dir / _TRANSLATION_MAP_FILE
    map_file.write_text(
        json.dumps(trans_map, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _record_manifest_created(manifest, map_file, kind="runtime_map", runtime_required=True)

    # Always deploy the display hook in replace mode. Static extraction does
    # not need the scan hook, so the replace hook may not exist yet.
    _inject_hook(game_dir, "replace", manifest=manifest)

    info(f"替换模式已部署: {len(trans_map)} 条翻译（HTTP + 本地 JSON 双通道）")
    return trans_map


def _sanitize_translation_map(trans_map: dict[str, str]) -> dict[str, str]:
    return _sanitize_runtime_translation_map(trans_map, should_skip=_should_skip)


def _contains_japanese_kana(text: str) -> bool:
    return contains_kana(str(text or ""))


def _contains_cjk(text: str) -> bool:
    return any("\u3400" <= char <= "\u9fff" for char in str(text or ""))


def _build_display_coverage_report(
    items: list[TextItem],
    generated_map: dict[str, str],
    deployed_map: dict[str, str],
) -> dict:
    """Compare extracted display sources with the map actually loaded by the hook."""
    source_contexts: dict[str, str] = {}
    for item in items:
        context = str(getattr(item, "context", "") or "")
        meta = getattr(item, "meta", {}) or {}
        segments = meta.get("rpgmaker_segments")
        sources = segments if isinstance(segments, list) and segments else [getattr(item, "original", "")]
        for value in sources:
            source = str(value or "").strip()
            if not source or _should_skip(source):
                continue
            source_contexts.setdefault(source, context)

    mapped = [source for source in source_contexts if source in deployed_map]
    unmapped = [source for source in source_contexts if source not in deployed_map]
    unmapped_with_kana = [source for source in unmapped if _contains_japanese_kana(source)]
    unmapped_cjk_without_kana = [
        source for source in unmapped
        if not _contains_japanese_kana(source) and _contains_cjk(source)
    ]
    total = len(source_contexts)

    def samples(sources: list[str]) -> list[dict[str, str]]:
        return [
            {"source": source, "context": source_contexts[source]}
            for source in sources[:20]
        ]

    return {
        "schema_version": 1,
        "extracted_entries": len(items),
        "extracted_unique_sources": total,
        "generated_map_entries": len(generated_map),
        "deployed_map_entries": len(deployed_map),
        "mapped_sources": len(mapped),
        "unmapped_sources": len(unmapped),
        "display_map_coverage_percent": round(len(mapped) * 100.0 / max(total, 1), 4),
        "unmapped_with_kana_count": len(unmapped_with_kana),
        "unmapped_with_kana_samples": samples(unmapped_with_kana),
        "unmapped_cjk_without_kana_count": len(unmapped_cjk_without_kana),
        "unmapped_cjk_without_kana_samples": samples(unmapped_cjk_without_kana),
    }


def _write_display_coverage_report(
    game_dir: Path,
    items: list[TextItem],
    generated_map: dict[str, str],
    deployed_map: dict[str, str],
    manifest: object | None = None,
) -> dict:
    report = _build_display_coverage_report(items, generated_map, deployed_map)
    report_path = game_dir / "_translation_meta" / _DISPLAY_COVERAGE_FILE
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _record_manifest_created(manifest, report_path, kind="diagnostic", runtime_required=False)
    info(
        "RPG Maker 显示映射覆盖: "
        f"{report['mapped_sources']}/{report['extracted_unique_sources']} "
        f"({report['display_map_coverage_percent']:.2f}%)，"
        f"仍含假名 {report['unmapped_with_kana_count']} 条"
    )
    if report["unmapped_with_kana_count"]:
        warning(
            "RPG Maker 仍有可见日文未进入显示映射，"
            f"详见 {report_path}"
        )
    return report


# ---- Public API ----

def scan(
    game_path: Path,
    timeout: int = _SCAN_TIMEOUT,
    on_progress: object = None,
    manifest: object | None = None,
) -> list[TextItem]:
    """收集游戏文本。MV/MZ 默认先静态扫描，失败再降级 hook 扫描。"""
    game_dir = _game_dir(game_path)

    # RPG Maker MV/MZ dialogue normally lives in data/*.json. Prefer static
    # extraction so a failed run does not leave a scan-mode hook in the game.
    info("尝试从 data/*.json 静态提取 RPG Maker 文本...")
    items = _scan_static(game_dir)
    if items:
        return items

    # Fallback for unusual deployments where database JSON is unavailable.
    info("静态提取没有文本，尝试 hook 扫描...")
    items = _scan_via_hook(game_dir, timeout, on_progress, manifest=manifest)
    if items:
        return items

    warning("所有扫描方式均失败，未获取到文本")
    return []


def translate_and_deploy(game_path: Path, items: list[TextItem],
                         source_lang: str = "ja", target_lang: str = "zh-CN",
                         workspace: str = "",
                         manifest: object | None = None) -> int:
    """Deploy translations to replace mode. Uses pipeline-translated items,
    falls back to checkpoint JSON in workspace if items have no translations."""
    game_dir = _game_dir(game_path)

    # Build the per-display-line map from grouped translation items.
    trans_map, map_stats = _build_runtime_translation_map(items)
    if map_stats["grouped_messages"]:
        info(
            "RPGMaker 消息映射: "
            f"{map_stats['grouped_messages']} 个对话框 -> "
            f"{map_stats['expanded_lines']} 条显示行"
        )

    # Fallback: checkpoint JSON in workspace
    if not trans_map and workspace:
        ck = Path(workspace) / "translation_checkpoint.json"
        if ck.exists():
            data = json.loads(ck.read_text(encoding="utf-8"))
            checkpoint_items = [
                TextItem(
                    file=str(raw.get("file", "")),
                    key=str(raw.get("key", "")),
                    original=str(raw.get("original", "")),
                    translated=str(raw.get("translated", "") or ""),
                    context=str(raw.get("context", "")),
                    line=int(raw.get("line", 0) or 0),
                    meta=raw.get("meta", {}) if isinstance(raw.get("meta"), dict) else {},
                )
                for raw in data.get("items", [])
            ]
            trans_map, map_stats = _build_runtime_translation_map(checkpoint_items)
            if trans_map:
                info(f"从检查点加载了 {len(trans_map)} 条翻译")

    if not trans_map:
        info("没有已翻译条目，跳过部署")
        _write_display_coverage_report(game_dir, items, {}, {}, manifest=manifest)
        return 0

    trans_map = _final_unify(trans_map)
    deployed_map = _deploy_replace(game_dir, trans_map, manifest=manifest)
    _write_display_coverage_report(
        game_dir,
        items,
        trans_map,
        deployed_map,
        manifest=manifest,
    )
    return len(deployed_map)


def _final_unify(trans_map: dict[str, str]) -> dict[str, str]:
    """在最终翻译映射表上做全局术语统一。

    对每个术语组，在所有译文中统计各候选词的总出现次数，
    取最高频者，强制替换所有其他候选词（不限原文范围）。
    """
    from collections import Counter

    # 术语组：每组内视为同义变体
    TERM_GROUPS = [
        {'哥哥', '大哥', '老哥', '兄长'},
        {'鸡鸡', '鸡巴', '小弟弟', '小弟', '那儿', '那东西', '那玩意儿', '那根'},
        {'阴茎', '肉棒', '老二'},
    ]

    fixed = 0

    for group in TERM_GROUPS:
        counts = Counter()
        for tgt in trans_map.values():
            for cn in group:
                counts[cn] += tgt.count(cn)
        if not counts:
            continue

        best_word = max(group, key=lambda c: counts[c])
        best_total = counts[best_word]
        total_all = sum(counts.values())
        if best_total == 0:
            continue

        # 占比 >= 70% → 噪声主导，全局统一
        # 占比 < 70% → 语境自然差异，保留不做统一
        if best_total < total_all * 0.7:
            debug(f"[统一] '{best_word}' 仅 {best_total}/{total_all}（{best_total*100//max(total_all,1)}%），存在语境差异，保留变体")
            continue

        replaced = 0
        other_words = [c for c in group if c != best_word]
        for src in list(trans_map.keys()):
            tgt = trans_map[src]
            modified = False
            for cn in other_words:
                if cn in tgt:
                    tgt = tgt.replace(cn, best_word)
                    modified = True
            if modified:
                trans_map[src] = tgt
                replaced += 1

        if replaced:
            info(f"[统一] '{best_word}' 占 {best_total}/{total_all}（{best_total*100//max(total_all,1)}%），修正 {replaced} 条")
            fixed += 1

    if fixed:
        info(f"[统一] 全局完成，{fixed} 组术语已统一")

    return trans_map


def launch_replace(game_path: Path, manifest: object | None = None) -> subprocess.Popen | None:
    """Launch game in replace mode (translation must already be deployed)."""
    game_dir = _game_dir(game_path)
    _kill_game()
    time.sleep(1)
    _inject_hook(game_dir, "replace", manifest=manifest)
    return _launch_game(game_dir, manifest=manifest)


# ---- Legacy helpers (used by hook_context.py) ----

def _wait_for_scan(hook_path: Path, timeout: int = _SCAN_TIMEOUT) -> bool:
    """Poll for hook_text.json to appear after scan completes."""
    for _ in range(timeout):
        if hook_path.exists() and hook_path.stat().st_size > 100:
            return True
        time.sleep(1)
    return False


def _load_collected(hook_path: Path) -> list[dict]:
    """Load hook_text.json."""
    data = json.loads(hook_path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and not isinstance(data, list):
        return [{"safe": k, "text": k, "context": "unknown", "count": v} for k, v in data.items()]
    if isinstance(data, list) and len(data[0]) >= 2:
        if len(data[0]) >= 4:
            return [{"safe": it[0], "text": it[1], "context": it[2], "count": it[3]} for it in data]
        return [{"safe": it[0], "text": it[1], "context": "unknown", "count": 1} for it in data]
    return []
