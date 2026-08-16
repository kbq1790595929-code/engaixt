# Third-Party Notices

This file tracks third-party projects referenced by or distributed with the
translator. It is a compliance checklist, not legal advice.

## Behavioral References

- LunaTranslator / LunaHook
  - Source: https://github.com/HIllya51/LunaTranslator
  - License observed in local checkout `7ac9647`: GPLv3.
  - Usage in this project: behavioral reference for KiriKiri hook coverage and
    filtering. The runtime should remain a clean-room, behavior-equivalent
    implementation. Do not copy LunaTranslator source code or generated engine
    signature tables into distributed builds without GPL review.

## Bundled Or Downloaded Tools

- Source Han Sans / 思源黑体
  - Source: https://github.com/adobe-fonts/source-han-sans
  - License: SIL Open Font License 1.1.
  - Bundled files: `assets/SourceHanSansCN-Regular.otf`,
    `assets/source_han_sans_cn_cjk.fontdata`, and
    `assets/SOURCE_HAN_SANS_LICENSE.txt`.
  - Used as the default redistributable CJK font for Godot, Unity, Ren'Py,
    GameMaker, and runtime overlay paths.
- BepInEx
  - Source: https://github.com/BepInEx/BepInEx
  - Used by Unity/XUnity runtime injection paths.
- XUnity.AutoTranslator
  - Source: https://github.com/bbepis/XUnity.AutoTranslator
  - Used by Unity runtime translation paths.
- GARbro / GARbro Mod
  - Sources: https://github.com/morkt/GARbro and https://github.com/crskycode/GARbro
  - Used as optional visual novel archive extraction helpers.
- KrkrDump / KrkrPatch / KrkrExtract / KirikiriTools
  - Sources:
    - https://github.com/crskycode/KrkrDump
    - https://github.com/crskycode/KrkrPatch
    - https://github.com/xmoezzz/KrkrExtract
    - https://github.com/arcusmaximus/KirikiriTools
  - Used as optional KiriKiri tooling or reference workflow material.
- RPGMakerDecrypter
  - Source: https://github.com/LynxShu/RPGMakerDecrypter
  - License file included under `tools/RPGMakerDecrypter/.../LICENSE` (MIT).
- rpgm-archive-decrypter / rpgmad
  - Source: https://github.com/rpg-maker-translation-tools/rpgm-archive-decrypter
  - License files included under `tools/rpgm-archive-decrypter-src/.../LICENSE.md`
    and `tools/rpgmad-lib-src/.../LICENSE.md` (WTFPL in the local copies).

Before public distribution, collect the exact license text and copyright notices
for every bundled binary, zip, patched DLL, font, and vendored Python package.
