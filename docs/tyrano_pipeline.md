# TyranoScript Pipeline

## Status

TyranoScript/TyranoBuilder support is beta. Multiple real games have completed
the translation pipeline successfully. Compatibility across different package
formats, script structures, and runtime environments is still being verified;
more independent startup and visible-text checks are needed before stable.

## Detection

The detector recognizes either:

- a loose Tyrano project containing `data/scenario`, `index.html`,
  `package.json`, and `tyrano/plugins/kag/kag.tag_system.js`, either at the
  game root or inside a `package.nw` directory;
- a `package.nw` ZIP archive with the same markers; or
- an NW.js executable with an appended ZIP package containing the same markers;
  or
- an Electron `resources/app.asar` archive containing the same markers.

The embedded detector reads ZIP or ASAR directory metadata only. It does not
extract scenario files during the detect stage. Tyrano is registered before
KiriKiri so the broad `.ks` fallback cannot route Tyrano games into the KRKR
runtime path.

## Pipeline Contract

1. Extract visible text spans from `data/scenario/**/*.ks`, including visible
   attributes in both `[tag]` and `@tag` commands.
2. Skip labels, comments, JavaScript blocks, resource paths, internal character
   IDs, and dynamic expressions such as `&tf.maney_buy+'円'`.
3. Translate through the shared translator, cache, quota, validation, and cost
   pipeline. The Tyrano engine has no provider-specific translation logic.
4. Replace only the exact source spans. KAG tags such as `[p]` and `[r]` remain
   byte-for-byte outside translated spans.
5. Stage patched files separately from extracted originals. For embedded NW.js
   packages, replace only changed ZIP entries and preserve the executable
   prefix and untouched assets. For Electron ASAR, append changed scenario data,
   update its file offsets and integrity hashes, and preserve untouched payloads.
6. Reopen every patched scenario entry and verify that every effective
   translation is present before reporting success.

Static display names in `#name[:face]` lines are translated as speaker spans;
the optional `:face` suffix is preserved. `chara_new`'s `name`/`jname`
attributes remain unchanged because Tyrano uses them as character references.
Dynamic speaker expressions and custom JavaScript-generated text are not
extracted.

## Source Selection

Current `.ks` content is preferred when its visible spans are Japanese. A
same-name `.ks.bak` is used only when the current visible text is no longer
Japanese and the backup is. This permits recovery from an existing third-party
translation without replacing a valid current script with a stale developer
backup.

## Recovery

The standard game manifest backs up the affected scripts or archive. The
normal "uninstall translation" action restores that centralized backup. Output
is staged before deployment, and each target has a snapshot of its state just
before this run. Deployment verifies written bytes and restores those snapshots
if a replacement or verification fails.
