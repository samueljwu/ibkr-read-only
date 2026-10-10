# Security design and release gates

Status: design in progress. The current application uses unencrypted SQLite
and unencrypted SQLite backups. It must not be described as production secure.

## Assets and boundaries

The sensitive assets are Flex Web Service tokens, account and query identifiers,
trades, positions, cash movements, reports, exports, backups, and diagnostic
logs. Source runs use the Dev channel. Windows portfolios, backups, reports
and logs live in a portfolio subfolder under
`%LOCALAPPDATA%\CustomIBMonitor\dev\data\`; stable builds use `stable\data\`.
macOS uses the corresponding channel under Application Support; Linux uses
the corresponding channel under `~/.local/share/CustomIBMonitor/`.
The old checkout's data and configuration are never adopted automatically.

IBKR account credentials are never requested by this application. Flex tokens
are stored in Windows Credential Manager under distinct stable/Dev targets and used only for direct report
requests. The token travels in IBKR's required query parameter, so requests
must reject redirects and diagnostics must never print full request URLs.
Legacy credential targets are not automatically reused. Live broker connections are local TWS or IB Gateway socket connections and
must remain read-only.

## Threats to address before publication

1. A public commit or release accidentally contains account data, a report,
   a database, or a token.
2. A copied database, temporary file, export, or backup exposes finances.
3. A malformed report exhausts resources, bypasses account checks, or creates
   incomplete ledger entries that appear reconciled.
4. A token leaks through logs, errors, redirects, command arguments, or a
   support bundle.
5. An installer or update is replaced or built from unreviewed dependencies.
6. A failed upgrade corrupts the only copy of the portfolio.

The application cannot protect data from malware already running with the
same user's access while the portfolio is open. Encryption at rest and the
Windows credential store address different risks; both require review.

## Selected designs and remaining gates

- Validate the selected SQLCipher engine and key custody before runtime
  integration; see [Encrypted storage](#encrypted-storage). The current app
  remains plaintext.
- Integrate the [portable recovery design](#portable-encrypted-recovery-backup)
  and verify restoration on an actual clean Windows installation.
- Enforce the [importer support contract](HISTORICAL_REPORTS.md#importer-support-contract-v1)
  and its remaining provenance/lifecycle gates before claiming a complete result.
- Local Windows builds use a pinned, isolated Python 3.11 environment and an
  explicit source allowlist. See [Windows packaging and releases](DEVELOPMENT.md#windows-packaging-and-releases)
  for candidate metadata and committed-tag release checks. Review dependency
  licenses before shipping.

## Required verification

- Audit the full public Git history and every release artifact, not only the
  working tree. Use synthetic fixtures; do not run CI with live tokens or data.
- Test wrong-account, malformed, oversized, repeated, and partial reports.
- Test direct HTTPS behavior, redirect rejection, token redaction, expiry, and
  revocation handling.
- Test schema migrations, encrypted backup and restore, clean install, upgrade,
  offline startup, and uninstall data retention on a clean Windows account.
- Review the build dependency list, sign release packages, verify provenance,
  and inspect the exact files shipped.

## Encrypted storage

Decision recorded 10 October 2026.

Selected engine: **SQLCipher 4**, with its default authenticated encrypted pages.
Selected Windows custody: a random 256-bit key per portfolio, held in Windows
Credential Manager, separate from the Flex token. Targets include the software
channel and immutable portfolio UUID, never a mutable portfolio name or account
number. Loss of that Windows profile is handled by the independent recovery
backup described in [Portable encrypted recovery backup](#portable-encrypted-recovery-backup). A missing credential must stop opening
the portfolio; never generate a replacement key over existing data.

This is a design and opt-in reference implementation. The desktop application,
its normal Backup button and existing installed candidates **still use plaintext
SQLite**. No existing data has been migrated. Reference modules in `scripts/`
are excluded from the installer. Do not describe the application as encrypted.

### Engine provenance and connection policy

The QA dependency is `sqlcipher3==0.6.3`, a third-party Python binding whose
Windows CPython 3.11 x64 wheel reports `4.12.0 community`. It is **not** a
Zetetic-supported Windows binary. The binding and engine versions are checked
at opening; there is no stdlib SQLite fallback. Exact Windows wheel hashes are
pinned in `requirements-security-qa.txt`. Establish native crypto-provider
provenance before approving this dependency for release;
a package version alone does not establish reproducible native contents.
Compare the embedded engine against current vendor fixes before distribution.

`scripts/security_storage.py` supplies raw random keys, SQLCipher 4 settings,
no plaintext header, HMAC defaults, `temp_store=MEMORY`, no memory-mapped database
and `synchronous=FULL`. It verifies the key with a schema read before changing
journal mode. Ordinary opening uses `mode=rw`; only explicit creation may create
a file. Key-bearing SQL must never be traced or logged. Keys must not appear in
CLI arguments, configuration, logs or filenames. Runtime integration must reuse
the same adapter for GUI, workers, relocation and restore.

The optional `cipher_memory_security=ON` setting produced a **native Windows
stack-overflow crash** in the downloaded wheel during QA. A separate minimal
probe also failed after enabling it; without it, the native storage tests ran.
The reference adapter leaves this optional setting disabled. Resolving the
native failure or choosing a reviewed alternative build remains a release gate;
this reference offers no locked-memory or comprehensive memory-wiping guarantee.
Python key copies, process memory, swap/hibernation and crash dumps are outside
the at-rest page-encryption boundary. OS disk encryption and dump policy remain
separate controls.

### Files and recovery

SQLCipher encrypts database pages in rollback journals and WAL files. WAL shared
memory holds index/locking information, not encrypted database pages; it is not
a confidential container for arbitrary application data. Force temporary SQL
tables and sorts into memory on **every** connection. Filename, size, access
times, WAL activity and locking metadata remain observable.

Raw Flex XML (including rejected/pending reports), workbook exports, existing
SQLite backups, old databases and diagnostics are separate plaintext artifacts
in the current app. Database encryption alone does not cover them. Before the
encrypted product gate passes, eliminate disk plaintext report caching or
encrypt each cache artifact, redact diagnostics, and require an explicit user
choice for plaintext workbook export. Existing plaintext copies on cloud storage,
snapshots, recycle bins and backups are not securely erased by migration.

Migration must quiesce all writers, take a verified recovery backup, export into
an encrypted temporary database with SQLCipher's supported export API, verify
schema/counts/settings and both integrity checks, close it, then publish the
selection pointer last. Preserve the original. Never rekey a plaintext file
in place. Resume after interruption only from a verified finalized copy; do not
adopt an incomplete temporary database. Do not copy just a live WAL main file.

### Executable verification and remaining gates

Use an isolated environment inside ignored build output:

```powershell
.\.venv\Scripts\python.exe -m venv build\security-qa\venv
.\build\security-qa\venv\Scripts\python.exe -m pip install --require-hashes --only-binary=:all: -r requirements-security-qa.txt
.\build\security-qa\venv\Scripts\python.exe -m pytest -q tests/test_security_storage.py tests/test_encrypted_backup.py --basetemp build\security-qa\new-run
```

Native tests cover WAL and DELETE journals with forced page-cache spill, temp
tables/sorting in memory, sentinel absence in files, wrong-key rejection without
rewriting the database, refusal by stdlib SQLite and abrupt process exit with an
uncommitted transaction. Reopening must retain only the committed record and
pass cipher and SQLite integrity checks. Active Windows `-shm` may be locked;
the post-crash scan inspects retained sidecars without bypassing read errors.
Sentinel scanning is evidence for the tested writes, not a proof about every
possible temporary allocation or filesystem trace.

Release acceptance additionally requires real Credential Manager round-trip
and channel/UUID isolation under clean Windows users, full runtime migration,
worker/GUI crash recovery, encrypted raw-report storage and install/upgrade/
restore tests on another Windows computer. Optional tests skipped because the
engine is absent do not satisfy these gates.

### Primary references

- [SQLCipher page/journal/temp design](https://www.zetetic.net/sqlcipher/design/)
- [SQLCipher key, export and integrity APIs](https://www.zetetic.net/sqlcipher/sqlcipher-api/)
- [Python binding source and binary provenance](https://github.com/coleifer/sqlcipher3)
- [Exact binding release](https://pypi.org/project/sqlcipher3/0.6.3/)
- [Microsoft credential storage guidance](https://learn.microsoft.com/en-us/windows/win32/SecBP/handling-passwords)

## Portable encrypted recovery backup

Decision: each backup is a standalone **SQLCipher 4 database encrypted with an
independent random 256-bit recovery key**. Export uses SQLCipher's supported
`sqlcipher_export`, not a copy of the live main database. The recovery key is
user-held outside the Windows profile, database directory and backup media.
There is no DPAPI, Credential Manager, machine ID or original local database-key
dependency in restore. No home-grown encryption envelope or password KDF is
needed. A human-selected password is not a supported substitute for a random key.

The reference functions are in `scripts/encrypted_backup.py`; they are opt-in
and are not exposed by the current desktop Backup button. The installed app's
normal backups remain plaintext. See [Encrypted storage](#encrypted-storage) for native provenance and
the unresolved optional memory-security crash.

### Export and interruption

1. The product must show a newly generated recovery code once, require a proof
   that it was saved (including a test restore), and explain that losing it
   loses access. Recommended custody is a password manager with independent
   recovery plus a separate offline copy. Never log or persist it beside backups.
2. Finish source transactions. Attach a newly created, encrypted `.pending`
   database in the destination directory; use DELETE journaling and FULL sync.
   Export inside one transaction, copying `user_version` and `application_id`
   explicitly. A WAL-only committed source record must appear in the backup.
3. Detach, reopen with the recovery key, check page authentication and SQLite
   integrity, and flush the file. Publish atomically without overwriting any
   existing path. The reference uses Windows no-clobber rename, or a
   same-directory hard link on POSIX; unsupported filesystems fail closed and
   need another reviewed publication mechanism. Current artifact size limit is
   512 MiB, checked before restoration and before publication.
4. A handled failure removes only its own temporary files. A hard crash may
   leave an encrypted `.pending` file or journal; it never establishes a final
   backup. Never automatically restore one. Keep existing finalized backups;
   attempt a fresh export and let the user deliberately clean old pending files.

FULL sync and atomic publication cover the tested process-crash boundaries;
they are not a promise against power failure, faulty drives or remote/cloud
filesystem behavior. Test those storage targets before supporting them. Backup
file sizes, names, dates and schema-independent filesystem metadata are visible.

### Restore after Windows profile loss

On another computer install the approved runtime, bring a **finalized** backup
and the separately held recovery key, and choose a fresh empty portfolio folder.
Open the backup read-only. Wrong key, missing file, unsupported engine, oversized
file, authentication failure or integrity failure must leave the destination
absent and the backup unchanged. Export into a new encrypted local database
under a newly generated local key; never retain the recovery key as its daily
unlock key. Do not overwrite any existing database.

Before adopting it, the future desktop restore flow must validate supported
schema version, account ownership and expected tables, migrate only a disposable
copy, store the new local key under this computer's channel/UUID credential
target, then publish the portfolio selection last. The reference creates a
quarantined copy only: it does not migrate schema, access credentials or change
selection. Unsupported/newer schemas must remain unadopted. Flex tokens are
not included; the user supplies a new token on the new profile.

Key rotation affects future backups; keep old recovery codes until all retained
old backups are replaced and verified. Losing the original Windows profile is
recoverable. Losing both the profile and every independent recovery code is not.

### Test evidence and release acceptance

Native synthetic tests cover wrong key, altered encrypted page, existing
destination protection, committed WAL content, application/schema version
preservation, uncommitted-source refusal, failure immediately before publication
and hard-process exit leaving only encrypted pending content. Fresh-profile
restore runs in another Python process with a new USERPROFILE/LOCALAPPDATA,
after deleting the original synthetic database; no credential store is read.

A separate **fresh reference installation** also passed: a new virtual
environment installed the hash-identified SQLCipher wheel offline, loaded only
the two copied reference modules, rejected a wrong key and restored with a new
local key after the original synthetic database was deleted. It used new
USERPROFILE/LOCALAPPDATA values and left backup bytes unchanged. Evidence is
`build/security-qa/fresh-installation-20261010/result.json`. The QA
requirements pin exact Windows wheels by SHA-256; the SQLCipher wheel hash is
`136b04e6e481c0a2a0b63f13d16b05419714a4368355c93ab10f9c4d99aab4fb`.

This is a fresh-profile **simulation on the development computer**, not a fresh
installed GUI or another-computer acceptance test. Before release, transfer a
synthetic backup and recovery code to an actual clean Windows installation
without the old profile; verify GUI schema/adoption, local credential custody,
restart, all totals and worker access, wrong-key behavior and interrupted export.
Record exact installer/native-library hashes and Windows version. Existing
clean-Windows Kanban acceptance remains open. Run commands are in [Encrypted storage](#encrypted-storage).

[SQLCipher export/key/integrity documentation](https://www.zetetic.net/sqlcipher/sqlcipher-api/)
describes the native operations used by this reference.

## Dependency and distribution review

The review recorded on 10 October 2026 is **blocked for distribution**.
`release-license-review.json` preserves all 20 active pinned runtime/build
versions and 26 installed licence/notice file hashes. This is an installed
environment inventory plus findings from earlier candidate payloads; it is not
an audit of a newly built installer or approval. Metadata alone cannot establish
rights for every native library. Both publication build modes enforce this gate
by default. An explicitly authorized owner deferral is recorded in publication
metadata without changing this review or approving any remaining findings;
local candidates display the status and exact review hash in provenance.

| Dependency group | Recorded licence information | Remaining review |
| --- | --- | --- |
| PySide6, Essentials, Addons, shiboken6 6.8.1 | LGPL metadata; installed notice inventory contains Qt Commercial text | Earlier payloads contain Qt6Charts and Qt6VirtualKeyboard, listed GPLv3/commercial by Qt 6.8. Establish a permitted distribution route or remove affected modules, and complete Qt/native notices and source obligations. |
| ibapi 9.81.1.post1 | IB noncommercial/commercial metadata; no complete bundled licence text | Establish applicable third-party redistribution permission or change delivery. Attribution alone supplies no permission. |
| pandas, NumPy | BSD licences; NumPy also carries native-library terms | Retain exact BSD/native notices, including shipped OpenBLAS/LAPACK/compiler runtime terms and exceptions. |
| openpyxl, et_xmlfile, pytz, six | MIT | Retain required notices for exact payload. |
| python-dateutil, packaging, tzdata | BSD/Apache alternatives or Apache; timezone data also has upstream terms | Retain applicable texts and native/data notices. |
| PyInstaller and community hooks | GPL build hooks/bootloader exception; runtime hooks Apache | Review the actual COPYING/exception and shipped runtime hooks. Tool metadata alone is insufficient. |
| altgraph, pefile, pywin32-ctypes, setuptools | MIT/BSD family | Retain applicable notices; distinguish build-only tools from distributed code. |
| CPython and application assets | Runtime licence available in local QA; no application LICENSE yet | Supply complete Python/Qt notices, choose application distribution licence, establish doge.ico rights and approve a security-patched Python 3.11 Windows runtime/provenance. |

The current candidate Python 3.11.4 pin supports reproducing local inputs; it
does not clear the runtime security gate. No licence choice or commercial
permission has been invented. Review the exact future payload after resolving
these items; source allowlisting does not settle licence obligations or remove
sensitive material from old Git history. Existing acceptance and owner-decision
Kanban cards remain open.

Primary references: [Qt 6.8 licensing](https://doc.qt.io/qt-6.8/licensing.html),
[IB API terms](https://interactivebrokers.github.io/index.html),
[Python licensing](https://docs.python.org/3.11/license.html), and
[Python 3.11 security release provenance](https://www.python.org/downloads/release/python-31117/).
