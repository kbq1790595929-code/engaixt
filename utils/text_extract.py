import re
import unicodedata
from collections import Counter

SQUARE_PLACEHOLDER = (
    r"\[(?:"
    r"/?[A-Za-z][A-Za-z0-9_.:-]{0,40}(?:=[^\]\s]{1,80})?|"
    r"/?[A-Za-z][A-Za-z0-9_.:-]{0,40}(?:\s+[A-Za-z_:.-]+=[^\]\s]{1,80})+|"
    r"[0-9]+|"
    r"#[0-9a-fA-F]{3,8}"
    r")\]"
)
KIRIKIRI_PERCENT_CONTROL = r"%(?:p-?\d*|f[^;\r\n]{0,80}|n|-?\d+);"
KIRIKIRI_EMPHASIS_MARK = r"\[・\]"
EMPTY_BRACE_CONTROL = r"\{\}"
ANGLE_CONTROL_TAG = r"<[^<>\r\n]{1,256}>"
BACKSLASH_CONTROL = (
    r"\\(?:"
    r"[A-Za-z]+(?:\[(?:[^\[\]\r\n]|\[[^\[\]\r\n]*\])*\])?"
    r"|[-+*/.<>|!^](?:\[[^\[\]\r\n]*\])?"
    r")"
)

_AT_TAG_RE = re.compile(r"@[a-zA-Z_][a-zA-Z0-9_]*\b")
_SOFT_RPGMAKER_DISPLAY_CODES = {r"\.", r"\|", r"\!", r"\>", r"\^", r"\n"}

# 覆盖所有常见游戏占位符和控制符
PLACEHOLDER_PATTERN = re.compile(
    r"(" + KIRIKIRI_PERCENT_CONTROL + r"|" + KIRIKIRI_EMPHASIS_MARK + r"|%[dsrfx]|%[0-9.]*[dsrfx]|\\[nrt]|\\x[0-9a-fA-F]{2}|"
    r"\{[a-zA-Z_][a-zA-Z0-9_]*\}|\{/[a-zA-Z_]+\}|\{[0-9]+\}|" + EMPTY_BRACE_CONTROL + r"|"
    + ANGLE_CONTROL_TAG + r"|" + SQUARE_PLACEHOLDER + r"|"
    r"\\[NnVvCcIi]\s*\[\d+\]|"
    r"\\[\.\|!>\^]|"
    r"\\[a-zA-Z]+\b|"
    r"@[a-zA-Z_][a-zA-Z0-9_]*\b|#[a-zA-Z_][a-zA-Z0-9_]*\b)"
)

# 危险模式：仅在占位符保护失效时才可能触发，出现则值得关注
DANGEROUS_PATTERNS = [
    (re.compile(r"[％ｓ]"), "中文全角 %s（应为半角 %%s）"),
    (re.compile(r"[％ｄ]"), "中文全角 %d（应为半角 %%d）"),
    (re.compile(r"％\("), "中文全角 %( 格式符"),
    (re.compile(r"(\d+\.\s+翻译)"), "列表格式泄露"),
    (re.compile(r"[浣鎴戜綘鍚楀ソ鏄鐨涓璇浜妗姝绔鈥銆鍛]{3,}|€"), "疑似 UTF-8/GBK 乱码"),
]

# 自动修复模式：可以安全地自动修复的问题（替换函数或替换字符串）
_AUTO_FIX_PREFIXES = [
    (re.compile(r"^翻译结果[：:]\s*"), ""),
    (re.compile(r"^翻译[：:]\s*"), ""),
    (re.compile(r"^译文[：:]\s*"), ""),
    (re.compile(r"^输出[：:]\s*"), ""),
]


def extract_placeholders(text: str) -> list[str]:
    return PLACEHOLDER_PATTERN.findall(text)


def validate_translation(original: str, translated: str) -> bool:
    """检查译文是否保留了所有关键占位符。"""
    if not translated:
        return False
    orig_ph = Counter(extract_placeholders(original))
    trans_ph = Counter(extract_placeholders(translated))
    return not (orig_ph - trans_ph)


def contains_kana(text: str) -> bool:
    """文本是否含日文假名（判断“是否还是日文”的核心信号）。"""
    return bool(_KANA_RE.search(text or ""))


def is_acceptable_same_as_source(original: str, translated: str) -> bool:
    """Allow short CJK strings that are valid Chinese even when unchanged.

    汉字可混合数字与常见符号（如 RPG 数据库条目“城镇2”、“攻击+3”）：
    这类文本模型保持原样是正确行为，判失败只会触发无意义的单条重译。
    仍要求至少一个汉字、无假名、可见长度 ≤8，避免放行真正的漏翻。
    """
    orig = (original or "").strip()
    trans = (translated or "").strip()
    if not orig or orig != trans:
        return False
    visible = PLACEHOLDER_PATTERN.sub("", orig)
    visible = re.sub(r"[\s\u3000。！？?!、，,.…:：;；「」『』（）()《》\"'“”‘’]", "", visible)
    if not visible or len(visible) > 8:
        return False
    if _KANA_RE.search(visible):
        return False
    if not _CJK_RE.search(visible):
        return False
    return bool(re.fullmatch(
        r"[㐀-䶿一-鿿豈-﫿0-9０-９"
        r"+\-×÷%％#＃&＆*＊/／·]+",
        visible,
    ))


def _restore_missing_leading_at_tags(original: str, translated: str, missing: set[str]) -> tuple[str, set[str]]:
    """Re-add leading RPG Maker face/speaker tags when the model dropped them."""
    leading = re.match(r"^\s*((?:@[a-zA-Z_][a-zA-Z0-9_]*\b\s*)+)", original)
    if not leading:
        return translated, missing

    restored: list[str] = []
    for tag in _AT_TAG_RE.findall(leading.group(1)):
        if tag in missing and tag not in translated:
            restored.append(tag)

    if not restored:
        return translated, missing

    prefix = " ".join(restored)
    if translated.strip():
        translated = f"{prefix} {translated.lstrip()}"
    else:
        translated = prefix
    return translated, missing - set(restored)


def _is_soft_missing_placeholder(token: str) -> bool:
    return (
        token in _SOFT_RPGMAKER_DISPLAY_CODES
        or token == "[・]"
        or bool(_AT_TAG_RE.fullmatch(token))
    )


# 占位符特征字符 — 原文不含这些字符则绝对没有占位符，可跳过正则扫描
_PLACEHOLDER_TRIGGER = re.compile(r'[{\[<%\@#\\\\]')
# Exclude punctuation that lives inside the Katakana Unicode block, notably
# U+30FB KATAKANA MIDDLE DOT (・). RPG Maker commonly uses it as a list bullet.
_KANA_CHARS = r"\u3041-\u3096\u309d-\u309f\u30a1-\u30fa\u30fc-\u30ff\uff66-\uff9f"
_KANA_RE = re.compile(f"[{_KANA_CHARS}]")
_KANA_SEQ_RE = re.compile(f"[{_KANA_CHARS}]+")
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_BGI_RUBY_RE = re.compile(r"<r[^>]*>(.*?)</r>", re.IGNORECASE | re.DOTALL)
_LANGUAGE_TERM_RE = re.compile(
    r"(日语|日文|日本語|日本|假名|平假名|片假名|五十音|读作|念作|发音|口癖|口吻|语气|意思|含义|词|字|音|行|料理|基本|口诀)"
)
_SHORT_QUOTE_PAIRS = (
    ("『", "』"),
    ("“", "”"),
    ("‘", "’"),
    ("\"", "\""),
    ("'", "'"),
    ("「", "」"),
)
_OUTER_QUOTE_PAIRS = (
    ("「", "」"),
    ("『", "』"),
    ("“", "”"),
    ("‘", "’"),
    ("\"", "\""),
    ("'", "'"),
)
_PUNCT_SYMBOL_EXTRA = set("、。，．.!！?？…・：:；;「」『』（）()［］[]【】《》〈〉～〜ー—-…♡♪☆·")


def is_punctuation_only(text: str) -> bool:
    """Return True for display punctuation/symbol fragments that should not be translated."""
    stripped = re.sub(r"\s+", "", PLACEHOLDER_PATTERN.sub("", text or ""))
    if not stripped:
        return False
    for ch in stripped:
        cat0 = unicodedata.category(ch)[0]
        if cat0 not in {"P", "S"} and ch not in _PUNCT_SYMBOL_EXTRA:
            return False
    return True


def _visible_len(text: str) -> int:
    visible = PLACEHOLDER_PATTERN.sub("", text or "")
    visible = re.sub(r"\s+", "", visible)
    return len(visible)


def strip_bgi_ruby(text: str) -> str:
    """Return BGI ruby display text, dropping pronunciation-only wrappers."""
    return _BGI_RUBY_RE.sub(lambda m: m.group(1), text or "")


def is_bgi_ruby_text(text: str) -> bool:
    return bool(_BGI_RUBY_RE.search(text or ""))


def translation_source_for_item(item_or_original) -> str:
    """Return the text sent to translators.

    Engine display markup that should not appear in Chinese, such as BGI ruby
    pronunciation wrappers, is removed here. Repack still binds the resulting
    translation to the original TextItem; only the API/cache source text changes.
    """
    if isinstance(item_or_original, str):
        return item_or_original
    return validation_source_for_item(item_or_original)


def validation_source_for_item(item_or_original, translated: str | None = None) -> str:
    """Choose the source string used for translation safety validation.

    BGI uses ``<r kana>visible</r>`` for ruby text. The tag is display markup,
    not a script control code that must survive in Chinese output, so BGI
    translations are validated against the visible text.
    """
    if isinstance(item_or_original, str):
        return item_or_original
    original = str(getattr(item_or_original, "original", "") or "")
    meta = getattr(item_or_original, "meta", {}) or {}
    is_bgi = bool(meta.get("arc") or meta.get("archive_kind") in {"arc20", "packfile"})
    if is_bgi and is_bgi_ruby_text(original):
        return strip_bgi_ruby(original)
    if meta.get("wolf_role") and "\\r[" in original:
        from engines.wolf.text_safety import strip_wolf_ruby

        return strip_wolf_ruby(original)
    return original


def _has_mixed_japanese_residual(original: str, translated: str) -> bool:
    """Detect Chinese-looking translations that still contain Japanese kana."""
    if not original or not translated:
        return False
    if original.strip() == translated.strip():
        return False
    original_visible = PLACEHOLDER_PATTERN.sub("", original)
    translated_visible = PLACEHOLDER_PATTERN.sub("", translated)
    # 说话人/控制标签（如 <ルルゥ>）在原文和译文中必须原样保留，不是翻译内容。
    # 剥离两边共有的 <...> 标签后再判残留，避免把标签里的日文人名误判为漏翻，
    # 触发无休止的"回退原文→重译"（实测 621 次风暴）。
    original_visible, translated_visible = _strip_shared_angle_tags(
        original_visible, translated_visible
    )
    if not _KANA_RE.search(original_visible) or not _KANA_RE.search(translated_visible):
        return False
    if _allows_intentional_kana_terms(original_visible, translated_visible):
        return False
    return bool(_CJK_RE.search(translated_visible))


_ANGLE_TAG_RE = re.compile(r"<[^<>]*>")


def _strip_shared_angle_tags(original: str, translated: str) -> tuple[str, str]:
    """Strip <...> angle-tag control markers shared verbatim by both sides.

    RPG Maker / KiriKiri speaker and style tags (e.g. ``<ルルゥ>``, ``<color>``)
    are authored controls that a correct translation must preserve unchanged.
    Stripping the tags that appear identically on both sides lets the kana-residual
    check look at the actual dialogue content instead of flagging the tag itself.
    """
    orig_tags = set(_ANGLE_TAG_RE.findall(original))
    trans_tags = set(_ANGLE_TAG_RE.findall(translated))
    shared = orig_tags & trans_tags
    if not shared:
        return original, translated
    for tag in shared:
        original = original.replace(tag, "")
        translated = translated.replace(tag, "")
    return original, translated


def _allows_intentional_kana_terms(original: str, translated: str) -> bool:
    """Allow short quoted kana terms used in language/puzzle explanations."""
    matches = list(_KANA_SEQ_RE.finditer(translated))
    if not matches:
        return True
    if not _CJK_RE.search(translated):
        return False
    total_kana = sum(len(m.group(0)) for m in matches)
    if total_kana > 24:
        return False

    has_language_context = bool(_LANGUAGE_TERM_RE.search(translated))
    for match in matches:
        token = match.group(0)
        if token not in original:
            return False
        if _is_inside_short_quote(translated, match.start(), match.end()) and (has_language_context or len(token) <= 1):
            continue
        if has_language_context and len(token) <= 12:
            continue
        return False
    return True


def _is_inside_short_quote(text: str, start: int, end: int) -> bool:
    for opener, closer in _SHORT_QUOTE_PAIRS:
        left = text.rfind(opener, 0, start + 1)
        if left < 0:
            continue
        right = text.find(closer, end)
        if right < 0:
            continue
        if opener == closer and right == left:
            right = text.find(closer, end)
            if right < 0:
                continue
        content_len = right - left - len(opener)
        if 0 <= content_len <= 16:
            return True
    return False


def normalize_translation_quotes(original: str, translated: str) -> tuple[str, str | None]:
    """Keep outer dialogue quotes consistent with the source text."""
    original_s = (original or "").strip()
    translated_s = (translated or "").strip()
    if not original_s or not translated_s:
        return translated, None

    original_pair = _outer_quote_pair(original_s)
    translated_pair = _outer_quote_pair(translated_s)

    if original_pair:
        if translated_pair:
            inner = translated_s[len(translated_pair[0]):len(translated_s) - len(translated_pair[1])].strip()
        else:
            inner = translated_s
        normalized = original_pair[0] + inner + original_pair[1]
        if normalized != translated:
            return normalized, "警告：已按原文统一外层台词引号"
        return translated, None

    if translated_pair:
        inner = translated_s[len(translated_pair[0]):len(translated_s) - len(translated_pair[1])].strip()
        if inner:
            return inner, "警告：原文无外层引号，已去除 AI 额外引号"
    return translated, None


def _outer_quote_pair(text: str) -> tuple[str, str] | None:
    stripped = (text or "").strip()
    for opener, closer in _OUTER_QUOTE_PAIRS:
        if len(stripped) >= len(opener) + len(closer) and stripped.startswith(opener) and stripped.endswith(closer):
            return opener, closer
    return None


def verify_translation(original: str, translated: str) -> tuple[str, list[str]]:
    """
    安全校验拦截器 —— 防线 1 & 2 的核心。
    返回 (安全译文, 警告列表)。
    如果译文破坏了控制符，强制回退到原文。
    """
    warnings = []
    if is_punctuation_only(original):
        if (translated or "").strip() != (original or "").strip():
            warnings.append("纯标点/符号不应翻译，已回退原文")
        return original, warnings

    if _looks_like_model_refusal_or_explanation(translated):
        warnings.append("检测到模型说明/拒答式输出，已回退原文")
        return original, warnings

    # Normalize literal line breaks before placeholder validation. Some models
    # turn RPG Maker "\\n" text controls into real newlines.
    if "\n" not in original and "\n" in translated:
        warnings.append("警告：AI 输出了换行符，已自动替换为 \\n")
        translated = translated.replace("\n", "\\n")
    if "[・]" in original and "[·]" in translated:
        warnings.append("警告：已将 KiriKiri 强调点 [·] 归一为 [・]")
        translated = translated.replace("[·]", "[・]")

    translated, quote_warning = normalize_translation_quotes(original, translated)
    if quote_warning:
        warnings.append(quote_warning)

    # 检查 1: 占位符完整性
    # 快速路径：原文不含占位符特征字符则跳过昂贵的正则扫描
    if _PLACEHOLDER_TRIGGER.search(original) or _PLACEHOLDER_TRIGGER.search(translated):
        orig_placeholders = Counter(extract_placeholders(original))
        trans_placeholders = Counter(extract_placeholders(translated))
    else:
        orig_placeholders = Counter()
        trans_placeholders = Counter()

    missing = orig_placeholders - trans_placeholders
    extra = trans_placeholders - orig_placeholders

    if missing:
        translated, _restored = _restore_missing_leading_at_tags(original, translated, set(missing))
        trans_placeholders = Counter(extract_placeholders(translated))
        missing = orig_placeholders - trans_placeholders
        hard_missing = Counter({
            token: count
            for token, count in missing.items()
            if not _is_soft_missing_placeholder(token)
        })
        if hard_missing:
            tag_list = ", ".join(sorted(hard_missing.elements()))
            warnings.append(f"危险！AI 破坏了控制符 [{tag_list}]，自动拦截并回滚原文！")
            return original, warnings
        if missing:
            warnings.append(
                f"警告：译文缺少显示控制符 [{', '.join(sorted(missing.elements()))}]，已保留译文"
            )
    if extra:
        warnings.append(f"警告：译文多出了控制符 [{', '.join(sorted(extra.elements()))}]，已去除")
        for token, count in extra.items():
            translated = translated.replace(token, "", count)

    # 检查 1.5: {{PH 占位符残留 — AI 幻想了占位符或原有的没被还原
    import re
    ph_residual = re.findall(r'\{\{?\s*PH\s*\d+\s*\}?\}', translated, re.IGNORECASE)
    if ph_residual:
        warnings.append(f"检测到漏网占位符残留 {ph_residual}，已清理")
        for r in ph_residual:
            translated = translated.replace(r, "")

    # 检查 2: 自动修复已知前缀泄露
    for pattern, replacement in _AUTO_FIX_PREFIXES:
        if pattern.search(translated):
            warnings.append(f"检测到 API 前缀泄露，已自动去除")
            translated = pattern.sub(replacement, translated)

    if _looks_like_short_source_overexplained(original, translated):
        warnings.append("短文本被翻成解释性长句，已回退原文")
        return original, warnings

    # 检查 3: 危险模式扫描
    for pattern, desc in DANGEROUS_PATTERNS:
        if pattern.search(translated):
            warnings.append(f"检测到可疑模式 [{desc}]，请人工检查")

    # 检查 6: 空翻译
    if not translated or not translated.strip():
        warnings.append("AI 返回了空翻译，回退原文")
        return original, warnings

    # 检查 7: 常见 mojibake 乱码。出现这类译文通常来自污染缓存或错误编码。
    if is_mojibake(translated):
        warnings.append("检测到疑似编码乱码，已回退原文")
        return original, warnings

    if _has_mixed_japanese_residual(original, translated):
        warnings.append("译文仍包含日文假名残留，已回退原文并触发重译")
        return original, warnings

    # 检查 8: 控制符语义 token 完整性 (RPG Maker hook v3)
    # __COLOR_5__ __WAIT__ 等 token 的 type+count 必须完整保留
    ctrl_ok, ctrl_msg = _validate_ctrl_tokens(original, translated)
    if not ctrl_ok:
        warnings.append(ctrl_msg)
        return original, warnings  # triggers AI retry with feedback

    return translated, warnings


def _looks_like_model_refusal_or_explanation(text: str) -> bool:
    stripped = (text or "").strip()
    if not stripped:
        return False
    if _looks_like_parenthetical_term_explanation(stripped):
        return True
    refusal_patterns = (
        r"您(?:没有|未)(?:提供|给出)",
        r"没有提供(?:具体|完整|需要翻译|日语|原文)",
        r"(?:可以|可)(?:翻译|译)(?:为|作)",
        r"通常(?:直接)?(?:翻译|译)(?:为|作)",
        r"具体取决于",
        r"根据(?:您|你)要求",
        r"如果是在.*(?:语境|游戏|界面|对话)",
        r"最(?:通用|自然|常见).*译法",
        r"自然流畅的中文",
        r"日语游戏文本",
        r"\*\*",
        r"(?:抱歉|对不起|不好意思)[，,、\s]*(?:我)?(?:无法|不能|没法)(?:为你|帮你|帮忙|识别|翻译|进行翻译|处理|提供|回答|生成|协助)",
        r"无法(?:识别|翻译|进行翻译)(?:[，,。；;]|$)",
        r"不能(?:识别|翻译|进行翻译)(?:[，,。；;]|$)",
        r"看起来像(?:是)?(?:乱码|编码错误|格式错误|特殊字符|占位符|键盘误输入|加密字符)",
        r"并(?:不是|非)(?:完整的|有效的|有意义的)?(?:英语|日语|游戏)?文本",
        r"请(?:检查|确认|重新)(?:并)?(?:提供|发送)",
        r"只提供了",
        r"没有提供完整",
        r"请提供",
        r"需要翻译的具体",
        r"保留原样",
        r"译作",
        r"（人名[，,、)]|人名[，,、]?(?:保留|译作|音译)",
        r"Output only",
        r"I(?:'|’)m sorry",
        r"I cannot",
        r"As an AI",
    )
    return any(re.search(pattern, stripped, re.IGNORECASE) for pattern in refusal_patterns)


def _looks_like_short_source_overexplained(original: str, translated: str) -> bool:
    orig_len = _visible_len(original)
    trans_len = _visible_len(translated)
    if orig_len <= 0:
        return False
    if orig_len <= 4 and trans_len >= max(12, orig_len * 3):
        return True
    if orig_len <= 8 and trans_len >= max(36, orig_len * 7):
        markers = (
            "意思", "含义", "译为", "翻译", "语境", "通常", "如果", "可以",
            "根据", "原文", "日语", "游戏文本", "保留原意",
        )
        return any(marker in translated for marker in markers)
    return False


def _looks_like_parenthetical_term_explanation(text: str) -> bool:
    """Detect glossary/encyclopedia notes hallucinated as a translation."""
    if len(text) > 80:
        return False
    match = re.fullmatch(r"([\w\u3400-\u4dbf\u4e00-\u9fff\u30a0-\u30ff・ー·]{1,16})[（(]([^）)]{2,48})[）)]", text)
    if not match:
        return False
    note = match.group(2)
    explanation_markers = (
        "人名", "地名", "国名", "战舰", "舰名", "船名", "姓", "名字",
        "角色", "人物", "专有名词", "音译", "读作", "译作", "意为",
        "意思", "又称", "指", "日本", "英文", "中文", "原文",
        "name", "means", "literal",
    )
    return any(marker.lower() in note.lower() for marker in explanation_markers)


# ---- RPG Maker control token validation ----

_CTRL_TOKEN_RE = re.compile(r"__([A-Z][A-Z0-9]*(?:_[a-zA-Z0-9]+)?)__")


def _extract_ctrl_token_types(text: str) -> dict[str, int]:
    """Count control tokens by base type: {'COLOR': 2, 'WAIT': 1}"""
    counts: dict[str, int] = {}
    for m in _CTRL_TOKEN_RE.finditer(text):
        token = m.group(1)
        base = re.sub(r"_\d+$", "", token)  # COLOR_5 → COLOR
        counts[base] = counts.get(base, 0) + 1
    return counts


def _validate_ctrl_tokens(original: str, translated: str) -> tuple[bool, str]:
    """Ensure control token types and counts are preserved in translation.
    Returns (ok, reason)."""
    orig_types = _extract_ctrl_token_types(original)
    if not orig_types:
        return True, ""

    trans_types = _extract_ctrl_token_types(translated)

    # Missing tokens
    for typ, count in orig_types.items():
        trans_count = trans_types.get(typ, 0)
        if trans_count < count:
            return False, (
                f"危险！AI 破坏了控制符 __{typ}__ "
                f"(期望{count}个 实际{trans_count}个)，自动拦截并回滚原文！"
            )

    # Extra tokens (AI hallucinated)
    for typ, count in trans_types.items():
        if typ not in orig_types:
            return False, (
                f"危险！AI 产生了不存在的控制符 __{typ}__，自动拦截并回滚原文！"
            )

    return True, ""


def is_mojibake(text: str) -> bool:
    if not text:
        return False
    # 高频 UTF-8 bytes 被当作 GBK/CP936 解码后的汉字碎片。
    if "€" in text and any("\u4e00" <= c <= "\u9fff" for c in text):
        return True
    suspicious_fragments = (
        "浣犲", "鍛€", "鎴", "戜", "綘", "鍚", "楀", "鐨",
        "涓", "璇", "浜", "妗", "姝", "绔", "鈥", "銆",
        "ä½", "å¥", "Ã", "Â",
    )
    if any(fragment in text for fragment in suspicious_fragments):
        return True
    suspicious_chars = set("浣鎴戜綘鍚楀ソ鏄鐨涓璇浜妗姝绔鈥銆鍛")
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    suspicious = sum(1 for c in text if c in suspicious_chars)
    if suspicious >= 3 and suspicious / max(cjk, 1) > 0.45:
        return True
    # Latin-1 mojibake fragments that sometimes leak through APIs/logs.
    return any(fragment in text for fragment in ("Ã", "Â", "�"))


def is_translatable(text: str) -> bool:
    """判断文本是否可翻译：包含自然语言句子，排除路径/变量/代码。"""
    if not text or not text.strip():
        return False
    stripped = text.strip()
    if len(stripped) < 1:
        return False

    # 排除含控制字符的二进制数据
    if any(0 < ord(c) < 32 for c in stripped):
        return False

    # 排除 Godot 资源路径和 UID
    if re.match(r"^(uid|res)://", stripped):
        return False

    # Exclude Windows paths before placeholder stripping. RPG Maker controls
    # such as \p are valid placeholders, but in C:\Program Files the same
    # pattern is just a path segment and should not be stripped first.
    if re.match(r"^[A-Za-z]:\\", stripped) or re.match(r"^(?:\.\.?\\)", stripped):
        return False

    # 先去掉 BBCode/占位符，避免 [/b] 等被误判为路径
    no_placeholders = PLACEHOLDER_PATTERN.sub("", stripped).strip()

    # 排除文件路径和资源引用（基于去占位符后的文本）
    if re.search(r"\.(png|jpg|jpeg|ogg|wav|mp3|webm|avi|rpy|rpyc|py|pyo)$", no_placeholders, re.IGNORECASE):
        return False
    if "/" in no_placeholders and " " not in no_placeholders:
        return False
    # Windows 反斜杠路径: C:\..., ..\..., .\...
    if "\\" in no_placeholders:
        if " " not in no_placeholders:
            return False
        # 含空格也可能是 Windows 路径（如 C:\Program Files\...）
        if re.match(r"^[A-Za-z]:\\", no_placeholders):
            return False

    # 排除纯控制/占位符文本（去掉占位符后无实际内容）
    if len(no_placeholders) < 1:
        return False

    # 排除全大写标识符（Godot 属性值如 TEXTURE, INLINE, RANDOM, STEP 等）
    # 也排除单字符大写字母（A, E, X 等按键提示）
    if re.match(r"^[A-Z][A-Z0-9_]*$", stripped):
        if len(stripped) == 1:
            return False
        if len(stripped) >= 2:
            # 全大写标识符且非已知 UI 词汇 → 排除
            _KNOWN_UI_CAPS = {'OK', 'YES', 'NO', 'ON', 'OFF'}
            if stripped not in _KNOWN_UI_CAPS:
                return False

    # 排除纯粹的场景/角色/样式标识符（短且无自然语言结构）
    if re.match(r"^[a-z][a-z0-9_ ]{1,30}$", stripped) and " " not in stripped:
        return False
    if re.match(r"^[a-z]+ [a-z0-9_]+$", stripped):  # e.g. "bg club_day", "natsuki ghost3"
        return False

    # 排除包含 Ren'Py 插值变量的纯代码行
    if re.search(r"\[currentpoem\.", stripped):
        return False

    # 排除十六进制颜色代码
    if re.match(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$", stripped):
        return False

    # 必须有字母，且不是纯代码标识符
    has_letter = bool(re.search(r"[a-zA-Z぀-ゟ゠-ヿ一-鿿가-힯Ѐ-ӿ]", stripped))
    if not has_letter:
        return False

    # 排除纯代码标识符（全小写/下划线/驼峰），但保留 UI 短文本（首字母大写如 OK/No/Yes）
    code_like = re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", stripped)
    if code_like:
        # 全小写或下划线分隔 → 代码标识符
        if not re.search(r"[A-Z]", stripped):
            return False
        if "_" in stripped:
            return False
        # 短标识符含数字 → 代码（如 G1, A0, pos2）
        if len(stripped) <= 5 and re.search(r"[0-9]", stripped):
            return False
        # 长标识符多单词驼峰 → 类名/变量名
        # 两个条件之一: (a) ≥2 大写字母, 或 (b) 以小写开头含大写（标准 camelCase）
        if len(stripped) > 5 and re.search(r"[a-z]", stripped):
            upper_count = sum(1 for c in stripped if c.isupper())
            if upper_count >= 2:
                return False
            # 标准 camelCase: 小写开头 + 含大写（如 playerHealth, getValue）
            if stripped[0].islower() and upper_count >= 1:
                return False

    # 排除以逗号分隔的参数/代码片段（如 ", name, "）
    comma_sep = stripped.replace(" ", "")
    if len(comma_sep) > 0 and all(c in ",;:()[]{}._-" for c in comma_sep):
        return False
    # 排除空参数引用（", name, " → "name" 含字母会通过上面检查，但模式可疑）
    if stripped.startswith(",") or stripped.startswith(";"):
        return False

    # 排除场景/章节 ID: 02_A, 03_B1, 02_A_event
    if re.match(r"^\d{2,3}_[A-Z][A-Z0-9]?(_.*)?$", stripped):
        return False

    # 排除函数/方法调用: setLevel(5), myFunc(args), obj.method()
    if re.search(r"^\w+\s*\([^)]*\)\s*;?\s*$", stripped):
        return False
    # 排除方法链: obj.method().another()（纯 ASCII 且无自然语言特征）
    if re.search(r"\w+\.\w+\s*\(", stripped) and not re.search(r"[一-鿿぀-ゟ가-힯]", stripped):
        return False

    # 排除赋值语句: x = 42, name = "value"
    if re.match(r"^[a-zA-Z_]\w*\s*=\s*.+", stripped):
        return False

    # 排除含 JS/TS 关键字的代码行（需 ≥2 个关键字，避免误杀含 "new", "return" 等的游戏文本）
    _JS_KEYWORDS = frozenset({
        'function', 'var', 'let', 'const', 'if', 'else', 'for', 'while',
        'switch', 'case', 'break', 'typeof', 'instanceof', 'this',
        'class', 'extends', 'import', 'export', 'default', 'async', 'await', 'yield',
        'throw', 'try', 'catch', 'finally', 'delete', 'void', 'undefined',
        'false', 'require', 'module', 'process', 'static', 'public', 'private',
        'interface', 'implements', 'abstract', 'enum', 'protected', 'super',
    })
    # Also catch: line starts with JS keyword + code-like syntax
    _JS_LINE_START = re.compile(
        r"^(var|let|const|function|if|for|while|switch|return|throw|try|class|import|export|typeof|"
        r"private|public|protected|static|async|await|interface|enum|delete|void|new)\s"
    )
    # 含 JS 关键字 + 赋值或分号 = 代码行（如 "private _hidden = null;"）
    _JS_WITH_ASSIGN = re.compile(
        r"\b(var|let|const|function|private|public|protected|static|async|return|throw|delete|void)\b.*[=;]"
    )
    if _JS_LINE_START.match(stripped):
        return False
    if _JS_WITH_ASSIGN.search(stripped):
        return False
    tokens = [w.lower() for w in re.findall(r"\b\w+\b", stripped)]
    if tokens:
        js_tokens = [t for t in tokens if t in _JS_KEYWORDS]
        if len(js_tokens) >= 2:
            return False

    # 排除 SQL 片段
    if re.match(r"^(SELECT|INSERT|UPDATE|DELETE|CREATE|DROP|ALTER)\s", stripped, re.IGNORECASE):
        return False

    # 排除纯标点/符号字符串
    if not re.search(r"\w", stripped):
        return False

    # 排除括号包裹的单标识符: (nil), (null), (empty)
    if re.match(r"^\(\w+\)$", stripped):
        return False

    # 排除带方法链的赋值: character._interpreter = null;
    if re.search(r"\w+\.\w+\s*=", stripped) or re.search(r"=\s*\w+\.\w+", stripped):
        return False

    # 必须有至少一个空格（自然语言特征），除非是单个有意义的短语
    # CJK 文本不使用空格，不适用此规则
    if " " not in stripped and len(stripped) > 15:
        if not re.search(r"[぀-ゟ゠-ヿ一-鿿]", stripped):
            return False  # 很长的无空格字符串可能是代码

    return True


# ---- 占位符保护：翻译前替换，翻译后还原 ----

_PROTECT_PATTERN = re.compile(
    r"("
    r"\{[a-zA-Z_][a-zA-Z0-9_]*\}|"          # {i} {/i} {nw} {fast} etc.
    r"\{/[a-zA-Z_]+\}|"
    r"\{[a-zA-Z_]+=[^}]*\}|"                # {color=#fff} {size=+10}
    + SQUARE_PLACEHOLDER + r"|"              # [gtext] [color=#fff] [0], not bracketed dialogue
    r"#[0-9a-fA-F]{3,8}\b|"                 # #fff #ffffff (加\b避免误杀中文#标签)
    + KIRIKIRI_PERCENT_CONTROL + r"|"        # KiriKiri/Cxdec controls: %p-1; %p; %fuser; %50;
    + KIRIKIRI_EMPHASIS_MARK + r"|"          # KiriKiri emphasis marker: [・]君[・]は
    r"%[0-9.]*[a-zA-Z]|"                    # %s %d %02d %A %B %Y %H %M etc.
    r"\r\n|\r|\n|"                         # Preserve authored display line breaks.
    + BACKSLASH_CONTROL + r"|"
    + EMPTY_BRACE_CONTROL + r"|"             # Empty engine control marker: {}
    + ANGLE_CONTROL_TAG + r"|"                # Includes CJK speaker tags.
    r"@[a-zA-Z_][a-zA-Z0-9_]*\b|"            # @name, @proto1
    r"__[A-Z][A-Z0-9]*(?:_[a-zA-Z0-9]+)?__"  # RPG Maker hook v3 semantic tokens: __COLOR_5__ __WAIT__
    r")"
)


def protect_placeholders(text: str) -> tuple[str, list[str]]:
    """翻译前：将控制符替换为安全占位符 {{PH0}} {{PH1}} ..."""
    placeholders: list[str] = []

    def _replace(m):
        placeholders.append(m.group(0))
        return f"{{{{PH{len(placeholders) - 1}}}}}"

    protected = _PROTECT_PATTERN.sub(_replace, text)
    return protected, placeholders


def restore_placeholders(text: str, placeholders: list[str]) -> str:
    """翻译后：将 {{PH0}} {{PH1}} ... 还原为原始控制符。"""
    import re
    # 标准化 AI 可能产生的各种占位符变体
    # {{PH0}}, {{ PH0 }}, {{ph0}}, {PH0}, 【PH0】, [PH0], (PH0)
    text = re.sub(r'\{\{?\s*PH\s*(\d+)\s*\}?\}', r'{{PH\g<1>}}', text, flags=re.IGNORECASE)
    text = re.sub(r'[【\[]\s*PH\s*(\d+)\s*[】\]]', r'{{PH\g<1>}}', text, flags=re.IGNORECASE)
    text = re.sub(r'\(\s*PH\s*(\d+)\s*\)', r'{{PH\g<1>}}', text, flags=re.IGNORECASE)
    for i, ph in enumerate(placeholders):
        text = text.replace(f"{{{{PH{i}}}}}", ph)
    return text


def split_long_text(text: str, max_chars: int = 2000) -> list[str]:
    """按句子边界拆分长文本。"""
    if len(text) <= max_chars:
        return [text]
    chunks = []
    sentences = re.split(r"(?<=[。！？.!?\n])", text)
    current = ""
    for s in sentences:
        if len(current) + len(s) > max_chars and current:
            chunks.append(current)
            current = s
        else:
            current += s
    if current:
        chunks.append(current)
    return chunks or [text]


# ---- 专有名词保护：翻译前替换，翻译后还原 ----

# 常见游戏专有名词（角色名、地名、技能名等），防止被机器翻译强行音译
# 用户可通过配置文件扩展此词典
DEFAULT_PROPER_NOUNS: dict[str, str] = {
    # 角色名 → 保留原文（或用户指定译名）
}

# 默认保留原文的专有名词（常见英文名/地名/技能名模式）
_PROPER_NOUN_PATTERNS = [
    re.compile(r'\b[A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,})+\b'),  # 多词专名: Artoria Pendragon
    re.compile(r'\b[A-Z][a-z]{2,}\b'),  # 单词首字母大写: Artoria, Mondstadt
]

# 占位符标记
_NOUN_PH_PATTERN = re.compile(r'@@N(\d+)@@')


def protect_proper_nouns(text: str, user_dict: dict[str, str] | None = None) -> tuple[str, list[str], list[str]]:
    """翻译前：将专有名词替换为安全占位符 @@N0@@ @@N1@@ ...

    返回 (保护后文本, 占位符列表, 原词列表)
    """
    nouns: list[str] = []  # 原词
    placeholders: list[str] = []  # @@N0@@

    # 合并用户词典和默认模式
    protected: set[str] = set()
    if user_dict:
        for k, v in user_dict.items():
            if v == "" or v == k:  # 保留原文
                protected.add(k)

    def _replace(m):
        word = m.group(0)
        if word in protected:
            idx = len(nouns)
            nouns.append(word)
            placeholders.append(f"@@N{idx}@@")
            return f"@@N{idx}@@"
        # 自动检测：首字母大写的连续词可能是专名
        if re.match(r'^[A-Z][a-z]{2,}', word):
            idx = len(nouns)
            nouns.append(word)
            placeholders.append(f"@@N{idx}@@")
            return f"@@N{idx}@@"
        return word

    # 先替换 user_dict 中的精确匹配
    result = text
    for k, v in (user_dict or {}).items():
        if v == "" or v == k:
            if k not in protected:
                protected.add(k)
            if k in result:
                idx = len(nouns)
                nouns.append(k)
                ph = f"@@N{idx}@@"
                placeholders.append(ph)
                result = result.replace(k, ph)

    # 再用模式匹配
    for pattern in _PROPER_NOUN_PATTERNS:
        result = pattern.sub(_replace, result)

    return result, placeholders, nouns


def restore_proper_nouns(text: str, placeholders: list[str], nouns: list[str]) -> str:
    """翻译后：将 @@N0@@ @@N1@@ 还原为原始专有名词。"""
    # 标准化 AI 可能产生的变体
    text = re.sub(r'@@\s*N(\d+)\s*@@', r'@@N\g<1>@@', text)
    text = re.sub(r'[【\[]\s*N(\d+)\s*[】\]]', r'@@N\g<1>@@', text)

    for i, (ph, noun) in enumerate(zip(placeholders, nouns)):
        text = text.replace(f"@@N{i}@@", noun)
        # 也尝试 AI 可能误改的形态
        text = text.replace(ph, noun)
    return text


def load_proper_noun_dict(dict_path: str) -> dict[str, str]:
    """从文件加载专有名词词典。

    文件格式: 每行 "原文=译文" 或 "原文"（表示保留原文）
    # 开头为注释
    """
    from pathlib import Path
    result: dict[str, str] = {}
    p = Path(dict_path)
    if not p.exists():
        return result
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            result[k.strip()] = v.strip()
        else:
            result[line] = ""  # 空值=保留原文
    return result
