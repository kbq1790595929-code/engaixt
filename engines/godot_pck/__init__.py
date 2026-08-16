"""Godot PCK Engine — handles packaged Godot games with Dialogic DTL timeline.

Supports:
- DTL (Dialogic Timeline) text extraction and patching
- SCN/RES binary safe string replacement (VARIANT_STRING format)
- CJK font replacement via Godot Editor import
- PCK v2 (GDPC) rebuilding

Flow:
  unpack() → extract PCK, parse DTL/SCN for translatable text
  repack() → patch DTL + SCN/RES + fonts → rebuild PCK → replace in game dir
"""


from engines.base import registry

from engines.godot_pck.dtl import extract_dtl_text, patch_dtl_content
from engines.godot_pck.engine import GodotPckEngine
from engines.godot_pck.pck import (
    _find_embedded_pck_range,
    _looks_like_godot_steam_pck,
    _parse_steam_entries,
    read_pck_payloads,
    rebuild_pck,
    rebuild_pck_steam,
)
from engines.godot_pck.text_formats import (
    extract_dialogue_txt,
    extract_tscn_strings,
    patch_dialogue_txt,
    patch_inline_strings_safe,
    patch_tscn_strings,
)

registry.register(GodotPckEngine())
