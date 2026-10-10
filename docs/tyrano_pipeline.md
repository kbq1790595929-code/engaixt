# TyranoScript Pipeline

## Status

TyranoScript/TyranoBuilder support is experimental. It has completed one real
end-to-end sample, including DeepSeek translation, embedded archive patching,
ZIP verification, and game startup. More independent samples are required
before promoting the engine to beta.

## Detection

The detector recognizes either:

- a loose Tyrano project containing `data/scenario`, `index.html`,
  `package.json`, and `tyrano/plugins/kag/kag.tag_system.js`; or
- an NW.js executable with an appended ZIP package containing the same markers.

The embedded detector reads ZIP directory metadata only. It does not decompress
the executable during the detect stage. Tyrano is registered before KiriKiri so
the broad `.ks` fallback cannot route Tyrano games into the KRKR runtime path.

## Pipeline Contract

1. Extract visible text spans from `data/scenario/**/*.ks`.
2. Skip labels, comments, JavaScript blocks, resource paths, internal character
   IDs, and dynamic expressions such as `&tf.maney_buy+'円'`.
3. Translate through the shared translator, cache, quota, validation, and cost
   pipeline. The Tyrano engine has no provider-specific translation logic.
4. Replace only the exact source spans. KAG tags such as `[p]` and `[r]` remain
   byte-for-byte outside translated spans.
5. For loose projects, copy the patched scenarios back through the standard
   manifest path. For embedded NW.js packages, replace only changed ZIP entries
   and preserve the native executable prefix and all untouched assets.
6. Reopen every patched scenario entry and verify that every effective
   translation is present before reporting success.

## Source Selection

Current `.ks` content is preferred when its visible spans are Japanese. A
same-name `.ks.bak` is used only when the current visible text is no longer
Japanese and the backup is. This permits recovery from an existing third-party
translation without replacing a valid current script with a stale developer
backup.

## Recovery

The standard game manifest backs up loose scripts or the complete embedded
executable before repack. The normal "uninstall translation" action restores
that centralized backup. A failed embedded rewrite remains in an
`.engaixt_tmp` file and is never promoted over the game executable unless ZIP
round-trip verification succeeds.
