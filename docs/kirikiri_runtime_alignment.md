# KiriKiri Runtime Alignment Matrix

Engine layout reference: KRKRZ `fd5c4baa6a2ef5978db1bd043634351f48667daf` (`tjs2/tjsVariantString.h`)

Alignment method: the game binary is the only oracle. Hook sites are located by
signature and verified against captured real-game behaviour; the matrix below is
the contract for that behaviour, not a description of another tool's internals.

This document records the remaining KRKR runtime reference branches. The normal
translation path is static patching; runtime hook code is diagnostic/experimental
and must not be selected by ordinary static output.

## Default Runtime Policy

- Default route: static script/XP3 patching loaded by the game's own resource
  lookup; native realtime capture and the second overlay window are disabled.
- Default VM behavior: capture-only, `vm_text_replace=0`.
- `EmbedKrkrZ` beta behavior: the single-codepoint classifier keeps its
  ECX map replacement, while the framed whole-string converter is
  hooked at function entry. Exact map hits are converted directly into the
  caller's UTF-16 destination and return the converted length; misses execute
  the original converter unchanged. This verified converter bypass is the
  required baseline when `embed_text_replace=1`.
- `EmbedKrkrZ` also contains a frameless single-codepoint decoder whose ECX
  contract is `const char **`, not `const char *`. The active dialogue
  typewriter and automatic wrapping path use this function, while history/save
  views use the whole-string converter. On a map hit the cursor hook updates
  `*cursor` to the stable translated UTF-8 buffer and lets the original decoder
  advance it character by character. Treating ECX itself as the string pointer
  only affects the whole-string path and leaves wrapped dialogue untranslated.
- A first-seen embedded line waits up to 3 seconds for the overlay worker
  to append its live translation. A timeout releases the original text and the
  same source is not blocked again. This bound applies to UTF-8 and UTF-16
  embed branches so a slow local translator cannot freeze the game thread.
- Embed diagnostics group each mapped conversion by source hash, hook RVA,
  caller RVA, and measure/write destination mode. This distinguishes capture
  success from propagation into the active dialogue renderer.
- The generated launcher starts the selected game executable directly after
  static patch output is verified. It does not inject a DLL or start a second
  translation window.

## Protected Static Patch Bridge

Behavior reference: the upstream KrkrPatch stream format (`KrkrPatch/KrkrPatcher.cpp` and
`KrkrPatch/KrkrPatchStream.cpp`). The upstream repository did not expose a
clear license when inspected, so this project uses an independent behavioral
implementation and does not copy its source.

- `patchstream` installs `TVPCreateStream` before signaling launcher readiness.
- Module hooks are opt-in through `KIRIKIRI_CAPTURE_HOOKS`: `kag`,
  `textrender`, and `psb` are never installed by an `embed`-only profile.
  This prevents a weak module signature from changing menu rendering while a
  different capture branch is under test.
- A static output may use the filter-preserving XP3 writer or an explicitly
  prepared archive bridge when the source format requires it. This bridge is
  still a static resource-loading path; it must never turn into a native text
  hook or a second-window fallback.
- Requests in `file://./data.xp3>entry`, `archive://./entry`, and
  `arc://./entry` form are normalized to manifest entries, including flat
  basename lookup.
- Loose patch files are returned through our own read-only MSVC x86
  `tTJSBinaryStream` ABI bridge. This prevents the game's content filter from
  processing already-decoded TJS and PSB data again.
- When both loose files and a packed patch exist, loose files take priority.
  Packed XP3 stays a fallback until its segment reader has equivalent coverage.
- The stream ABI is isolated in `kirikiri_patch_stream.cpp`: `Seek`, `Read`,
  `Write`, `SetEndOfStorage`, and `GetSize` use cdecl with `this` on the stack;
  the deleting destructor uses the MSVC x86 ECX/stack-flag convention.
- Signature requests can return the five-byte `skip!` stream while the patch
  bridge is active. The runtime records `sig_bypass` separately.
- Startup diagnostics record the first 120 raw storage requests, normalized
  keys, manifest candidates, and match result.
- This bridge is beta and x86-only. It remains opt-in until multiple protected
  KRKR samples pass continuous dialogue, choice, save/load, and uninstall
  regression tests.

## Branch Matrix

| Engine branch | Hook site | Match strategy | Text read | Role handling | Our status | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| `KAGParser` | legacy KAG parser text buffer | `cmp word ptr [eax+ecx*2], 0x5b` near `[r]` | UTF-16 buffer from `eax` | speaker + text | implemented embed beta | Installed as `KAGParser text buffer`; `name=` updates speaker diagnostics, `word=`/visible body emits text. With explicit embedded display enabled, exact mapped dialogue may replace the KAG buffer before cursor parsing without enabling global VM replacement. |
| `KAGParserEx` / `ExtKAGParser` | external KAG parser module | module scan plus `tTJSString::operator +` marker | UTF-16 `tTJSString` arg2 or text buffer | speaker + text | implemented embed beta | Name commands update speaker; `word/text` attributes become text only. The internal text-buffer branch shares the scoped KAG display replacement; entry-level string hooks remain capture-only. |
| `TextRender` | `textrender.dll` render path | `textrender.dll` / `V2Link`; first match the `tTJSVariant::GetString()` resolver sequence, then fall back to the switch-table signature inside `TextRenderBase::render` | UTF-16 from `eax` after `GetString`; fallback stack arg1 | text | implemented embed beta | The primary stub replaces the `GetString()` return pointer in EAX and replays its original zero/nonzero control flow, matching the engine's write-after-render timing. The older stack-argument entry hook is retained only as a fallback. This distinction matters because generic UTF-8 or intermediate-buffer replacement can update save/backlog summaries without changing the active dialogue renderer. |
| PSB string bridge | PSB plugin resolver | `psbfile.dll` resolver marker for `TVPUtf8ToWideCharString`, followed by its conversion-wrapper return sequence | UTF-8 source in `ebx`, completed UTF-16 destination in `edi` | text | implemented diagnostic-only | Records whether a mapped translation reached the completed UTF-16 buffer. It does not mutate either buffer: this game proved that post-conversion replacement can miss an earlier layout cache, while source mutation can corrupt script state. Static embedding uses the stream patch bridge instead. |
| `KiriKiriZ1/Z2` | Z1/Z2 glyph capture | GetGlyphOutline/GetTextExtent caller signatures plus Z2 indirect text pointer | UTF-16 string | text | implemented display hook | The default `display` profile enables the inline Z3/Z2/WCS candidates as well as the capture set, so an engine that does not expose KAGParser still reaches its active renderer. |
| `KiriKiriZX` | ZX dispatch table | byte pattern around dispatch table, enclosing function | stack arg as `tTJSString` | text | implemented | Installed by default through `zx`; source label `tjs_string`. |
| `EmbedKrkrZ` | scenario text classifier | UTF-8 classifier sequence after `mov reg, ecx`, then function-shape classification | UTF-8 string from `ecx` | text | implemented embed beta | Two hooks are expected. The classifier hook captures eligible text; the framed `mov esi,ecx` converter is hooked at its enclosing function entry. A mapped hit writes translated UTF-16 directly to the supplied destination and bypasses the original conversion. Removing this converter bypass and forcing all candidates through ECX pointer replacement is a known regression: title/menu construction stalls or renders without choices. |
| `EmbedKrkr2` | pre-hook/post-hook pair | `mov ax,[esi]`; checks `;` and `*` | UTF-16 string from `esi` | speaker + text | implemented embed beta | The active parser reloads `ESI` from `[ebx+0x64] + [ebx+0x74]*8` during its per-character loop. The dedicated stub therefore replaces both the saved `ESI` register and that exact pointer slot with a stable mapped UTF-16 string. It never guesses buffer capacity or writes beyond the original allocation. Slot writes require the slot to still contain the captured source pointer; failures leave the original text untouched and are reported by `embed_wide_slot_write_fail`. Tagged/command text remains capture-only. |
| `Krkr2wcs` | wide-string helper | `wcscpy` or `wcslen` prologue signatures | UTF-16 stack arg | text | implemented display hook | Enabled by the default `display` profile together with the Luna-compatible experimental inline candidates. |
| GDI caller fallback | GDI/GDI+ draw-text imports | GDI/GDI+ import hooks | A/W draw text | text | implemented fallback | Used for display fallback and font compatibility, not primary KRKR logic. |

## Capture Schema

Runtime misses are appended to `_translation_meta/kirikiri_runtime_capture.jsonl`.

Each new record must keep the legacy fields and add role-aware fields:

```json
{
  "source": "embed_krkrz_utf8",
  "text": "visible text",
  "role": "text",
  "hook_name": "embed_krkrz_utf8",
  "visible_text": "visible text",
  "raw_text": "raw engine text"
}
```

`role=speaker` records are diagnostic only. The translation pipeline must only translate missing or absent role values that mean `text`.

## Realtime Process Lifecycle

- The static entry remains separate and does not use these runtime components.
- Realtime preserves the selected entry EXE, but the native launcher follows its
  descendant tree. Only x86 KRKR engine executables in the same game directory
  are eligible for child injection; unrelated helper processes are excluded.
- `_translation_meta/kirikiri_runtime_target.tsv` publishes the actual PID and
  path atomically. The subtitle window follows this target, including entry-to-
  child handoff, rather than treating the entry EXE's exit as game termination.
- `kirikiri_native_launcher.log` records process detection, injection/readiness
  failures with Windows error codes, target changes, and individual exit codes.
  Hook records carry their PID so entry signature misses cannot be confused with
  successful capture in the real engine process.
- GUI diagnostics consume only the new session's complete log lines. They
  distinguish DLL readiness, signature mismatch, text capture, translation
  failure, and displayed subtitles. No-capture exit is not reported as proof
  of successful realtime translation.
- Validation sample: Nemophilia's small `.text` entry creates a full KRKRZ
  child. Existing KRKRZ/UTF-8 hooks capture its dialogue after child injection;
  no title-specific signature or filename rule is needed.

## Runtime Text Provenance

- The general UTF-8 conversion hook sees object/layer names as well as text.
  Bare strings are deferred unless another text/render hook confirms them;
  dialogue punctuation or script whitespace provides a fallback signal.
  Short reactions, sound effects and pure-kanji labels remain eligible on
  wide/KAG/render paths. No per-game string blacklist is used.
- Punctuation-only dialogue is displayed unchanged, never translated, cached
  as a generated translation, or counted as successful Chinese output.
- Delayed translation results still update history/cache, but do not replace
  newer game dialogue, including an ellipsis-only line.
- Existing polluted resource-name cache entries cannot bypass native source
  eligibility. Existing generated punctuation entries are ignored on load.

## Static Output Verification

- Ordinary static output is verified from the generated `patch.xp3`, the
  metadata bridge archive, or the manifest-controlled loose patch directory.
- When a protected source XP3 is rebuilt in place, the rebuilt archive and
  the `rebuilt_files_by_archive` entries are reopened and checked as well.
- A present but unreadable active XP3 is a hard failure even if another patch
  happens to contain a matching translation; this prevents a damaged shadow
  archive from taking precedence at game startup.
- UTF-8/UTF-16/CP932 payloads, the SJIS tunnel table, and ASCII placeholder
  mappings are all accepted as distinct validated output forms.

## Acceptance Rules

- If a statically extracted and translated KRKR item is absent from the final
  patch payload, classify it as a repack/output defect before investigating
  runtime hooks.
- Fixes must update this matrix, native diagnostics, and at least one source or behavior test.
- Real games are validation samples only; no per-title KRKR hook special cases.

## License Boundary

- Captured behaviour is used as a reference; no third-party hook source is copied into this runtime. The KiriKiri runtime here is an independent implementation.
- KRKRZ's public headers are used to verify engine data layout and calling
  assumptions; our runtime remains an independent implementation.
