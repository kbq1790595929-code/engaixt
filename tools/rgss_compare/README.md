# Local MTool RGSS Comparator

This optional developer interface runs the user's installed MTool functions
from a command line. It needs no game window, GUI clicks, game execution,
translation account, or cloud translation. The NW.js application itself runs
with a separate profile and is hidden after initialization. NW.js 0.67.1's
literal `--headless` mode does not load its application package, so the harness
uses its normal application runtime behind a command-line API.

## Setup

From the repository root, run:

```powershell
& tools/rgss_compare/setup.ps1
```

The setup installs `websocket-client` and a portable Ruby in the user's
`.cache/engaixt-analysis/ruby` directory. It downloads the official RubyInstaller
4.0.7-1 x64 archive and verifies the pinned SHA-256. It requires 7-Zip, changes
no system PATH, and does not install or redistribute MTool.

```powershell
python -X utf8 -m tools.rgss_compare `
  --mtool 'C:\baidunetdiskdownload\工具\MTool\Tool' `
  --ruby "$env:USERPROFILE\.cache\engaixt-analysis\ruby\rubyinstaller-4.0.7-1-x64\bin\ruby.exe" `
  --archive '.codex_runs/mtool-audit/rgss-public-fixtures/Game.rgss2a' `
  --archive '.codex_runs/mtool-audit/rgss-public-fixtures/Game.rgss3a'
```

Use a trusted local standard RGSSAD v1/v3 archive. Output defaults to a new
timestamped `.codex_runs/rgss-compare` directory. The local fixture files are
from `RPG-Maker-Translation-Tools/rpgm-archive-decrypter-lib`, not an MTool test
suite. Different archives with the same filename get separate numbered output
prefixes. Exit code 0 means the declared comparison passed; 2 means a text or
payload difference was found. Startup/export errors exit nonzero with detail.

## What Runs

- Original MTool `RGSSAD_MTool` enumerates and reads each archive payload.
- Original MTool `CLoader.getAllStrings` traverses real Ruby Marshal objects.
- Original `CLoader.extract_script_data` decompresses the script table.
- Original `CLoader.eventCommandListProcObj` exports event commands.
- Original NW.js `RGSSWorker.getStringFromRubyScript` extracts Ruby text.
- EngAixt independently reads the same input, with the input SHA-256 in each
  report. File order and payload hashes, per-file data text sets, event message
  sets, and per-script text sets and occurrence counts are compared.

The Ruby adapter defines data-only RPG classes and opaque `Table/Color/Tone`
objects, and makes the CLoader window-evaluation bridge a no-op. It does not
execute game Ruby code. The original extraction functions are unchanged.

MTool's raw data traversal includes paths and internal names; reports retain
that raw result. The comparison applies the same EngAixt Japanese selector to
both data outputs, and the same Japanese predicate to both script outputs.
English event message comparison separately uses the visible-text predicate.
Matching under these explicit selectors is not proof that every UI option,
resource exclusion rule, custom engine extension, or runtime behavior matches.

## Evidence And Limits

Each run retains `summary.json`, per-input `*.comparison.json`, original MTool
data/script outputs, `provenance.json`, `mtool-integrity.json`, and startup/error
logs. The provenance identifies Node/NW.js/Ruby versions and SHA-256 values for
MTool's loader and Ruby resources. The integrity file verifies that the package,
executable, and loaders did not change during the run. MTool may create its own
normal local startup/cache files; the harness does not edit installation code.

The extracted MTool resources and fixture source stay inside ignored
`.codex_runs`; never commit, publish, or bundle them. Only this independent
adapter is project source. A different MTool build may change internal APIs or
resource packaging; such errors must be fixed in the adapter and must not be
replaced by EngAixt-generated expected outputs.

These tests do not validate native RGSS1/2/3 font rendering, game display,
Ruby 1.8/1.9 runtime execution, MTool cloud translation, or all RPG Maker games.
The public VX/VX Ace samples are compatibility samples, not a success-rate
measurement. XP needs an independent real XP sample before making the same
sample-specific parity claim for XP.
