# Third-Party Notices

EngAixt is distributed under the **GNU General Public License v3.0**. See
[`LICENSE`](LICENSE). This file records the third-party projects used by the
public source tree and its runtime assets.

## Behavioral references

The KiriKiri runtime hooks are written against the engine's own calling
conventions. No third-party hook implementation is copied into this repository.

## Runtime components

- **BepInEx** — <https://github.com/BepInEx/BepInEx> — used by Unity runtime
  injection paths. Patched assemblies under `assets/bepinex_patches/` retain
  their upstream notices.
- **XUnity.AutoTranslator** — <https://github.com/bbepis/XUnity.AutoTranslator>
  — MIT. EngAixt's Unity fallback plugin is implemented independently.
- **UberWolf** — <https://github.com/Sinflower/UberWolf> — MIT. The public tree
  contains the runtime executable and license notice used by EngAixt; the
  complete upstream build checkout is maintained separately.

## Sidecars

The installation package for this application already includes the following tools.:

- GARbro / GARbro Mod — MIT
- KirikiriTools — MIT
- VNTextPatch / VNTextProxy — MIT
- msg-tool — GPL-3.0
- RPGMakerDecrypter — MIT
- rpgm-archive-decrypter / rpgmad — WTFPL

Source URLs and the detailed KiriKiri notices are recorded under
`_internal/licenses/`.

## Fonts

- **Source Han Sans / 思源黑体** — SIL Open Font License 1.1. See
  `assets/SOURCE_HAN_SANS_LICENSE.txt`.
- **Noto Sans CJK** — SIL Open Font License 1.1. See
  `assets/xunity_fonts/NOTO_SANS_CJK_OFL.txt`.

## Not included

No game resources, extracted game text, translation databases, model weights,
per-game keys, API keys, or user runtime data are distributed with this
repository.
