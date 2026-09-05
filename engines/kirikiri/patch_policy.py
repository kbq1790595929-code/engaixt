"""Patch deployment policy for KiriKiri text sources.

The archive-level protection diagnosis is deliberately not used to choose the
deployment route. A game can contain one opaque XP3 entry beside ordinary,
statically extracted KAG scripts. Those scripts still need a regular root
``patch.xp3``; forcing all of them through a runtime bridge can make every
otherwise valid translation disappear when that bridge does not support the
game's executable.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from engines.base import TextItem


def item_requires_stream_bridge(item: TextItem) -> bool:
    """Return whether this exact source needs an intercepted file stream.

    Runtime dumps/captures have no guaranteed original archive entry to be
    overridden by a normal root patch. Text produced by an external archive
    extractor is also conservative: its original content filter is unknown to
    the native XP3 writer, so it must not be silently deployed as a plain root
    archive.
    """
    meta = item.meta or {}
    return bool(
        meta.get("runtime_dump")
        or meta.get("runtime_capture")
        or meta.get("from_external_tool")
    )


def changed_items_require_stream_bridge(
    changed: Mapping[str, Iterable[TextItem]] | None,
) -> bool:
    """Check sources selected for this repack, never global archive state."""
    if not changed:
        return False
    return any(
        item_requires_stream_bridge(item)
        for items in changed.values()
        for item in items
    )
