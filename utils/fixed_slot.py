"""Helpers for fixed-size script string slots."""
from __future__ import annotations

import re
from collections.abc import Callable


_PUNCT_REPLACEMENTS = str.maketrans({
    "，": "，",
    "。": "。",
    "！": "!",
    "？": "?",
    "；": "；",
    "：": "：",
    "“": "",
    "”": "",
    "‘": "",
    "’": "",
    "…": "…",
    "　": " ",
})

_PH_RE = re.compile(
    r"(%[0-9.]*[A-Za-z]|\\[A-Za-z]+\[[^\]]+\]|\\[A-Za-z]|<[^>]+>|\[[A-Za-z0-9_./:#=-]+\]|\{[^}]+\})"
)

_DROP_WORDS = [
    "其实", "真的", "明明", "大概", "或许", "可能", "应该", "似乎", "感觉", "总之",
    "不过", "但是", "然而", "而且", "于是", "然后", "因为", "所以", "如果", "的话",
    "这种", "那种", "这样的", "那样的", "一下", "一点", "一些", "有些", "非常",
    "特别", "十分", "相当", "简直", "完全", "已经", "正在", "开始", "继续",
    "起来", "下来", "过去", "回来", "出去", "进去", "出来", "当然", "反正",
]

_ROLE_REPLACEMENTS = [
    ("过于在意他人眼光的", "自意"),
    ("在意他人眼光的", "自意"),
    ("有明星范儿的", "明星"),
    ("明星范儿", "明星"),
    ("刷存在感的", "刷存在"),
    ("住宿生", "住宿"),
    ("宿舍生", "宿舍"),
    ("顾客", "客"),
    ("客人", "客"),
    ("女孩子", "女"),
    ("女生", "女"),
    ("女人", "女"),
    ("男性", "男"),
    ("男人", "男"),
    ("的死", ""),
    ("死女人", "女"),
]

_DROP_PARTICLES = [
    "的", "地", "得", "了", "着", "过", "个", "们", "啊", "呀", "哦", "啦", "呢", "吧",
]

_TINY_SLOT_REPLACEMENTS = [
    ("老爸", "爸"),
    ("爸爸", "爸"),
    ("父亲", "父"),
    ("老妈", "妈"),
    ("妈妈", "妈"),
    ("母亲", "母"),
    ("哥哥", "兄"),
    ("姐姐", "姐"),
    ("弟弟", "弟"),
    ("妹妹", "妹"),
]

_BOILERPLATE_PREFIXES = [
    "好的，这是您要求的自然流畅的中文翻译：",
    "好的，这是你要求的自然流畅的中文翻译：",
    "好的，以下是自然流畅的中文翻译：",
    "以下是自然流畅的中文翻译：",
    "这是自然流畅的中文翻译：",
    "中文翻译如下：",
    "翻译如下：",
    "译文如下：",
    "中文翻译：",
    "译文：",
]

_PHRASE_REPLACEMENTS = [
    ("没有办法", "没法"),
    ("不可以", "不行"),
    ("不能够", "不能"),
    ("是不是", "是否"),
    ("为什么", "为何"),
    ("怎么样", "怎样"),
    ("什么东西", "什么"),
    ("这种事情", "这事"),
    ("那种事情", "那事"),
    ("这件事情", "这事"),
    ("那件事情", "那事"),
    ("说起来", "说来"),
    ("看起来", "看来"),
    ("听起来", "听着"),
    ("总而言之", "总之"),
    ("不管怎么说", "总之"),
    ("与此同时", "同时"),
    ("也就是说", "即"),
    ("换句话说", "即"),
    ("没关系", "无妨"),
    ("没问题", "可以"),
    ("一瞬间", "瞬间"),
    ("一开始", "起初"),
    ("到最后", "最终"),
    ("那个时候", "那时"),
    ("这个时候", "此时"),
    ("现在", "如今"),
    ("马上", "立刻"),
    ("稍微", "稍"),
    ("终于", "终"),
    ("只不过", "只是"),
    ("就算", "即便"),
    ("虽然", "虽"),
    ("但是", "但"),
    ("可是", "可"),
    ("因为", "因"),
    ("所以", "故"),
]

_ENDING_REPLACEMENTS = [
    ("了吧", "吧"),
    ("了吗", "吗"),
    ("了呢", "呢"),
    ("的吧", "吧"),
    ("的呢", "呢"),
    ("啊", ""),
    ("呀", ""),
    ("哦", ""),
    ("啦", ""),
    ("呢", ""),
    ("吧", ""),
]


def fit_fixed_slot(
    text: str,
    max_bytes: int,
    encode: Callable[[str], bytes],
) -> str | None:
    """Return a readable shortened text that fits ``max_bytes`` when encoded."""
    text = clean_translation_text(text)
    for candidate in fixed_slot_candidates(text):
        if len(encode(candidate)) <= max_bytes:
            return candidate
    for candidate in _budget_trim_candidates(text):
        if len(encode(candidate)) <= max_bytes:
            return candidate
    return None


def fixed_slot_candidates(text: str) -> list[str]:
    """Generate deterministic compressed Chinese candidates, best first."""
    base = _normalize(clean_translation_text(text))
    candidates: list[str] = []

    def add(value: str) -> None:
        value = _normalize(value)
        if value and value not in candidates:
            candidates.append(value)

    add(base)
    add(_apply_tiny_slot_replacements(base))
    add(_apply_phrase_replacements(base))
    compact = _drop_parenthetical(_apply_phrase_replacements(base))
    add(compact)
    no_fillers = _drop_words(compact, conservative=True)
    add(no_fillers)
    add(_trim_sentence_endings(no_fillers))
    no_fillers_hard = _drop_words(compact, conservative=False)
    add(no_fillers_hard)
    add(_trim_sentence_endings(no_fillers_hard))
    add(_collapse_roles(no_fillers_hard))
    add(_drop_particles(_collapse_roles(no_fillers_hard)))

    for value in list(candidates):
        add(_strip_light_punctuation(value))
        add(_keep_first_clause(value))
        add(_keep_core_clauses(value, 2))
        add(_keep_core_clauses(value, 1))

    return [candidate for candidate in candidates if _same_placeholders(text, candidate)]


def clean_translation_text(text: str) -> str:
    """Remove common translator boilerplate before static repack."""
    output = _normalize(text)
    changed = True
    while changed:
        changed = False
        for prefix in _BOILERPLATE_PREFIXES:
            if output.startswith(prefix):
                output = output[len(prefix):].lstrip()
                changed = True
        cleaned = re.sub(
            r"^\s*(?:好的[，,。!！]?\s*)?(?:以下|下面)?(?:是|为)?(?:您|你)?(?:要求的)?"
            r"(?:自然流畅的)?(?:中文)?(?:翻译|译文)(?:如下|是|为)?[：:]\s*",
            "",
            output,
        )
        if cleaned != output:
            output = cleaned
            changed = True
    return output.strip()


def _normalize(text: str) -> str:
    text = text.translate(_PUNCT_REPLACEMENTS)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[，,]{2,}", "，", text)
    text = re.sub(r"\.{2,}", "…", text)
    text = re.sub(r"。{2,}", "。", text)
    text = re.sub(r"^。", "", text)
    text = re.sub(r"([「『（(《])。", r"\1", text)
    text = re.sub(r"。(?=[!?])", "", text)
    text = re.sub(r"([!?])。", r"\1", text)
    return text.strip()


def _apply_phrase_replacements(text: str) -> str:
    output = text
    for old, new in _PHRASE_REPLACEMENTS:
        output = output.replace(old, new)
    return output


def _apply_tiny_slot_replacements(text: str) -> str:
    output = text
    for old, new in _TINY_SLOT_REPLACEMENTS:
        if output == old:
            return new
        output = output.replace(old, new)
    return output


def _collapse_roles(text: str) -> str:
    output = text
    for old, new in _ROLE_REPLACEMENTS:
        output = output.replace(old, new)
    return output


def _drop_particles(text: str) -> str:
    output = text
    for word in _DROP_PARTICLES:
        output = output.replace(word, "")
    return output


def _drop_parenthetical(text: str) -> str:
    return re.sub(r"[（(][^）)]{1,20}[）)]", "", text)


def _drop_words(text: str, conservative: bool) -> str:
    output = text
    words = _DROP_WORDS[:24] if conservative else _DROP_WORDS
    for word in words:
        output = output.replace(word, "")
    return output


def _trim_sentence_endings(text: str) -> str:
    output = text
    changed = True
    while changed:
        changed = False
        for old, new in _ENDING_REPLACEMENTS:
            if output.endswith(old):
                output = output[: -len(old)] + new
                changed = True
    return output


def _strip_light_punctuation(text: str) -> str:
    return text.replace("，", "").replace("。", "").replace("；", "").replace("、", "")


def _strip_heavy_punctuation(text: str) -> str:
    return re.sub(r"[^\w\u3400-\u4dbf\u4e00-\u9fff]", "", text)


def _keep_first_clause(text: str) -> str:
    parts = [part for part in re.split(r"[。！？!?；;]", text) if part]
    return parts[0] if parts else text


def _keep_core_clauses(text: str, count: int) -> str:
    parts = [part for part in re.split(r"[，,。！？!?；;]", text) if part]
    if len(parts) <= count:
        return text
    return "，".join(parts[:count])


def _same_placeholders(original: str, candidate: str) -> bool:
    return _PH_RE.findall(original) == _PH_RE.findall(candidate)


def _budget_trim_candidates(text: str) -> list[str]:
    """Generate aggressive no-growth candidates for very small string slots.

    This intentionally avoids strings with placeholders/control tags. For tiny
    BGI fixed slots, a compact Chinese label is preferable to leaving Japanese
    behind, but dropping a tag would be worse than skipping the static patch.
    """
    if _PH_RE.search(text):
        return []

    base = _normalize(text)
    seeds = [
        base,
        _apply_tiny_slot_replacements(base),
        _apply_phrase_replacements(base),
        _collapse_roles(_apply_phrase_replacements(base)),
        _drop_particles(_collapse_roles(_apply_tiny_slot_replacements(_apply_phrase_replacements(base)))),
    ]
    candidates: list[str] = []

    def add(value: str) -> None:
        value = _normalize(_strip_heavy_punctuation(value))
        if value and value not in candidates:
            candidates.append(value)

    for seed in seeds:
        add(seed)
        add(_trim_sentence_endings(seed))
        add(_keep_first_clause(seed))
        add(_keep_core_clauses(seed, 1))
        add(_drop_particles(seed))

    for value in list(candidates):
        match = re.search(r"([A-Za-z0-9Ａ-Ｚａ-ｚ０-９]+)$", value)
        suffix = match.group(1) if match else ""
        stem = value[: -len(suffix)] if suffix else value
        chars = list(stem)
        for keep in range(len(chars), 0, -1):
            add("".join(chars[:keep]) + suffix)
            if suffix:
                add("".join(chars[-keep:]) + suffix)

    return [candidate for candidate in candidates if _same_placeholders(text, candidate)]
