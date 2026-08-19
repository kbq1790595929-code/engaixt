"""SQLite 持久化翻译缓存 —— 增量更新与文件 Hash 比对。

每次游戏更新后，资源文件可能变化，但已翻译内容不应丢失。
此模块维护一个 SQLite 数据库，记录每个文件的 Hash 和每条翻译的状态，
使得游戏小版本更新后的重新汉化时间从数分钟压缩到数秒。
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from pathlib import Path

from utils.logger import info, debug, warning
from utils.text_extract import is_acceptable_same_as_source, validation_source_for_item, verify_translation


DB_DIR = Path.home() / ".game_translator" / "cache"
DB_DIR.mkdir(parents=True, exist_ok=True)


def _cache_source_for_item(item) -> str:
    """Namespace engine contracts without exposing the marker to translators."""
    original = str(getattr(item, "original", "") or "")
    meta = getattr(item, "meta", {}) or {}
    scope = str(meta.get("translation_cache_scope") or "").strip()
    if not scope:
        return original
    return f"\x1e{scope}\x1e{original}"


def _get_db_path(game_dir: Path) -> Path:
    """根据游戏目录路径生成独立的数据库文件。"""
    game_hash = hashlib.md5(str(game_dir.resolve()).encode()).hexdigest()[:12]
    return DB_DIR / f"translations_{game_hash}.db"


def get_cache_paths_for_game(game_dir: Path) -> list[Path]:
    """Return the SQLite cache files owned by one game directory."""
    game_dir = Path(game_dir)
    if game_dir.exists() and not game_dir.is_dir():
        game_dir = game_dir.parent
    db_path = _get_db_path(game_dir)
    return [db_path, Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm")]


def delete_cache_for_game(game_dir: Path) -> dict[str, int]:
    """Delete only the persistent translation cache for one game."""
    stats = {"deleted_files": 0, "bytes_freed": 0, "errors": 0}
    for path in get_cache_paths_for_game(game_dir):
        if not path.exists():
            continue
        try:
            size = path.stat().st_size
            path.unlink()
            stats["deleted_files"] += 1
            stats["bytes_freed"] += size
            info(f"Removed game translation cache: {path.name} ({size // 1024} KB)")
        except Exception as e:
            stats["errors"] += 1
            warning(f"Failed to remove translation cache {path.name}: {e}")
    return stats


class TranslationCacheDB:
    """SQLite 翻译持久化缓存。

    数据库结构：
    - translations: 翻译条目（source_hash, source_text, target_text, source_file, time, verified）
    - file_hashes: 资源文件 Hash 记录（file_path, file_hash, last_scan）
    """

    def __init__(self, game_dir: Path):
        self.game_dir = game_dir.resolve()
        self.db_path = _get_db_path(self.game_dir)
        self._conn: sqlite3.Connection | None = None

    # ---- 上下文管理 ----

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *args):
        self.close()

    def open(self):
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._create_tables()

    def close(self):
        if self._conn:
            self._conn.commit()
            self._conn.close()
            self._conn = None

    def _create_tables(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS translations (
                source_hash    TEXT PRIMARY KEY,
                source_text    TEXT NOT NULL,
                target_text    TEXT,
                source_file    TEXT,
                extraction_time INTEGER,
                verified       INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS file_hashes (
                file_path  TEXT PRIMARY KEY,
                file_hash  TEXT NOT NULL,
                last_scan  INTEGER
            );
        """)

    # ---- 文件 Hash 管理 ----

    @staticmethod
    def _sha256(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def _text_hash(text: str) -> str:
        """文本的 SHA256 前 16 字节 hex，用于去重查找。"""
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]

    def get_file_hash(self, file_path: Path) -> str | None:
        """查询数据库中记录的文件 Hash。"""
        if not self._conn:
            return None
        row = self._conn.execute(
            "SELECT file_hash FROM file_hashes WHERE file_path=?",
            (str(file_path),)
        ).fetchone()
        return row[0] if row else None

    def update_file_hash(self, file_path: Path, file_hash: str):
        """更新或插入文件 Hash 记录。"""
        if not self._conn:
            return
        self._conn.execute(
            "INSERT OR REPLACE INTO file_hashes (file_path, file_hash, last_scan) VALUES (?, ?, ?)",
            (str(file_path), file_hash, int(time.time()))
        )

    def is_file_changed(self, file_path: Path) -> bool:
        """判断文件是否与上次扫描时不同。"""
        if not file_path.exists():
            return True  # 新文件
        current_hash = self._sha256(file_path.read_bytes())
        stored_hash = self.get_file_hash(file_path)
        if stored_hash is None:
            return True  # 首次扫描
        changed = current_hash != stored_hash
        if not changed:
            debug(f"  跳过未变化文件: {file_path.name}")
        return changed

    def mark_file_scanned(self, file_path: Path):
        """标记文件已扫描，记录当前 Hash。"""
        if not file_path.exists():
            return
        current_hash = self._sha256(file_path.read_bytes())
        self.update_file_hash(file_path, current_hash)

    # ---- 翻译条目管理 ----

    def get_translation(self, source_text: str) -> str | None:
        """根据原文查找已有译文。"""
        if not self._conn:
            return None
        h = self._text_hash(source_text)
        row = self._conn.execute(
            "SELECT target_text FROM translations WHERE source_hash=? AND target_text IS NOT NULL",
            (h,)
        ).fetchone()
        return row[0] if row else None

    def save_translation(self, source_text: str, target_text: str, source_file: str = ""):
        """保存一条翻译。"""
        if (
            not self._conn
            or not target_text
            or (target_text == source_text and not is_acceptable_same_as_source(source_text, target_text))
        ):
            return
        h = self._text_hash(source_text)
        self._conn.execute(
            """INSERT OR REPLACE INTO translations
               (source_hash, source_text, target_text, source_file, extraction_time)
               VALUES (?, ?, ?, ?, ?)""",
            (h, source_text, target_text, source_file, int(time.time()))
        )

    def save_batch(self, items: list[tuple[str, str, str]]):
        """批量保存翻译 [(source_text, target_text, source_file), ...]"""
        if not self._conn:
            return
        now = int(time.time())
        data = []
        for source_text, target_text, source_file in items:
            if not target_text or (
                target_text == source_text and not is_acceptable_same_as_source(source_text, target_text)
            ):
                continue
            h = self._text_hash(source_text)
            data.append((h, source_text, target_text, source_file, now))
        self._conn.executemany(
            """INSERT OR REPLACE INTO translations
               (source_hash, source_text, target_text, source_file, extraction_time)
               VALUES (?, ?, ?, ?, ?)""",
            data
        )

    def load_all(self) -> dict[str, str]:
        """加载所有已有译文（原文→译文）。"""
        if not self._conn:
            return {}
        rows = self._conn.execute(
            "SELECT source_text, target_text FROM translations WHERE target_text IS NOT NULL"
        ).fetchall()
        return {row[0]: row[1] for row in rows}

    def load_for_files(self, file_paths: list[str]) -> dict[str, str]:
        """加载指定文件相关的已有译文。"""
        if not self._conn:
            return {}
        placeholders = ",".join("?" * len(file_paths))
        rows = self._conn.execute(
            f"SELECT source_text, target_text FROM translations "
            f"WHERE source_file IN ({placeholders}) AND target_text IS NOT NULL",
            file_paths
        ).fetchall()
        return {row[0]: row[1] for row in rows}

    def get_stats(self) -> dict:
        """获取缓存统计。"""
        if not self._conn:
            return {}
        total = self._conn.execute("SELECT COUNT(*) FROM translations").fetchone()[0]
        verified = self._conn.execute(
            "SELECT COUNT(*) FROM translations WHERE verified=1"
        ).fetchone()[0]
        files = self._conn.execute("SELECT COUNT(*) FROM file_hashes").fetchone()[0]
        return {"total": total, "verified": verified, "files_tracked": files}

    def verify_translation(self, source_text: str, verified: bool = True):
        """标记翻译为已校对/未校对。"""
        if not self._conn:
            return
        h = self._text_hash(source_text)
        self._conn.execute(
            "UPDATE translations SET verified=? WHERE source_hash=?",
            (1 if verified else 0, h)
        )

    def cleanup_orphaned(self, active_files: list[str]):
        """清理孤儿条目：标记已删除文件中来源的翻译。"""
        if not self._conn or not active_files:
            return
        # 保留所有翻译，只记录日志
        rows = self._conn.execute(
            f"SELECT COUNT(*) FROM translations WHERE source_file NOT IN "
            f"({','.join('?' * len(active_files))})",
            active_files
        ).fetchone()
        if rows and rows[0] > 0:
            info(f"数据库中有 {rows[0]} 条翻译来自已删除的文件（保留不删除）")


# ---------------------------------------------------------------------------
# 便捷函数
# ---------------------------------------------------------------------------

def load_translations_from_cache(game_dir: Path, items: list) -> int:
    """从 SQLite 缓存加载已有译文到 TextItem 列表。

    返回加载的条数。
    """
    try:
        game_dir = _as_game_dir(game_dir)
        with TranslationCacheDB(game_dir) as db:
            loaded = 0
            for item in items:
                if item.translated and (
                    item.translated != item.original
                    or is_acceptable_same_as_source(item.original, item.translated)
                ):
                    continue  # 已有翻译
                cached = db.get_translation(_cache_source_for_item(item))
                if cached:
                    source_for_validation = validation_source_for_item(item)
                    safe, warns = verify_translation(source_for_validation, cached)
                    if safe and (
                        safe != item.original or is_acceptable_same_as_source(item.original, safe)
                    ):
                        item.translated = safe
                        loaded += 1
                    elif warns:
                        debug(f"  跳过未通过质量校验的 SQLite 缓存: {item.original[:40]}")
            if loaded > 0:
                info(f"从 SQLite 缓存加载: {loaded} 条翻译")
            return loaded
    except Exception as e:
        warning(f"SQLite 缓存读取失败: {e}")
        return 0


def save_translations_to_cache(game_dir: Path, items: list):
    """保存翻译结果到 SQLite 缓存。"""
    try:
        game_dir = _as_game_dir(game_dir)
        with TranslationCacheDB(game_dir) as db:
            batch = []
            for item in items:
                if item.translated and (
                    item.translated != item.original
                    or is_acceptable_same_as_source(item.original, item.translated)
                ):
                    source_for_validation = validation_source_for_item(item)
                    safe, warns = verify_translation(source_for_validation, item.translated)
                    if safe and (
                        safe != item.original or is_acceptable_same_as_source(item.original, safe)
                    ):
                        batch.append((_cache_source_for_item(item), safe, item.file))
                    elif warns:
                        debug(f"  跳过未通过质量校验的 SQLite 缓存写入: {item.original[:40]}")
            if batch:
                db.save_batch(batch)
                info(f"SQLite 缓存已保存: {len(batch)} 条")
    except Exception as e:
        warning(f"SQLite 缓存写入失败: {e}")


def _as_game_dir(path: Path) -> Path:
    path = Path(path)
    return path if path.is_dir() else path.parent
