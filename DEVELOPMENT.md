# Development and Windows releases

[Back to README](README.md)

## Development

Application modules are grouped by responsibility:

| Package | Responsibility |
| --- | --- |
| `app/ui/` | Main window, tabs, dialogs, tables, and theme. |
| `app/finance/` | Portfolio calculations, analytics, returns, exposure, and matching. |
| `app/broker/` | IBKR market data, execution ingestion, lots, and fees. |
| `app/imports/` | Flex and Activity Statement importers and workbook export. |
| `app/storage/` | Database, portfolio paths, credentials, and file operations. |
| `app/sync/` | Sync coordination, history coverage/status, timing, and workers. |
| `app/platform/` | Build identity, diagnostics, and package verification. |
| `app/updates/` | GitHub release checks, verified downloads, and installer handoff. |

`app/resources.py` anchors assets and project paths independently of module depth.
`main.py` and the `scripts/` command entry points remain the launch interfaces.
The Windows source allowlist in `scripts/build_windows.py` includes these packages;
update its explicit `APP_MODULES` file list when adding any application module.
Unknown or nested Python modules fail staging until reviewed and enumerated.

Install test dependencies and run the synthetic test suite:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
```

To deliberately test with a copy of an old or stable portfolio, use the copy tool. It reads the source without modifying it, creates a new Dev portfolio ID, and clears copied storage/backup directory settings:

```powershell
.\.venv\Scripts\python.exe -m scripts.copy_database --source "data\my-old-portfolio\portfolio.sqlite3"
```

Launch with `--database` and the path printed by that command. Dev requires its own saved Flex token. Copying data does not copy OS credentials.

## Windows packaging and releases

One source tree supplies stable and Dev builds. Source launches always use
Dev. Frozen builds read their channel from bundled build metadata; changing an
environment flag cannot turn Dev into stable. Both show the version in the
window title and Windows executable properties. Update checks and downloads
start only when requested through Help; the app does not check automatically.

| Item | Stable | Dev |
| --- | --- | --- |
| Name | Portfolio Monitor | Portfolio Monitor Dev |
| Install directory | `%LOCALAPPDATA%\Programs\CustomIBMonitor-Stable` | `%LOCALAPPDATA%\Programs\CustomIBMonitor-Dev` |
| Data root | `%LOCALAPPDATA%\CustomIBMonitor\stable\data` | `%LOCALAPPDATA%\CustomIBMonitor\dev\data` |
| Credential target | `custom-ib-monitor:stable:ibkr-flex:<account>` | `custom-ib-monitor:dev:ibkr-flex:<account>` |
| Installer/taskbar identity | `CustomIBMonitor.PortfolioMonitor.stable` | `CustomIBMonitor.PortfolioMonitor.dev` |

No channel automatically uses an old checkout, old installed app, or another
channel's config/database/token. Existing data and credentials are preserved.
Use `python -m scripts.copy_database --source <database>` from source to create
an explicit read-only copy in Dev. This clears copied data/backup directory
settings and never copies credentials. To use previous data in stable, review
and back up the database, then deliberately copy it into a stable portfolio
subfolder and launch stable with `--database <copied-path>`. Do not relocate the
original during migration. The two copies then evolve independently.

### Build a local candidate

Builds currently pin **Windows x64 Python 3.11.4**, matching the
historical environment snapshot. The v1.0.1 patch retains this older runtime
under the owner's explicit distribution review deferral. Runtime and licensing
findings remain unresolved. A future runtime update must select a reviewed
security-patched Windows Python 3.11 build, record its provenance, and update
`CANDIDATE_PYTHON`, the environment and licensing record together. Create an isolated build environment
with the exact candidate interpreter:

```powershell
& '<candidate-Python-3.11.4>\python.exe' -m venv .venv-release
.\.venv-release\Scripts\python.exe -m pip install -r requirements-release.txt
```

The builder rejects a base interpreter, system-site-enabled virtual environment,
wrong patch/architecture, `PYTHONHOME`/`PYTHONPATH`, dependency version drift,
ranges/wildcards/extras, missing active transitive pins and unexpected installed
distributions. Bootstrap `pip` is excluded from the payload. Exact versions do
not establish native binary provenance; inspect the recorded tools and payload.

`release-license-review.json` records the pinned dependency/notice inventory
and unresolved distribution gates. Local candidates retain its blocked state
and SHA-256 in build metadata and copy it beside the executable. Both `--release`
and `--dev-release` refuse an uncleared, incomplete or version-drifted review
before invoking native tools by default. An owner who explicitly authorizes
publication with outstanding findings can supply `--defer-distribution-review
"<reason>"` together with a publication mode. This records the reason alongside
the unchanged review status/hash in the installer provenance. It does not grant
licence permission, approve the older runtime, or pass native acceptance checks;
the release notes must retain these limitations. Exact inventory, dependency,
source and committed-tag checks still apply. Approval requires documented reviewer/evidence,
no unresolved entries and exact interpreter/dependency agreement. Staging checks
the exact copied requirements and review again. See the
[distribution limitations](SECURITY_DESIGN.md#verification-and-distribution-limitations).
Publication also rechecks HEAD, tag and cleanliness after staging and compares
every staged input to its committed Git object, applying Git's checkout newline
filters. An edit between the initial check and staging cannot inherit the tag.

Use the official signed Inno Setup 6.7.3 compiler. Its own installer accepts
`/PORTABLE=1 /CURRENTUSER /NOICONS /DIR="<project>\build\tools\inno-6.7.3"` to
keep build tooling inside the ignored build directory. Verify its signature
before running it. The build script verifies its version and records its hash.

```powershell
.\.venv-release\Scripts\python.exe -m scripts.build_windows --channel stable --iscc build\tools\inno-6.7.3\ISCC.exe
.\.venv-release\Scripts\python.exe -m scripts.build_windows --channel dev --iscc build\tools\inno-6.7.3\ISCC.exe
```

The script never commits, pushes, tags, publishes, or installs the app. It:

1. Copies only selected source files, documentation and the icon into a fresh
   staging directory. No financial data, local settings, tests or `.git` history
   are copied. Checks selected text for likely account IDs and embedded tokens.
2. Records source file hashes, the aggregate source hash, base Git revision,
   dirty state, channel and version. Uncommitted builds are visibly local
   candidates: `1.0.2+local.<hash>` or `1.0.2-dev+local.<hash>`.
3. Builds a PyInstaller one-folder app, with all four workers using that same
   executable and an explicit database path. Includes timezone data and runtime
   package metadata and available licence files, and creates an exact payload
   manifest. These files and the review inventory do not constitute complete
   redistribution notices or permission clearance.
4. Rejects data-like files/configs in the payload, compiles the Inno installer,
   and writes the installer manifest and SHA-256 checksum under `dist/<channel>`.

Builds preserve prior outputs and refuse to overwrite a matching build. Logs
are under `build/<channel>/<source-hash>`. A repeated build after editing source
gets a new source hash. Byte-for-byte reproducibility is not claimed: manifests
make the exact inputs and outputs inspectable.

### Offline package checks

The v1.0.1 stable and Dev patch candidate uses SQLCipher for portfolio
connections, local backups, migration and portable recovery. The published v1.0.0 installer remains
plaintext. Native tests require the reviewed `sqlcipher3` wheel. Tests marked
`native_key_store` use random synthetic Windows Credential Manager targets and
delete only those targets; ordinary unit fixtures use a test-only memory store.
Running the full suite with the Windows store requires access outside the
restricted credential-store sandbox. Never substitute real portfolio files or
broker credentials. See [the security boundary](SECURITY_DESIGN.md).

Production uses DELETE journals because the reviewed wheel's SQLite 3.51.1
predates the WAL-reset corruption fix. Explicit-WAL reference tests do not
approve production WAL concurrency. Record the exact native extension hash,
SQLCipher/SQLite/OpenSSL versions and installed package receipts with each
candidate. Successful same-machine profile-key deletion and restore is useful
evidence, but actual clean-account/other-computer acceptance remains separate.

The owner has authorized the combined v1.0.1 patch release and explicitly
deferred the recorded distribution review. Publication requires final source
and installed-artifact verification; the runtime/license findings and actual
clean-profile/other-computer recovery acceptance remain unresolved.

Use a fresh disposable Windows local-data directory inside the workspace:

```powershell
$env:LOCALAPPDATA = Join-Path (Get-Location) 'build\qa\fresh-profile'
$env:QT_QPA_PLATFORM = 'offscreen'
& '<bundle-or-installed-app>\PortfolioMonitor.exe' --verify-package '<project>\build\qa\stable.json'
```

Run in a dedicated shell so these QA environment settings do not affect normal
launches. `--verify-package` refuses existing channel data. It checks empty
offline GUI startup, identity/icon, persistence/restart, backup/export, and
all packaged workers' unconfigured outcomes without credentials or broker
requests. It writes a JSON report and GUI screenshot. Use separate fresh roots
for subsequent runs; stable and Dev can share one fresh profile to check that
their channel directories differ. These checks do not prove live broker
connectivity or successful imports.

Installers have separate stable/Dev application IDs, names, shortcuts and
directories. Upgrades reuse only their channel's install identity. No installer
or uninstaller action removes portfolio directories or credential targets.
Numeric version downgrades are refused when a newer same-channel executable
is registered. Before distribution, verify clean-user/VM install, side-by-side
launch, upgrade with saved data, uninstall retention, and successful fresh
broker import. Offline synthetic checks alone do not satisfy those live gates.

### Publication workflow

Use reviewed `main` and short-lived `codex/...` development branches. Keep
Windows v1 separate from the existing future macOS Kanban cards. Formal stable
builds use `--release`; the script requires a clean committed checkout and
`v1.0.2` pointing at its exact source commit. Update `BASE_VERSION` in
`app/platform/runtime.py` for subsequent releases. Never reuse a published tag for changed
source. Published Dev prereleases use `--channel dev --dev-release N` with a
positive sequence number `N`, a clean committed checkout, and an exact
`v1.0.2-dev.N` tag. Stable assets are named
`PortfolioMonitor-1.0.2-windows-x64-setup.exe`; Dev assets are named
`PortfolioMonitorDev-1.0.2-dev.N-windows-x64-setup.exe`. The build also emits
matching `.manifest.json` and `.sha256` files. Stable and local executable
numeric versions use `1.0.2.0`; published Dev sequence `N` uses `1.0.2.N`
for downgrade protection. A local candidate or Dev prerelease does not become
a formal stable release.

The updater requires public releases and assets from
`samueljwu/custom-ib-monitor`; it has no token or private-repository mode.
Only exact stable tags `vMAJOR.MINOR.PATCH` and Dev tags
`vMAJOR.MINOR.PATCH-dev.N` are eligible in their respective installed channels.
The matching installer must have a valid SHA-256 release asset digest or an
exact matching `.sha256` asset. Missing, malformed, mismatching, oversized or
ambiguous integrity information blocks installation. This verifies the bytes
against the trusted GitHub release; it is not independent publisher signing.
Release notes are displayed as plain text. The app never checks automatically
or downloads until the user selects **Update now**. Source checkouts cannot
self-update. Existing installations that do not contain this updater require
one manually obtained compatible installer before this Help action exists.

The updater refuses handoff while a sync, import, database writer or data move
is pending. Complete the work first; a pending data relocation requires a
normal close and restart. It uses a separate channel cache outside portfolio
storage, preserves database/settings/credentials and opens the ordinary per-user
Inno wizard. If Setup is cancelled or rejects the install, reopen the retained
installed version; the updater does not claim success. Other busy instances
must close normally. No updater path force-closes them. The normal postinstall
**Open Portfolio Monitor** checkbox waits for Setup to finish before the new
process enters channel admission and registers its app mutex.

Audit the complete publishable Git history before any push. The current local
repository's old history is not made safe by the source-file packaging
allowlist. Review privacy, licensing, native-machine checks and code signing
before publication. Upload the installer, manifest and checksum as public
GitHub Release assets; do not commit generated binaries or financial data. No
publication is authorized by running this build script. Validate public API
visibility, exact asset names/digests, signed installer behavior, clean-user
stable and Dev upgrades, cancel/reopen, and mutex timing in a disposable
Windows VM before claiming those acceptance gates passed. The owner-authorized v1.0.1
publication defers the recorded distribution review; it does not establish
actual clean-profile/other-computer recovery, live broker, or signed installer
acceptance. Offline synthetic tests do not pass those gates.
