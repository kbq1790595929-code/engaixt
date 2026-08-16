"""KiriKiri/KAG 引擎包：re-export 对外符号并注册引擎。"""

from engines.base import registry

from engines.kirikiri.codec import (
    _apply_koihazi_xp3dec_filter,
    _decode_kirikiri_sjis_tunnel_text,
    _descramble_kirikiri_text,
    _scramble_kirikiri_text,
)
from engines.kirikiri.diagnostics import (
    _diagnose_xp3_archive,
    _diagnosis_to_dict,
)
from engines.kirikiri.dump_targets import (
    _expand_kirikiri_storage_refs,
    export_kirikiri_dump_targets_from_xp3,
    import_kirikiri_external_dump,
    prepare_kirikiri_dump_targets_from_game,
)
from engines.kirikiri.engine import KiriKiriEngine
from engines.kirikiri.garbro import (
    _GarbroEntry,
    _GarbroListResult,
    _garbro_script_entries,
)
from engines.kirikiri.spans import (
    _clean_runtime_capture_text,
    _extract_kirikiri_text_spans,
    _is_kirikiri_runtime_overlay_patch_file,
)
from engines.kirikiri.xp3 import (
    XP3_SIGNATURE,
    _append_xp3_replacement_index,
    _patch_xp3_replacements_in_original_slots,
    _read_xp3_entry_bytes,
    _read_xp3_index,
    _rewrite_xp3_with_replacements,
    _select_script_xp3_files,
    _write_xp3_patch,
)

registry.register(KiriKiriEngine())
