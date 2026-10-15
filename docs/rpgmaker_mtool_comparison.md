# MTool RGSS Differential Test Record

Date: 2026-10-15. Application source baseline: `2a5f103` plus the uncommitted
RGSS changes. This record concerns extraction, not translated game display.

## Executed Oracle

`python -m tools.rgss_compare` runs the installed MTool NW.js runtime with its
own test profile, hides its window, and calls original MTool code over loopback
CDP. It requires no GUI input or game process. Literal Chromium `--headless`
was tested but only loaded an empty browser, so it is not used by the interface.

The following original functions were executed:

- `RGSSAD_MTool.decrypt/read_data` for archive enumeration and payloads.
- `CLoader.getAllStrings` for ordinary data.
- `CLoader.extract_script_data` for the compressed script table.
- `CLoader.eventCommandListProcObj` for message command export.
- `RGSSWorker.getStringFromRubyScript` for every decompressed script.

The independent Ruby host used RubyInstaller 4.0.7-1 x64. The downloaded
official archive SHA-256 was
`7a9be96d3448ecbd9a81eccae1f619131dfcc761f2c98dded03ece6c05d0474f`.
MTool's NW.js version was 0.67.1, Node 18.7.0, Chromium 104.0.5112.80.

MTool loader SHA-256:
`b819389c4396ca5d9f277fe64a38b056c88014b640cb24e8aca5ec7b7665a78b`.
Original CLoader resource SHA-256:
`0003488629c9fdc370f1fa6ea9ad44b17f40182517ae55838f2dc07a7829e59c`.
Original RGSSAD resource SHA-256:
`cf3150835edb170ac3a5a3e35cd0ca9f74fe549c299867a1fb218f27b0698997`.

## Same Inputs

Both files came from the public
`RPG-Maker-Translation-Tools/rpgm-archive-decrypter-lib` repository. They are
real serialized compatibility fixtures, not MTool's own tests or representative
commercial game coverage.

| Input | Input SHA-256 |
| --- | --- |
| `Game.rgss2a` | `1c680d186f88848aab66f26e76a89427659aa39dc943a245c984acd2df552318` |
| `Game.rgss3a` | `64f099d30ec3df9e7af19f08fae9dd894fec29740150507a15e4b4b6ee97b4c0` |

| Comparison | VX | VX Ace |
| --- | ---: | ---: |
| Archive entries with identical order/name/payload hashes | 17/17 | 17/17 |
| Ordinary Japanese data keys, summed per file | 608 | 1 |
| Decompressed script blocks sent to MTool's worker | 95 | 126 |
| Unique `(script index, Japanese text)` keys | 49 | 4 |
| Japanese script occurrences | 53 | 6 |
| Visible message keys, summed per event-containing file | 16 | 12 |
| Missing/extra keys after the correction | 0/0 | 0/0 |
| Script occurrence counts equal | yes | yes |

Data comparison uses the same `utils.rvdata2.is_translatable` Japanese
predicate on both ordinary text sets. Script comparison uses
`is_ruby_translatable` on both outputs and also checks occurrence counts.
Message command comparison uses the common visible-text predicate, including
the English fixture messages. Event key totals are per-file unique counts,
not a global unique count or the number of all command occurrences.

## Difference Fixed

The first actual MTool execution found two missing VX Main-script fragments:
the text before and after `#{filename}` in an interpolated error message.
EngAixt previously skipped the whole interpolated string. It now exports exact
content spans around the expression and preserves the Ruby expression during
rewrite. The final script key sets and occurrence counts match MTool for both
fixtures. Tests include an independent Ruby Marshal load, zlib decompression,
and Ripper syntax parse of the rewritten script.

Final full regression: **877 passed, 9 skipped**, with one existing UnityPy
deprecation warning. The independent Ruby parser test ran in this result.

## Reproduction And Limits

Run `tools/rgss_compare/setup.ps1`, then follow the command in
`tools/rgss_compare/README.md`. A successful run produces a `summary.json`,
per-input raw MTool exports and comparison files, `provenance.json`, startup
logs, and `mtool-integrity.json`. Exit code 2 reports differences.

The verified local run is in ignored
`.codex_runs/rgss-compare/20261014T230644693099Z`. The directory name uses UTC;
the local date was 2026-10-15. Its integrity record confirms that MTool's
package, executable and loaders stayed byte-identical. Test processes exited.

The Ruby host supplies data-only RPG classes and opaque Table/Color/Tone
primitives, and replaces only the window-evaluation bridge with a no-op.
MTool extraction function bodies remain unchanged. Game scripts are parsed but
never executed. This is not native Ruby 1.8/1.9 or RGSS runtime validation.

This result does not establish full MTool GUI filtering equivalence, font
rendering, cloud translation, every event extension, custom Ruby syntax,
commercial game compatibility, or a percentage success rate. No independent
real XP fixture was compared in this run. Actual game display acceptance
remains a separate release requirement.

Original MTool resources, raw fixture source and binary inputs stay local under
ignored `.codex_runs` and are excluded from source exports and releases.
