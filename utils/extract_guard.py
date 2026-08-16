"""提取安全层 — 所有引擎共用的文本提取过滤和诊断。

ExtractionGuard:
    包装 is_translatable()，添加：
    - 提取前结构化过滤（key 黑名单、长度检查等）
    - 统计跟踪（通过/拒绝数量及原因）
    - 诊断报告输出

SharedFilter:
    从 xunity.py 提取的 Unity 多级过滤管线，可供 unity.py 等引擎复用。
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Callable

from utils.logger import info, warning


# ---------------------------------------------------------------------------
# JSON key blocklist — 通用配置文件 key 黑名单
# ---------------------------------------------------------------------------

_KEY_SKIP_PATTERNS: list[re.Pattern] = [
    re.compile(r"^(id|uid|guid|uuid)$", re.IGNORECASE),
    re.compile(r"^(index|idx|order|sort|priority|rank)$", re.IGNORECASE),
    re.compile(r"^(type|kind|mode|format|encoding|version|method)$", re.IGNORECASE),
    re.compile(r"^(path|url|href|src|link|file|filename|folder)$", re.IGNORECASE),
    re.compile(r"^(color|colour|rgb|rgba|hex|hue)$", re.IGNORECASE),
    re.compile(r"^(size|width|height|length|offset|margin|padding|scale)$", re.IGNORECASE),
    re.compile(r"^(script|code|command|cmd|function|func|method|class)$", re.IGNORECASE),
    re.compile(r"^(tag|tags|category|categories|label|labels)$", re.IGNORECASE),
    re.compile(r"^(enabled|disabled|visible|hidden|active|debug)$", re.IGNORECASE),
    re.compile(r"^(.*_)?(type|id|key|code|path|url|class|mode|format|index)(_.*)?$", re.IGNORECASE),
]


def is_config_key(key: str) -> bool:
    """判断 JSON key 是否为配置/技术字段（非文本内容）。"""
    key_clean = key.rsplit(".", 1)[-1]  # 最后一段 key 名
    return any(p.match(key_clean) for p in _KEY_SKIP_PATTERNS)


# ---------------------------------------------------------------------------
# ExtractionGuard
# ---------------------------------------------------------------------------

class ExtractionGuard:
    """提取安全守卫 — 封装过滤 + 统计 + 诊断。"""

    def __init__(self, engine_name: str = "unknown",
                 filter_fn: Callable[[str], bool] | None = None):
        self.engine_name = engine_name
        self._filter_fn = filter_fn  # 注入的过滤函数，默认用 is_translatable
        self.stats: dict[str, int] = defaultdict(int)
        self._sample_rejected: list[tuple[str, str]] = []  # (reason, text)
        self._sample_passed: list[str] = []

    # ---- public API ----

    def is_translatable(self, text: str,
                         context: dict | None = None,
                         key: str = "") -> bool:
        """判断文本是否可翻译。同时记录统计信息。

        Args:
            text: 待判断文本
            context: 附加上下文（如父级 key、JSON 路径等）
            key: 当前 JSON key 名（用于 key 黑名单检查）
        Returns:
            True 如果文本应当被提取翻译
        """
        # 0. Key 黑名单
        if key and is_config_key(key):
            self._record_reject("config_key", text)
            return False

        # 1. 使用过滤函数（默认 is_translatable）
        fn = self._filter_fn or _default_filter
        if fn(text):
            self._record_pass(text)
            return True
        else:
            self._record_reject("filter", text)
            return False

    def emit_report(self) -> dict:
        """输出提取统计报告。返回统计数据 dict。"""
        passed = self.stats["passed"]
        rejected = sum(v for k, v in self.stats.items() if k != "passed")
        total = passed + rejected
        if total == 0:
            info(f"[提取统计] {self.engine_name}: 无文本")
            return {"engine": self.engine_name, "passed": 0, "rejected": 0}

        pct = passed / total * 100 if total > 0 else 0
        info(f"[提取统计] {self.engine_name}: {passed} 通过 / {rejected} 拒绝 "
             f"({pct:.1f}% 通过率)")

        # 拒绝原因分布（Top 5）
        reasons = [(k, v) for k, v in self.stats.items() if k != "passed"]
        reasons.sort(key=lambda kv: kv[1], reverse=True)
        if reasons:
            detail = " | ".join(f"{k}:{v}" for k, v in reasons[:6])
            info(f"[提取详情] {self.engine_name}: {detail}")

        # 丢弃率过高时警告
        if rejected > 0 and passed > 0 and rejected > passed * 3:
            warning(f"[提取警告] {self.engine_name}: 拒绝率过高 ({rejected}/{total})，"
                    f"可能存在大量误提取，请检查样本")

        report: dict = {
            "engine": self.engine_name,
            "passed": passed,
            "rejected": rejected,
            "pass_rate": round(pct, 1),
            "by_reason": {k: v for k, v in self.stats.items() if k != "passed"},
            "sample_passed": self._sample_passed[-10:],
            "sample_rejected": [
                {"reason": r, "text": t[:120]}
                for r, t in self._sample_rejected[-10:]
            ],
        }
        return report

    # ---- internal ----

    def _record_pass(self, text: str):
        self.stats["passed"] += 1
        if len(self._sample_passed) < 20:
            self._sample_passed.append(text[:120])

    def _record_reject(self, reason: str, text: str):
        self.stats[reason] += 1
        if len(self._sample_rejected) < 20:
            self._sample_rejected.append((reason, text[:120]))


def _default_filter(text: str) -> bool:
    """默认过滤函数：使用 is_translatable（延迟导入避免循环）。"""
    from utils.text_extract import is_translatable
    return is_translatable(text)


# ---------------------------------------------------------------------------
# SharedFilter — Unity 多级过滤管线（从 xunity.py 提取）
# ---------------------------------------------------------------------------

class SharedFilter:
    """Unity 多级文本过滤管线。可从 unity.py 和 xunity.py 共用。"""

    _FILTER_PATH_LIKE = re.compile(r"^[A-Za-z0-9_\-/\\\.@:]+$")
    _FILTER_HASH_LIKE = re.compile(r"^[0-9a-f]{8,}$", re.IGNORECASE)
    _FILTER_NUMERIC = re.compile(r"^[\d\s\.\,\-\+\=\(\)\'\"\[\]\{\}]+$")
    _FILTER_FORMAT_ONLY = re.compile(
        r"^(\{[^}]*\}|\%[^%]|\<[^>]+\>|\[[^\]]+\])+$"
    )
    _FILTER_UNITY_ENUM = re.compile(r"^[A-Z][A-Z0-9_]{3,}$")
    _FILTER_UNITY_INTERNAL = re.compile(r"^(m_|unity_|__)[A-Za-z]")
    _FILTER_SHORT_ID = re.compile(r"^[a-z][a-zA-Z0-9]{0,3}$")

    @classmethod
    def filter_items(cls, items: list,
                     filter_fn: Callable[[str], bool] | None = None
                     ) -> tuple[list, dict]:
        """对文本条目列表执行多级过滤。

        Args:
            items: TextItem 列表
            filter_fn: 最终兜底过滤函数（默认 is_translatable）
        Returns:
            (过滤后的 items, 统计 dict)
        """
        if filter_fn is None:
            from utils.text_extract import is_translatable as _fn
            filter_fn = _fn

        filtered: list = []
        stats = {"total": len(items), "length": 0, "path": 0, "hash": 0,
                  "numeric": 0, "format": 0, "enum": 0, "internal": 0,
                  "short": 0, "final_filter": 0}

        for item in items:
            s = item.original.strip()

            # 1: 长度
            if not (2 <= len(s) <= 500):
                stats["length"] += 1
                continue

            # 2: 技术字符串
            if cls._FILTER_PATH_LIKE.match(s):
                stats["path"] += 1
                continue
            if cls._FILTER_HASH_LIKE.match(s) and len(s) >= 16:
                stats["hash"] += 1
                continue
            if cls._FILTER_NUMERIC.match(s):
                stats["numeric"] += 1
                continue

            # 3: 纯格式
            if cls._FILTER_FORMAT_ONLY.match(s):
                stats["format"] += 1
                continue

            # 4: Unity 内部
            if cls._FILTER_UNITY_ENUM.match(s) and len(s) >= 6:
                stats["enum"] += 1
                continue
            if cls._FILTER_UNITY_INTERNAL.match(s):
                stats["internal"] += 1
                continue

            # 5: 短标识符
            if cls._FILTER_SHORT_ID.match(s):
                stats["short"] += 1
                continue

            # 6: 最终兜底
            if filter_fn(item.original):
                filtered.append(item)
            else:
                stats["final_filter"] += 1

        return filtered, stats
