"""checkpoint 阶段：检查点候选判定与快速续翻、JSON 检查点读写/增量同步、
KiriKiri 检查点条目清洗与工作区安全子路径工具。

模块级函数收 pipeline 作第一参数（Pipeline 类保留委托薄壳），
与 core/pipeline_stage_runtime.py 的拆分模式一致。
"""
from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from config import get_config
from utils.logger import info, warning
from utils.text_extract import is_acceptable_same_as_source, validation_source_for_item, verify_translation


def _translation_contract(meta: object) -> str:
    if not isinstance(meta, dict):
        return ""
    return str(meta.get("translation_contract") or "")


def _required_checkpoint_contracts(game_path: Path) -> set[str]:
    game_dir = game_path if game_path.is_dir() else game_path.parent
    if (
        (game_dir / "www" / "data" / "CommonEvents.json").exists()
        or (game_dir / "data" / "CommonEvents.json").exists()
    ):
        from core.rpgmaker_event_extraction import RPGMAKER_MESSAGE_CONTRACT

        return {RPGMAKER_MESSAGE_CONTRACT}
    return set()


def checkpoint_resume_candidate(pipeline, game_path: Path, file_filter: list[str] | None = None) -> Path | None:
    """Return a usable game checkpoint for fast resume without re-extracting.

    The full checkpoint mode normally means extract -> translate -> repack.
    For large resource-layer engines that already completed extraction, doing
    that again is wasteful.  This fast path is deliberately conservative: it
    only accepts a checkpoint that belongs to the current game and has items,
    and it is disabled when a file filter asks for a different extraction
    slice.
    """
    from core import pipeline as _pipeline_mod
    if file_filter:
        return None
    checkpoint = _pipeline_mod._game_meta_path(game_path, "translation_checkpoint.json")
    if not checkpoint.exists():
        return None
    try:
        data = json.loads(checkpoint.read_text(encoding="utf-8-sig"))
    except Exception:
        return None
    items = data.get("items")
    if not isinstance(items, list) or not items:
        return None
    required_contracts = _required_checkpoint_contracts(game_path)
    available_contracts = {
        str(value) for value in data.get("translation_contracts", []) if value
    }
    if required_contracts - available_contracts:
        info("RPGMaker 检查点使用旧逐行合同，将重新提取以避免对话错位")
        return None
    source = data.get("source")
    if source:
        try:
            if Path(str(source)).resolve() != (game_path if game_path.is_dir() else game_path.parent).resolve():
                return None
        except Exception:
            return None
    return checkpoint

def load_items_from_checkpoint_json(pipeline, checkpoint: Path) -> tuple[list, str, str, dict]:
    from engines.base import TextItem

    data = json.loads(checkpoint.read_text(encoding="utf-8-sig"))
    raw_items = data.get("items", [])
    items = [
        TextItem(
            file=str(raw.get("file", "")),
            key=str(raw.get("key", "")),
            original=str(raw.get("original", "")),
            translated=str(raw.get("translated", "") or ""),
            context=str(raw.get("context", "")),
            line=int(raw.get("line", 0) or 0),
            meta=raw.get("meta", {}) if isinstance(raw.get("meta", {}), dict) else {},
        )
        for raw in raw_items
        if raw.get("file") is not None and raw.get("original") is not None
    ]
    return (
        items,
        str(data.get("source_lang") or "ja"),
        str(data.get("target_lang") or get_config().target_lang or "zh-CN"),
        data,
    )

def reuse_checkpoint_workspace_if_available(pipeline, checkpoint: Path) -> bool:
    if not pipeline.workspace:
        return False
    source_workspace = checkpoint.parent
    source_original = source_workspace / "original"
    if not source_original.is_dir():
        return False
    if source_workspace.resolve() == pipeline.workspace.root.resolve():
        return True
    dest_original = pipeline.workspace.root / "original"
    try:
        if dest_original.exists():
            shutil.rmtree(dest_original)
        shutil.copytree(source_original, dest_original)
        if pipeline.diagnostics:
            pipeline.diagnostics.set("checkpoint_workspace_reused", str(source_workspace.resolve()))
        info(f"复用检查点工作区脚本: {source_original}")
        return True
    except Exception as exc:
        warning(f"复用检查点工作区失败，将按需重新准备回填资源: {exc}")
        return False

def workspace_has_repack_sources(pipeline, items: list) -> bool:
    from core import pipeline as _pipeline_mod
    if not pipeline.workspace:
        return False
    original_dir = pipeline.workspace.root / "original"
    if not original_dir.is_dir():
        return False
    needed = {
        _checkpoint_item_rel(getattr(item, "file", "") or "")
        for item in items
        if _pipeline_mod._has_effective_translation(item)
    }
    needed.discard("")
    if not needed:
        return False
    return all((original_dir / rel).exists() for rel in needed)

async def run_checkpoint_resume_fast_path(
    pipeline,
    path: Path,
    engine,
    injector: str | None,
    checkpoint: Path,
    launch: bool,
) -> bool:
    """Continue translation from an already extracted checkpoint."""
    from core import pipeline as _pipeline_mod
    items, source_lang, target_lang, data = pipeline._load_items_from_checkpoint_json(checkpoint)
    if getattr(engine, "name", "") == "kirikiri":
        original_raw_items = data.get("items", [])
        raw_items = _clean_kirikiri_checkpoint_items(original_raw_items)
        raw_items = pipeline._merge_kirikiri_runtime_capture_raw_items(path, raw_items)
        if raw_items != original_raw_items:
            data["items"] = raw_items
            data["total"] = len(raw_items)
            data["translated_count"] = sum(1 for raw in raw_items if raw.get("translated"))
            checkpoint.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        items, source_lang, target_lang, data = pipeline._load_items_from_checkpoint_json(checkpoint)

    if not items:
        if pipeline.diagnostics:
            pipeline.diagnostics.warn("检查点中没有可续翻文本", checkpoint=str(checkpoint))
            pipeline.diagnostics.finish(False)
        return False

    items = pipeline._apply_configured_translation_scope(
        engine,
        items,
        int(getattr(get_config(), "translation_coverage", 100)),
        note="检查点续翻",
    )
    if not items:
        if pipeline.diagnostics:
            pipeline.diagnostics.warn("当前补翻策略没有选中任何文本", checkpoint=str(checkpoint))
            pipeline.diagnostics.finish(False)
        warning("当前补翻策略没有选中任何文本")
        return False

    pipeline._checkpoint_path = checkpoint
    pipeline._reuse_checkpoint_workspace_if_available(checkpoint)
    pipeline.diagnostics.set("checkpoint", str(checkpoint))
    pipeline.diagnostics.set("checkpoint_fast_resume", True)
    info(f"从检查点续翻，跳过重新提取: {checkpoint}")

    pipeline._update_progress("translate")
    pipeline._reset_api_cache_stats(get_config().active_translator)
    items = await pipeline._translate_with_checkpoint(items, checkpoint, source_lang, target_lang, path)
    pipeline._record_api_cache_stats(path, len(items))

    pipeline._update_progress("validate")
    items = pipeline._validate_all(items)
    pipeline._sync_checkpoint_json(checkpoint, items)
    items = await pipeline._retry_invalid_translations(items, checkpoint, source_lang, target_lang, path)
    pipeline._record_api_cache_stats(path, len(items))

    translated_count = sum(1 for i in items if _pipeline_mod._has_effective_translation(i))
    info(f"翻译完成: {translated_count}/{len(items)} 条")
    pipeline.diagnostics.set("translation_stats", {
        "translated": translated_count,
        "total": len(items),
        "blocked": pipeline._blocked_count,
    })
    if not pipeline._has_required_translation_coverage(items):
        pipeline._fail_insufficient_translation_coverage(items)
        return False

    return await pipeline._do_patch_only(path, checkpoint, launch, injector)

def load_checkpoint_into(pipeline, items: list, checkpoint: Path):
    """从已有检查点 JSON 加载翻译到 items 中（断点续传）。"""
    import json as _json
    if not checkpoint.exists():
        return
    data = _json.loads(checkpoint.read_text(encoding="utf-8"))
    lookup = {}
    invalid = 0
    for raw in data.get("items", []):
        if raw.get("translated"):
            source_for_validation = validation_source_for_item(type("_CheckpointItem", (), {
                "original": raw["original"],
                "meta": raw.get("meta", {}),
            })())
            safe, warns = verify_translation(source_for_validation, raw["translated"])
            if safe and (safe != raw["original"] or is_acceptable_same_as_source(raw["original"], safe)):
                lookup[(
                    raw["file"], raw.get("key", ""), raw["original"],
                    _translation_contract(raw.get("meta")),
                )] = safe
            elif warns:
                invalid += 1
    if not lookup:
        if invalid > 0:
            warning(f"检查点中 {invalid} 条旧译文未通过质量校验，已放回待翻译队列")
        return
    loaded = 0
    for item in items:
        t = lookup.get((
            item.file, item.key, item.original,
            _translation_contract(getattr(item, "meta", {})),
        ))
        if t:
            item.translated = t
            loaded += 1
    if loaded > 0:
        info(f"从检查点恢复: {loaded} 条翻译")
    if invalid > 0:
        warning(f"检查点中 {invalid} 条旧译文未通过质量校验，已放回待翻译队列")

def save_checkpoint_json(pipeline, items: list, game_path: Path,
                         source_lang: str, target_lang: str, json_path: Path):
    """将提取的文本保存为 JSON 检查点。"""
    from core import pipeline as _pipeline_mod
    import json as _json

    pck_name = ""
    game_dir = game_path if game_path.is_dir() else game_path.parent
    for sub in ["contents", "game", "data", ""]:
        search = game_dir / sub if sub else game_dir
        if not search.is_dir():
            continue
        pcks = list(search.glob("*.pck"))
        if pcks:
            pck_name = pcks[0].name
            break

    data = {
        "source": str(game_dir.resolve()),
        "pck_name": pck_name,
        "source_lang": source_lang,
        "target_lang": target_lang,
        "total": len(items),
        "translated_count": sum(1 for it in items if _pipeline_mod._has_effective_translation(it)),
        "translation_contracts": sorted({
            contract
            for contract in (
                _translation_contract(getattr(it, "meta", {})) for it in items
            )
            if contract
        }),
        "items": [
            {
                "file": it.file,
                "key": it.key,
                "original": it.original,
                "context": it.context,
                "line": it.line,
                "meta": it.meta,
                "translated": it.translated if _pipeline_mod._has_effective_translation(it) else "",
            }
            for it in items
        ],
    }

    payload = _json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    json_path.write_text(payload, encoding="utf-8")
    game_meta_checkpoint = _pipeline_mod._game_meta_path(game_path, "translation_checkpoint.json")
    if game_meta_checkpoint != json_path:
        game_meta_checkpoint.parent.mkdir(parents=True, exist_ok=True)
        game_meta_checkpoint.write_text(payload, encoding="utf-8")

def sync_checkpoint_json(pipeline, checkpoint: Path, items: list) -> None:
    """将 items 中的翻译增量同步到 JSON 检查点文件（全量改写但紧凑格式）。"""
    from core import pipeline as _pipeline_mod
    import json as _json

    if not checkpoint.exists():
        return
    try:
        data = _json.loads(checkpoint.read_text(encoding="utf-8"))
    except Exception:
        warning("检查点 JSON 读取失败，跳过同步")
        return

    lookup: dict[tuple, list[dict]] = {}
    for raw in data.get("items", []):
        key = (
            raw["file"], raw.get("key", ""), raw["original"],
            _translation_contract(raw.get("meta")),
        )
        lookup.setdefault(key, []).append(raw)

    effective_by_key: dict[tuple, str] = {}
    for it in items:
        key = (
            it.file, it.key, it.original,
            _translation_contract(getattr(it, "meta", {})),
        )
        effective_by_key.setdefault(key, "")
        if not effective_by_key[key] and _pipeline_mod._has_effective_translation(it):
            effective_by_key[key] = it.translated

    updated = 0
    for key, raws in lookup.items():
        effective = effective_by_key.get(key, "")
        for raw in raws:
            if effective:
                if raw.get("translated") != effective:
                    raw["translated"] = effective
                    updated += 1
            elif raw.get("translated"):
                raw["translated"] = ""
                updated += 1

    if updated > 0:
        data["translated_count"] = sum(1 for r in data["items"] if r.get("translated"))
        try:
            payload = _json.dumps(data, ensure_ascii=False, separators=(",", ":"))
            checkpoint.write_text(payload, encoding="utf-8")
            source = data.get("source")
            if source:
                game_checkpoint = _pipeline_mod._game_meta_path(Path(str(source)), "translation_checkpoint.json")
                if game_checkpoint != checkpoint:
                    game_checkpoint.parent.mkdir(parents=True, exist_ok=True)
                    game_checkpoint.write_text(payload, encoding="utf-8")
        except Exception:
            warning("检查点 JSON 写入失败")
            return
        info(f"检查点已同步: {data['translated_count']} 条")

def _checkpoint_item_rel(path: str) -> str:
    return str(path or "").replace("\\", "/").lstrip("/")


def _safe_workspace_child(root: Path, rel: str) -> Path | None:
    try:
        root_resolved = root.resolve()
        candidate = (root / _checkpoint_item_rel(rel)).resolve()
        candidate.relative_to(root_resolved)
        return candidate
    except Exception:
        return None


def _clean_kirikiri_checkpoint_items(raw_items: list[dict]) -> list[dict]:
    cleaned: list[dict] = []
    for raw in raw_items:
        item = dict(raw)
        original = str(item.get("original") or "").strip()
        translated = str(item.get("translated") or "")
        if _looks_like_kirikiri_control_identifier(original):
            item["translated"] = ""
        elif translated and re.fullmatch(r"[?？\s]+", translated):
            item["translated"] = ""
        cleaned.append(item)
    return cleaned


def _looks_like_kirikiri_control_identifier(text: str) -> bool:
    if not text:
        return False
    if re.fullmatch(r"\*[A-Za-z0-9_./\\:-]+", text):
        return True
    return bool(re.fullmatch(
        r"[A-Za-z0-9_./\\:-]+\.(?:ks|tjs|scn|xp3|png|jpg|jpeg|webp|ogg|wav|mp3|m4a|mp4|avi)",
        text,
        re.I,
    ))
