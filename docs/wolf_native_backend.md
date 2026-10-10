# WOLF Native Backend

## Purpose

The WOLF RPG pipeline uses the pinned UberWolf source at
`vendor/uberwolf` (`663dc2defaefc3073cc1178b488894a5aa1d4595`) for archive
operations that the public UberWolf CLI cannot express safely: a caller-known
custom DXArchive key with its matching archive encoder.

The default pipeline remains unchanged for standard WOLF archives. Native
custom-key packing is selected only for a local protected-archive profile that
matches the archive SHA-256 and crypt version and carries the
`clone_runtime_v1` validation level. Unknown WOLF Pro archives stop before AI
translation and before any game resource is changed.

## Components

- `native/wolf_runtime/engaixt_wolf_native.cpp`: narrow command-line adapter.
- `native/wolf_runtime/patches/uberwolf-custom-key-pack.patch`: enables an
  encoder for UberWolf's additional/custom key mode and fixes a non-portable
  wide-path stream constructor for the reproducible build.
- `native/wolf_runtime/build_engaixt_wolf_native.ps1`: copies the submodule to
  a temporary build tree, applies the patch there, and builds the bridge with
  Zig. It never modifies the pinned submodule.
- `engines/wolf/native_bridge.py`: Python boundary around inspect, unpack and
  pack commands.
- `engines/wolf/profile_store.py`: user-scoped DPAPI encrypted profile store.
- `engines/wolf/runtime_keys.py`: stable `CDataBase` runtime-key fingerprint.
The bridge accepts a key file, not a key command-line argument. Raw key
material must never be added to `WolfState`, diagnostics, logs, manifests or
source control.

## Full Pipeline

For standard archives and verified protected profiles, the user-facing flow
remains one action:

`detect -> archive unpack -> structured/CSV extraction -> translation ->
structured patch -> original-key repack -> normal output deployment`

`Script/*.csv` text is extracted from column 9. The CSV patcher changes only
target cell spans, so quoted multiline dialogue, commas, encoding and line
endings are retained. JSON map/common-event/database text remains handled by
the existing WolfRPG bridge.

## Validation Contract

- Native archive `inspect` must report the original crypt version.
- A custom-key round trip must survive `unpack -> patch -> pack -> unpack`.
- Exported `CDataBase` record-key fingerprints must match before and after
  patching and after the native round trip.
- The original game archive is never a native pack target; only the staged
  workspace copy may be replaced.
- The generated archive is parsed again before it can be deployed.
- No native path is selected for multiple protected archives until that case
  has a dedicated regression fixture.

## Protected Archive Boundary

The native archive backend deliberately does not hook or inspect a running
game process. A protected archive is promoted by storing a user-scoped,
DPAPI-encrypted profile only after a disposable clone passes the runtime gate.
The profile store contains no game title or path, and diagnostics expose only
the archive fingerprint and validation level. Profiles that are absent,
unreadable on the current Windows account, or use another crypt version are
treated as unknown and stop safely before translation.
