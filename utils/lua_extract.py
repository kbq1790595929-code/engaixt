"""Lua VN script text extractor for Unity TextAsset-based visual novels.

Handles common Lua-based visual novel helper calls:
- text("dialogue"), textr("msg"), textl("msg") — dialogue
- setname("character") — character names
- item[N]="choice" — selection choices
- Comments with Japanese scene descriptions
"""
import re
from dataclasses import dataclass, field


@dataclass
class LuaTextItem:
    """A translatable text item in a Lua script."""
    original: str
    line: int
    item_type: str  # 'text', 'setname', 'item', 'sel', 'seticon', 'comment'
    context: str = ""  # surrounding code context


# Patterns for text extraction
# text("...") — main dialogue/narration
TEXT_PATTERN = re.compile(
    r'(text[lr]?)\s*\(\s*"((?:[^"\\]|\\.)*)"\s*\)',
    re.MULTILINE
)

# setname("...") — character name
SETNAME_PATTERN = re.compile(
    r'setname\s*\(\s*"((?:[^"\\]|\\.)*)"\s*\)',
    re.MULTILINE
)

# item[N]="..." — choice items
ITEM_PATTERN = re.compile(
    r'item\s*\[(\d+)\]\s*=\s*"((?:[^"\\]|\\.)*)"',
    re.MULTILINE
)

# sel("...", item) — selection prompt
SEL_PATTERN = re.compile(
    r'sel\s*\(\s*"((?:[^"\\]|\\.)*)"',
    re.MULTILINE
)

# seticon("...") — icon name display in LINE-style windows
SETICON_PATTERN = re.compile(
    r'seticon\s*\(\s*"((?:[^"\\]|\\.)*)"\s*\)',
    re.MULTILINE
)

# Comments with Japanese text (scene descriptions / stage directions)
COMMENT_PATTERN = re.compile(
    r'--\s*([^\n]+)',
    re.MULTILINE
)

# All extraction patterns in priority order
# Each rule: (type, pattern, getter, content_group_index)
EXTRACTION_RULES = [
    ('text', TEXT_PATTERN, lambda m: m.group(2), 2),
    ('setname', SETNAME_PATTERN, lambda m: m.group(1), 1),
    ('item', ITEM_PATTERN, lambda m: m.group(2), 2),
    ('sel', SEL_PATTERN, lambda m: m.group(1), 1),
    ('seticon', SETICON_PATTERN, lambda m: m.group(1), 1),
]


def _unescape_lua(s: str) -> str:
    """Convert Lua escape sequences to actual characters."""
    return s.replace('\\n', '\n').replace('\\t', '\t').replace('\\r', '\r').replace('\\"', '"').replace("\\'", "'").replace('\\\\', '\\')


def _escape_lua(s: str) -> str:
    """Convert actual characters to Lua escape sequences."""
    return s.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\t', '\\t').replace('\r', '\\r')


def extract_texts(lua_code: str) -> list[dict]:
    """Extract all translatable text items from a Lua script.

    Returns list of dicts with keys: original, line, type, context
    """
    items = []

    # 1. Extract function call strings
    for item_type, pattern, getter, content_group in EXTRACTION_RULES:
        for m in pattern.finditer(lua_code):
            raw = getter(m)
            unescaped = _unescape_lua(raw)
            if not _is_translatable(unescaped):
                continue
            line = lua_code[:m.start()].count('\n') + 1
            lines_before = lua_code[:m.start()].split('\n')
            ctx_start = max(0, len(lines_before) - 3)
            context = '\n'.join(lines_before[ctx_start:])
            items.append({
                'original': unescaped,
                'line': line,
                'type': item_type,
                'context': context[-200:],
                '_raw': raw,
                '_start': m.start(content_group),
                '_end': m.end(content_group),
            })

    # 2. Extract comments (Japanese scene descriptions)
    for m in COMMENT_PATTERN.finditer(lua_code):
        comment_text = m.group(1).strip()
        if not _is_translatable(comment_text):
            continue
        # Skip pure punctuation/symbols and code-like comments
        if re.match(r'^[=▼▽▲△▼◆◇■□●○★☆→←↑↓➡\s\-—\-]+$', comment_text):
            continue
        # Skip Lua keywords and structural comments
        if re.match(r'^(block_\d+|LINE|選択肢|選択結果|【.*】)$', comment_text):
            continue
        line = lua_code[:m.start()].count('\n') + 1
        items.append({
            'original': comment_text,
            'line': line,
            'type': 'comment',
            'context': '',
            '_raw': comment_text,
            '_start': m.start(1),
            '_end': m.end(1),
        })

    return items


def _is_translatable(text: str) -> bool:
    """Check if text contains natural language content worth translating."""
    if not text or not text.strip():
        return False
    stripped = text.strip()
    if len(stripped) < 1:
        return False
    # Must have CJK characters or be a multi-word phrase
    has_cjk = bool(re.search(r'[぀-ヿ一-鿿]', stripped))
    if not has_cjk:
        return False
    # Skip image/file paths
    if re.match(r'^[\w_\-/]+\.(png|jpg|ogg|wav|mp3)$', stripped, re.IGNORECASE):
        return False
    return True


def apply_translations(lua_code: str, items: list[dict]) -> str:
    """Apply translations back to the Lua code using regex matching.

    Builds a translation map from items and re-matches all calls in the Lua code.
    This is more robust than position-based replacement when line endings or
    encoding may have shifted between extract and repack passes.
    """
    # Build translation map: (type, raw_content) → translated
    # raw_content is the Lua source string content (with escape sequences),
    # which matches what we get from regex group captures
    trans_map: dict[tuple[str, str], str] = {}
    for item in items:
        key = (item['type'], item.get('original', ''))
        if item.get('translated') and item['translated'] != item['original']:
            trans_map[key] = item['translated']

    if not trans_map:
        return lua_code

    # Rebuild map with unescaped keys for regex matching
    # Items store 'original' as raw (with Lua escapes). But regex group
    # captures are also raw. So we use raw content directly for lookup.
    import re as _re

    def _replace_text(m: _re.Match) -> str:
        raw = m.group(2)  # raw Lua content, with escape sequences
        translated = trans_map.get(('text', raw))
        if translated is None:
            return m.group(0)
        return f'{m.group(1)}("{_escape_lua(translated)}")'

    def _replace_setname(m: _re.Match) -> str:
        raw = m.group(1)
        translated = trans_map.get(('setname', raw))
        if translated is None:
            return m.group(0)
        return f'setname("{_escape_lua(translated)}")'

    result = _re.sub(TEXT_PATTERN, _replace_text, lua_code)
    result = _re.sub(SETNAME_PATTERN, _replace_setname, result)

    def _replace_item(m: _re.Match) -> str:
        raw = m.group(2)
        translated = trans_map.get(('item', raw))
        if translated is None:
            return m.group(0)
        return f'item[{m.group(1)}]="{_escape_lua(translated)}"'

    result = _re.sub(ITEM_PATTERN, _replace_item, result)

    def _replace_sel(m: _re.Match) -> str:
        raw = m.group(1)
        translated = trans_map.get(('sel', raw))
        if translated is None:
            return m.group(0)
        return f'sel("{_escape_lua(translated)}"'

    result = _re.sub(SEL_PATTERN, _replace_sel, result)

    def _replace_seticon(m: _re.Match) -> str:
        raw = m.group(1)
        translated = trans_map.get(('seticon', raw))
        if translated is None:
            return m.group(0)
        return f'seticon("{_escape_lua(translated)}")'

    result = _re.sub(SETICON_PATTERN, _replace_seticon, result)

    return result


def split_long_dialogue(text: str, max_chars: int = 200) -> list[str]:
    """Split long dialogue into translatable chunks at sentence boundaries."""
    if len(text) <= max_chars:
        return [text]
    chunks = []
    sentences = re.split(r'(?<=[。！？.!?\n])', text)
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
