# Third-Party Notices

EngAixt is distributed under the **GNU General Public License v3.0** (see
`LICENSE`). This file records the third-party projects it uses and under what
terms. It is a compliance record, not legal advice.

Two conventions are used below:

- **Sidecar** — an independent external program invoked as a separate process.
  Sidecars are not linked into EngAixt and keep their own license.
- **Vendored** — source copied into this repository and compiled into an
  EngAixt binary. Vendored code keeps its upstream license.

## Behavioral references

- **LunaTranslator / LunaHook** — <https://github.com/HIllya51/LunaTranslator>
  - License: **GPLv3** (observed in local checkout `7ac9647`).
  - Used as a behavioral reference for KiriKiri hook coverage and filtering.
  - EngAixt's runtime hooks are independently written, behavior-equivalent
    implementations. No LunaTranslator source code is copied into this
    repository or into distributed builds.
  - This is one reason EngAixt itself is GPLv3: it keeps the licensing position
    unambiguous with respect to the reference implementation.

## Vendored source compiled into EngAixt binaries

These live under `vendor/` and are built by the scripts in `native/`.

- **UberWolf** — <https://github.com/Sinflower/UberWolf>
  - Pinned at `663dc2defaefc3073cc1178b488894a5aa1d4595`.
  - License: **MIT**. Full text: `assets/wolf_runtime/UBERWOLF-LICENSE.txt`.
  - `native/wolf_runtime/build_engaixt_wolf_native.ps1` compiles it into
    `assets/wolf_runtime/engaixt_wolf_native.exe`, applying
    `native/wolf_runtime/patches/uberwolf-custom-key-pack.patch` to enable the
    archive encoder for caller-supplied custom keys.
  - `native/wolf_runtime/build_wolf_text_bridge.ps1` compiles it into
    `assets/wolf_runtime/wolf_text_bridge.exe`.
- **DX Library (DXArchive component)** — <https://dxlib.xsrv.jp/>
  - Copyright **Takumi Yamada**. Retained in `vendor/uberwolf/3rdParty/DXLib/`
    exactly as UberWolf publishes it; used for DXArchive read and write.
  - EngAixt consumes this code only through UberWolf's published source tree.
    No game code, game keys, or game data are bundled.
  - Note: upstream UberWolf ships this subtree without a license file. The
    copyright holder permits free use and redistribution of DX Library; the
    stated restriction is that DX Library itself may not be sold as-is.
    EngAixt is a separate program that compiles a subset of these sources, not
    a redistribution of DX Library — but if you intend to redistribute EngAixt
    commercially, verify the current DX Library terms at the link above.
- **CLI11** — <https://github.com/CLI11/CLI11> — License: **BSD-3-Clause**.
- **nlohmann/json** — <https://github.com/nlohmann/json> — License: **MIT**.
- **LZ4** — <https://github.com/lz4/lz4> — License: **BSD-2-Clause**.
- **LLVM libc++, libc++abi, libunwind** — <https://llvm.org/> — License:
  **Apache-2.0 WITH LLVM-exception**. Texts: `assets/wolf_runtime/LLVM-*.txt`.

## Sidecars (independent external programs)

Assembled at packaging time and downloaded on demand at runtime by
`core.tool_manager`. The source tree does not vendor these binaries.

| Component | License | Source |
| --- | --- | --- |
| GARbro / GARbro Mod | MIT | <https://github.com/morkt/GARbro>, <https://github.com/crskycode/GARbro> |
| KirikiriTools | MIT | <https://github.com/arcusmaximus/KirikiriTools> |
| VNTextPatch / VNTextProxy | MIT | <https://github.com/arcusmaximus/VNTranslationTools> |
| KrkrDump | see upstream | <https://github.com/crskycode/KrkrDump> |
| KrkrPatch | see upstream | <https://github.com/crskycode/KrkrPatch> |
| KrkrExtract | see upstream | <https://github.com/xmoezzz/KrkrExtract> |
| msg-tool | **GPL-3.0** | <https://github.com/lifegpc/msg-tool> |
| RPGMakerDecrypter | MIT | <https://github.com/LynxShu/RPGMakerDecrypter> |
| rpgm-archive-decrypter / rpgmad | WTFPL (local copies) | <https://github.com/rpg-maker-translation-tools/rpgm-archive-decrypter> |
| 7-Zip | LGPL / BSD-3-Clause (unRAR restriction) | <https://github.com/ip7z/7zip> |

**msg-tool is GPL-3.0 and is used strictly as a sidecar** — invoked as a
separate process, never linked into EngAixt. Its license text ships next to the
release package in `_internal/licenses/`.

License texts for the bundled KiriKiri sidecars are kept in
`_internal/licenses/` (`GARbro-LICENSE.txt`, `KirikiriTools-LICENSE.txt`,
`KRKR-THIRD-PARTY-NOTICES.md`), which `game-translator.spec` copies into the
distribution.

## Runtime components

- **BepInEx** — <https://github.com/BepInEx/BepInEx> — used by the Unity /
  XUnity runtime injection paths. BepInEx is LGPL-2.1; see the upstream LICENSE
  for the authoritative text. Patched assemblies under `assets/bepinex_patches/`
  retain BepInEx's notices.
- **XUnity.AutoTranslator** — <https://github.com/bbepis/XUnity.AutoTranslator>
  — Copyright (c) 2018 Bepis. License: **MIT**. EngAixt's Unity 6000 fallback
  plugin implements equivalent asynchronous AssetBundle loading behavior
  without copying XUnity source code.

## Fonts

- **Source Han Sans / 思源黑体** — <https://github.com/adobe-fonts/source-han-sans>
  - License: **SIL Open Font License 1.1**.
  - Bundled: `assets/SourceHanSansCN-Regular.otf`,
    `assets/source_han_sans_cn_cjk.fontdata`, `assets/SOURCE_HAN_SANS_LICENSE.txt`.
  - Default redistributable CJK font for Godot, Unity, Ren'Py, GameMaker and
    runtime overlay paths.
- **Noto Sans CJK** — Copyright 2014-2026 Adobe (<http://www.adobe.com/>)
  - License: **SIL Open Font License 1.1**.
  - `assets/xunity_fonts/` — the bundled TMP font asset is derived from Noto
    Sans CJK Simplified Chinese. Text: `assets/xunity_fonts/NOTO_SANS_CJK_OFL.txt`.

## Not included

No game resources, extracted game text, translation databases, model weights,
or per-game keys are distributed with this repository. Datasets derived from
local game caches are never published — see `docs/lora_dataset.md`.

## Maintenance note

This file was audited before the repository was made public: every bundled
binary, font and vendored subtree listed above has its license text in the tree
or in the release package. When adding a new third-party component, record it
here **and** ship its license text alongside it.
