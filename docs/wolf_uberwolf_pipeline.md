# WOLF RPG / UberWolf Pipeline

## Scope

WOLF RPG support is a beta static pipeline. It uses the MIT-licensed
[Sinflower/UberWolf](https://github.com/Sinflower/UberWolf) project for archive
handling and structured WOLF data parsing.

- Bundled archive CLI: UberWolf `v0.6.3`
- Structured parser source: `Sinflower/UberWolf@663dc2defaefc3073cc1178b488894a5aa1d4595`
- Supported data: `Game.dat`, `CommonEvent.dat`, project databases, and `.mps` maps
- Supported archives: extensions accepted by UberWolf, including `.wolf`
- Runtime translation: not used

All distributed third-party license texts live in `assets/wolf_runtime/`.

## Stage Contract

1. `detect`
   - Reads root-level names and directory structure only.
   - Never opens large executables, unpacks archives, or invokes tools.
2. `extract`
   - Copies archive inputs to the job workspace.
   - Runs `UberWolfCli` only against that copy.
   - If UberWolf cannot open one protected archive, selects the native backend
     only when a matching DPAPI-encrypted, runtime-validated profile exists.
   - Runs `wolf_text_bridge export` to produce structured JSON.
   - Converts visible JSON fields into standard `TextItem` records.
   - Never invokes AI or writes to the game directory.
3. `translate`
   - Uses the shared translator/cache/cost pipeline without WOLF-specific API logic.
4. `repack`
   - Applies translations to exported JSON using exact JSON pointers.
   - Rejects a patch if the source field changed after extraction.
   - Protects database record names and mirrored string aliases used by common
     events as runtime lookup keys, including checkpoints from older versions.
   - Serializes to a fresh output directory and parses it again before packaging.
   - Repackages standard workspace directories with `UberWolfCli`.
   - Unpacks every generated standard archive again, parses the unpacked data,
     and verifies the complete runtime-key fingerprint before deployment.
   - Native Pro output is unpacked again, structurally parsed, and checked
     against the original `CDataBase` runtime-key fingerprint.
5. `copy_back`
   - Uses the normal manifest backup and copy-back path.
   - Loose games copy only changed `.dat/.mps` files.
   - Archived games copy only archives that contain effective translations.

## Text Coverage

The beta extractor includes:

- Message and choice commands
- String conditions and `SetString` commands
- Picture commands only when their mode is text, never image paths
- Game title/startup messages
- Normal database record names and visible string values that pass the shared
  text filter

Database record names and string fields that exactly mirror those names are
runtime lookup keys and are never translated.

Comments, resource paths, font settings, internal event names, and metadata are
not translated. New command coverage must be added with a fixture proving that
the field is player-visible and safe to rewrite.

## Diagnostics

The main run report records `engine_stage_diagnostics`, including:

- archive list and selected pack mode
- UberWolf path, release identity, and SHA-256
- bridge source commit and SHA-256
- every external command, exit code, duration, stdout, and stderr
- JSON file count and extracted text count

Repack verification records the generated outputs and confirms that the
serialized WOLF data can be parsed again. A failure should be classified first
as archive unpack, structured export, JSON patch, structured serialization, or
archive repack before changing extraction logic.

## Release Gate

The synthetic contract tests are in `tests/test_wolf_engine.py`. A release that
changes WOLF, manifest handling, copy-back, uninstall, or tool management must
also run a real archived-game smoke test:

1. Extract an UberWolf-supported archive.
2. Replace one known visible message with CJK text.
3. Repack the archive.
4. Unpack the generated archive again.
5. Export JSON and confirm the exact translated message is present.
6. Launch the game and verify text display and uninstall restoration manually.
