# KRKR optional static components

This directory contains the KRKR sidecars used by the current private build.
The bundled files are copied from the managed local tool installation and are
described in `components.json` with their source, license, version, and hash.

Bundled components:

- `garbro/`: GARbro Mod 1.0.2.2 Console and its managed dependencies
- `kirikiri_tools/Xp3Pack.exe`: KirikiriTools 1.7 XP3 packer
- `kirikiri_tools/version.dll`: KirikiriTools 1.7 unencrypted XP3 bridge

Optional components that are not currently bundled:

- VNTextPatch/VNTextProxy: KS/SCN export/import and SJIS tunnel support (MIT)
- msg-tool: second archive/script adapter (GPL-3.0, separate sidecar)

EngAixt discovers these files at runtime and runs each command in an isolated
workspace. Missing components are reported in the KRKR preflight; they are
never downloaded during detection or translation.
