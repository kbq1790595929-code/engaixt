"""XP3 提取诊断：保护层识别、索引异常与内容签名分类、建议动作。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from engines.kirikiri.codec import (
    _decode_script_bytes_for_quality,
    _is_kirikiri_scrambled_text,
    _looks_like_kirikiri_script_text,
    _try_decode_xp3_single_byte_xor_filter,
)
from engines.kirikiri.xp3 import (
    _Xp3Entry,
    _Xp3Index,
    _read_xp3_entry_bytes,
    _xp3_entry_is_lightweight_obfuscated_script_candidate,
    _xp3_entry_is_script,
)


class _ProtectionLayer(str, Enum):
    NONE = "no_protection"
    INDEX_OBFUSCATED = "index_obfuscated"
    CONTENT_FILTERED = "content_filtered"
    BOTH = "index_and_content"


class _IndexAnomaly(str, Enum):
    NORMAL = "normal"
    UNKNOWN_CHUNK = "unknown_chunk"
    NAME_MANGLED = "name_mangled"
    ENTRY_COUNT_MISMATCH = "count_mismatch"


class _ContentSignature(str, Enum):
    PLAINTEXT_UTF8 = "plaintext_utf8"
    PLAINTEXT_CP932 = "plaintext_cp932"
    PLAINTEXT_UTF16 = "plaintext_utf16"
    KNOWN_SCRAMBLE = "known_krkr_scramble"
    SINGLE_BYTE_XOR = "single_byte_xor"
    HIGH_ENTROPY = "high_entropy_unknown"
    TJS_BYTECODE = "tjs2_compiled"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class _ExtractionDiagnosis:
    archive_file: str
    index_anomaly: _IndexAnomaly
    index_anomalies: tuple[_IndexAnomaly, ...]
    index_subtypes: tuple[str, ...]
    content_signature: _ContentSignature
    content_signatures: tuple[_ContentSignature, ...]
    protection_layer: _ProtectionLayer
    entropy_sample: float
    sample_hex_head: str
    sample_entry: str
    unknown_chunks: tuple[str, ...]
    mangled_name_ratio: float
    entry_count: int
    script_entry_count: int
    recommended_action: str


def _diagnose_xp3_archive(path: Path, index: _Xp3Index) -> _ExtractionDiagnosis:
    index_anomalies = _detect_xp3_index_anomalies(index)
    index_anomaly = index_anomalies[0] if index_anomalies else _IndexAnomaly.NORMAL
    index_subtypes = _detect_xp3_index_subtypes(index)
    mangled_ratio = _mangled_name_ratio(index.entries)
    sample_entries = _select_xp3_diagnostic_sample_entries(path, index)
    signatures: list[_ContentSignature] = []
    entropy_values: list[float] = []
    sample_hex = ""
    sample_entry = ""
    sample_rank = -1.0
    script_entry_count = sum(1 for entry in index.entries if _xp3_entry_is_script(entry))

    for entry in sample_entries[:12]:
        try:
            data = _read_xp3_entry_bytes(path, entry)
        except Exception:
            continue
        sample = data[:8192]
        signature = _classify_xp3_payload(sample)
        signatures.append(signature)
        entropy = _shannon_entropy(sample)
        entropy_values.append(entropy)
        rank = _content_signature_suspicion_score(signature) + entropy / 10.0
        if not sample_hex or rank > sample_rank:
            sample_hex = sample[:16].hex()
            sample_entry = entry.name
            sample_rank = rank

    content_signature = _dominant_content_signature(signatures)
    entropy_sample = max(entropy_values) if entropy_values else 0.0
    if content_signature == _ContentSignature.UNKNOWN and entropy_sample >= 7.2:
        content_signature = _ContentSignature.HIGH_ENTROPY
    protection_layer = _combine_protection_layer(index_anomaly, content_signature)
    return _ExtractionDiagnosis(
        archive_file=path.name,
        index_anomaly=index_anomaly,
        index_anomalies=tuple(index_anomalies or [_IndexAnomaly.NORMAL]),
        index_subtypes=tuple(index_subtypes),
        content_signature=content_signature,
        content_signatures=tuple(signatures or [_ContentSignature.UNKNOWN]),
        protection_layer=protection_layer,
        entropy_sample=round(entropy_sample, 3),
        sample_hex_head=sample_hex,
        sample_entry=sample_entry,
        unknown_chunks=tuple(index.unknown_chunks),
        mangled_name_ratio=round(mangled_ratio, 3),
        entry_count=len(index.entries),
        script_entry_count=script_entry_count,
        recommended_action=_recommended_xp3_action(protection_layer, index_anomaly, content_signature),
    )


def _detect_xp3_index_anomalies(index: _Xp3Index) -> list[_IndexAnomaly]:
    anomalies: list[_IndexAnomaly] = []
    if index.unknown_chunks:
        anomalies.append(_IndexAnomaly.UNKNOWN_CHUNK)
    if _mangled_name_ratio(index.entries) > 0.50:
        anomalies.append(_IndexAnomaly.NAME_MANGLED)
    if not index.entries:
        anomalies.append(_IndexAnomaly.ENTRY_COUNT_MISMATCH)
    return anomalies


def _detect_xp3_index_subtypes(index: _Xp3Index) -> list[str]:
    subtypes: list[str] = []
    names = [entry.name for entry in index.entries[:300]]
    if names and sum(1 for name in names if _looks_like_resource_id_name(name)) / len(names) > 0.50:
        subtypes.append("possible_id_as_name")
    if index.unknown_chunks:
        subtypes.append("custom_index_chunk")
    return subtypes


def _mangled_name_ratio(entries: tuple[_Xp3Entry, ...]) -> float:
    if not entries:
        return 0.0
    suspect = sum(1 for entry in entries if _xp3_name_looks_mangled(entry.name))
    return suspect / len(entries)


def _xp3_name_looks_mangled(name: str) -> bool:
    if not name:
        return True
    basename = Path(name).name
    suffix = Path(name).suffix.lower()
    if suffix in _COMMON_KIRIKIRI_EXTENSIONS:
        return False
    if suffix:
        return False
    if _looks_like_resource_id_name(basename):
        return True
    if len(basename) <= 3 and any("\u3400" <= ch <= "\u9fff" for ch in basename):
        return True
    printable = sum(1 for ch in basename if ch.isprintable())
    return printable / max(1, len(basename)) < 0.80


def _looks_like_resource_id_name(name: str) -> bool:
    if not name or "/" in name or "\\" in name or "." in name:
        return False
    if len(name) > 4:
        return False
    return all("\u4e00" <= ch <= "\u9fff" or "\ue000" <= ch <= "\uf8ff" for ch in name)


def _select_xp3_diagnostic_sample_entries(path: Path, index: _Xp3Index) -> list[_Xp3Entry]:
    direct = [
        entry for entry in index.entries
        if _xp3_entry_is_script(entry)
        and not Path(entry.name).name.lower().startswith("startup")
        and 0 < entry.original_size <= 8 * 1024 * 1024
    ]
    if direct:
        return direct
    extensionless = [
        entry for entry in index.entries
        if _xp3_entry_is_lightweight_obfuscated_script_candidate(path, entry)
        and not Path(entry.name).name.lower().startswith("startup")
    ]
    if extensionless:
        return extensionless
    fallback = [
        entry for entry in index.entries
        if not Path(entry.name).name.lower().startswith("startup")
        and 0 < entry.original_size <= 8 * 1024 * 1024
    ]
    return fallback[:12]


def _classify_xp3_payload(data: bytes) -> _ContentSignature:
    if not data:
        return _ContentSignature.UNKNOWN
    if _is_kirikiri_scrambled_text(data):
        return _ContentSignature.KNOWN_SCRAMBLE
    if data.startswith((b"TJS2", b"\x00T\x00J\x00S\x002")):
        return _ContentSignature.TJS_BYTECODE
    if _try_decode_xp3_single_byte_xor_filter(data) is not None:
        return _ContentSignature.SINGLE_BYTE_XOR

    decoded = _decode_script_bytes_for_quality(data)
    if decoded is not None and _looks_like_kirikiri_script_text(decoded.text):
        if decoded.encoding in {"utf-8", "utf-8-sig"}:
            return _ContentSignature.PLAINTEXT_UTF8
        if decoded.encoding in {"cp932", "shift_jis"}:
            return _ContentSignature.PLAINTEXT_CP932
        if decoded.encoding.startswith("utf-16"):
            return _ContentSignature.PLAINTEXT_UTF16

    if _shannon_entropy(data) >= 7.2:
        return _ContentSignature.HIGH_ENTROPY
    return _ContentSignature.UNKNOWN


def _dominant_content_signature(signatures: list[_ContentSignature]) -> _ContentSignature:
    if not signatures:
        return _ContentSignature.UNKNOWN
    priority = [
        _ContentSignature.HIGH_ENTROPY,
        _ContentSignature.UNKNOWN,
        _ContentSignature.SINGLE_BYTE_XOR,
        _ContentSignature.KNOWN_SCRAMBLE,
        _ContentSignature.TJS_BYTECODE,
        _ContentSignature.PLAINTEXT_UTF8,
        _ContentSignature.PLAINTEXT_CP932,
        _ContentSignature.PLAINTEXT_UTF16,
    ]
    counts = {signature: signatures.count(signature) for signature in set(signatures)}
    return sorted(counts, key=lambda item: (-counts[item], priority.index(item) if item in priority else 99))[0]


def _content_signature_suspicion_score(signature: _ContentSignature) -> float:
    scores = {
        _ContentSignature.HIGH_ENTROPY: 4.0,
        _ContentSignature.UNKNOWN: 3.0,
        _ContentSignature.SINGLE_BYTE_XOR: 2.0,
        _ContentSignature.KNOWN_SCRAMBLE: 1.5,
        _ContentSignature.TJS_BYTECODE: 1.0,
        _ContentSignature.PLAINTEXT_UTF8: 0.0,
        _ContentSignature.PLAINTEXT_CP932: 0.0,
        _ContentSignature.PLAINTEXT_UTF16: 0.0,
    }
    return scores.get(signature, 0.0)


def _combine_protection_layer(index_anomaly: _IndexAnomaly, content_signature: _ContentSignature) -> _ProtectionLayer:
    index_bad = index_anomaly != _IndexAnomaly.NORMAL
    content_bad = content_signature in {_ContentSignature.HIGH_ENTROPY, _ContentSignature.UNKNOWN}
    if index_bad and content_bad:
        return _ProtectionLayer.BOTH
    if index_bad:
        return _ProtectionLayer.INDEX_OBFUSCATED
    if content_bad:
        return _ProtectionLayer.CONTENT_FILTERED
    return _ProtectionLayer.NONE


def _recommended_xp3_action(
    layer: _ProtectionLayer,
    index_anomaly: _IndexAnomaly,
    content_signature: _ContentSignature,
) -> str:
    if layer == _ProtectionLayer.CONTENT_FILTERED and content_signature == _ContentSignature.HIGH_ENTROPY:
        return "静态定位 content filter: 反汇编 exe/DLL，搜索 TVPCreateStream/TVPRegisterStorageMedia 调用链"
    if layer in {_ProtectionLayer.INDEX_OBFUSCATED, _ProtectionLayer.BOTH} and index_anomaly == _IndexAnomaly.NAME_MANGLED:
        return "优先确认是否为资源 ID 误解码，检查主程序或插件内的 name lookup table"
    if layer in {_ProtectionLayer.INDEX_OBFUSCATED, _ProtectionLayer.BOTH} and index_anomaly == _IndexAnomaly.UNKNOWN_CHUNK:
        return "定位自定义 XP3 chunk parser，搜索 unknown chunk tag 对应处理函数"
    if content_signature == _ContentSignature.TJS_BYTECODE:
        return "已识别 TJS2 字节码，走字节码/字符串池提取与回填路线"
    if content_signature == _ContentSignature.KNOWN_SCRAMBLE:
        return "已识别 KiriKiri scramble，走内置 descramble 静态提取"
    if content_signature == _ContentSignature.SINGLE_BYTE_XOR:
        return "已识别单字节 XOR filter，走内置静态 XOR 解码"
    if layer == _ProtectionLayer.NONE:
        return "索引与内容未见保护，按普通 KiriKiri 静态提取"
    return "需要静态分析 XP3 保护层后再提取"


def _diagnosis_to_dict(diagnosis: _ExtractionDiagnosis) -> dict[str, object]:
    return {
        "archive_file": diagnosis.archive_file,
        "index_anomaly": diagnosis.index_anomaly.value,
        "index_anomalies": [item.value for item in diagnosis.index_anomalies],
        "index_subtypes": list(diagnosis.index_subtypes),
        "content_signature": diagnosis.content_signature.value,
        "content_signatures": [item.value for item in diagnosis.content_signatures],
        "protection_layer": diagnosis.protection_layer.value,
        "entropy_sample": diagnosis.entropy_sample,
        "sample_hex_head": diagnosis.sample_hex_head,
        "sample_entry": diagnosis.sample_entry,
        "unknown_chunks": list(diagnosis.unknown_chunks),
        "mangled_name_ratio": diagnosis.mangled_name_ratio,
        "entry_count": diagnosis.entry_count,
        "script_entry_count": diagnosis.script_entry_count,
        "recommended_action": diagnosis.recommended_action,
    }


def _primary_xp3_diagnosis(diagnoses: list[_ExtractionDiagnosis]) -> _ExtractionDiagnosis | None:
    if not diagnoses:
        return None
    severity = {
        _ProtectionLayer.BOTH: 3,
        _ProtectionLayer.INDEX_OBFUSCATED: 2,
        _ProtectionLayer.CONTENT_FILTERED: 2,
        _ProtectionLayer.NONE: 0,
    }
    return sorted(
        diagnoses,
        key=lambda item: (
            -severity.get(item.protection_layer, 0),
            -item.script_entry_count,
            -item.entry_count,
            item.archive_file.lower(),
        ),
    )[0]


def _shannon_entropy(data: bytes) -> float:
    if not data:
        return 0.0
    counts = [0] * 256
    for byte in data:
        counts[byte] += 1
    total = len(data)
    return -sum((count / total) * math.log2(count / total) for count in counts if count)


_COMMON_KIRIKIRI_EXTENSIONS = {
    ".ks", ".tjs", ".tjsc", ".scn", ".asd", ".png", ".jpg", ".jpeg", ".bmp", ".tlg",
    ".ogg", ".wav", ".mp3", ".m4a", ".opus", ".avi", ".mp4", ".mpg", ".mpeg", ".wmv",
    ".ttf", ".otf", ".txt", ".csv", ".json", ".xml", ".ini", ".dic", ".dat", ".dll",
}
