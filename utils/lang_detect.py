import re


def detect_language(text: str) -> str:
    """基于字符集启发式检测源语言，返回 ISO 639-1 代码。"""
    if not text or not text.strip():
        return "unknown"

    text = text.strip()

    hiragana = len(re.findall(r"[぀-ゟ]", text))
    katakana = len(re.findall(r"[゠-ヿ]", text))
    kanji = len(re.findall(r"[一-鿿]", text))
    hangul = len(re.findall(r"[가-힯]", text))
    latin = len(re.findall(r"[a-zA-Z]", text))
    cyrillic = len(re.findall(r"[Ѐ-ӿ]", text))

    jp_score = hiragana * 3 + katakana * 2 + kanji * 1
    ko_score = hangul * 3
    en_score = latin * 1
    ru_score = cyrillic * 3

    scores = {"ja": jp_score, "ko": ko_score, "en": en_score, "ru": ru_score}
    best = max(scores, key=scores.get)

    if scores[best] == 0:
        return "unknown"
    return best


def detect_batch_language(texts: list[str]) -> str:
    """对一批文本检测主要语言。"""
    counts: dict[str, int] = {}
    for t in texts:
        lang = detect_language(t)
        if lang != "unknown":
            counts[lang] = counts.get(lang, 0) + 1
    if not counts:
        return "unknown"
    return max(counts, key=counts.get)
