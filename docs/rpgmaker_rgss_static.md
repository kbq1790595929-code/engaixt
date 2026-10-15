# RPG Maker RGSS Static Route

## Supported Standard Inputs

- XP: loose `Data/*.rxdata` or RGSSAD v1 `.rgssad`.
- VX: loose `Data/*.rvdata` or RGSSAD v1 `.rgss2a`.
- VX Ace: loose `Data/*.rvdata2` or RGSSAD v3 `.rgss3a`.
- XP/mkxp games with existing PO localization keep the PO/LOC route.

MV/MZ scanning and display hooks are separate. A directory named `Data` is not
evidence of MV/MZ, especially on a case-insensitive Windows filesystem.

Archive format reference:
`RPG-Maker-Translation-Tools/rpgm-archive-decrypter-lib`, `src/lib.rs`.
The v1 filename/table key advances independently of each payload's key.
Rebuilt archives retain version, entry order, raw filename bytes and, for v3,
the original base key and per-entry keys.

## Stage Contract

1. Extract only into the workspace. Never start MV/MZ scanning for RGSS.
2. Validate a no-op Marshal roundtrip before accepting text.
3. Parse `Data/Scripts.*` as a script table. Decompress each script's zlib Ruby
   source and extract Ruby string literals. For simple `#{expression}`
   interpolation, surrounding text is translated through exact content spans
   and the expression stays unchanged. Compressed bytes, comments and `require`
   paths are excluded.
4. Restore route information when patching a checkpoint with a fresh engine.
5. Require current game data to match the extraction source.
6. Serialize and reparse all modified data before deployment.
7. For archives, read the rebuilt output again and compare every entry,
   including unchanged binary resources.
8. Stage outputs and establish manifest backups before replacing game files.
9. Record actual deployed game files, not just workspace files.
10. Uninstall restores the original bytes from the manifest backup.

Direct engine calls without a manifest retain `.bak` backups. Shared pipeline
calls use the existing centralized manifest and rollback implementation.

Multiple RGSS archives, nonstandard archive versions, path traversal, filename
aliases and unsupported Marshal structures fail explicitly. No fallback into
MV/MZ and no proprietary MTool implementation are involved.

## Validation

`tests/test_rpgmaker_rgss_static.py` covers loose formats, independent v1
fixtures, v3 fixtures produced by the existing archive writer, checkpoint
resumption, archive/resource preservation, PO catalogs, failure boundaries and
byte-for-byte uninstall restoration.

The optional public archive fixtures in `.codex_runs/mtool-audit` additionally
exercise real VX/VX Ace script tables. They are audit inputs and are not
distributed with the application. These tests do not claim that the native
RGSS runtime, custom Ruby syntax, or every event-command extension matches
MTool; those require an independent same-input comparator and actual game
display testing before a release is accepted.

`tools/rgss_compare` supplies a command-line interface to the user's installed
MTool for that comparison. It executes MTool's original archive, CLoader and
RGSSWorker functions, saves raw outputs and hashes, and independently compares
EngAixt's results. See its README for setup, exact selectors and adapter limits.
Public VX/VX Ace fixtures passed the per-file Japanese data sets, event message
sets, per-script Japanese sets, occurrence counts, and all archive payload
hashes. This is a measured sample result, not complete engine-family parity.
