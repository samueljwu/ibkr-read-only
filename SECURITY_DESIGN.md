# Security design and release gates

Status: design in progress. Freshly initialized portfolio databases and new
backups use SQLCipher. Existing databases stay plaintext until converted, and
migration retains a plaintext original. Old backups, workbook exports,
imported source files, and diagnostic logs may remain plaintext. The application must not be
described as production secure.

## Current storage behavior

- A freshly initialized portfolio gets a random SQLCipher key stored in Windows
  Credential Manager. The app shows its recovery code on first launch and can
  show it again from **File → Show Database Recovery Code**. Restoring an encrypted file on
  another Windows profile requires that code.
- **File → Create SQLite Backup** and automatic backups of changed imports
  create encrypted copies, including when the active database is still
  plaintext. Earlier backups are not converted automatically.
- A previously created plaintext database remains plaintext during normal use.
  **File → Encrypt Database** converts it after verification and retains a
  plaintext recovery copy beside it. Copying a plaintext portfolio to a new
  data directory also leaves that copy plaintext. Check every retained copy,
  older backup, export, and source CSV before treating a folder as encrypted.
- Workbook exports, selected older CSV reports, diagnostic logs, and worker
  status files are outside SQLCipher's database protection. Downloaded Flex XML
  is not saved by default. Keep data and backup folders local and protected;
  a user-selected folder may be synchronized by another service.

## Assets and boundaries

The sensitive assets are Flex Web Service tokens, account and query identifiers,
trades, positions, cash movements, reports, exports, backups, and diagnostic
logs. User data belongs in a per-user local data directory, separate from
installed application files and source control. A user-selected directory may
be synchronized by another service; the application must explain that choice.

IBKR account credentials are never requested by this application. Flex tokens
are stored in Windows Credential Manager and used only for direct report
requests. The token travels in IBKR's required query parameter, so requests
must reject redirects and diagnostics must never print full request URLs.
Live broker connections are local TWS or IB Gateway socket connections and
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
same user's access while the portfolio is open. Keep the recovery code
separately from the database and backups. SQLCipher integration and its
packaged dependency still require release review.

## Decisions still required

- Verify the packaged SQLCipher binding, journals, temporary files, crash
  recovery, and key handling on clean supported Windows installations.
- Test encrypted backup restoration after the original Windows profile is lost,
  including wrong keys, interrupted exports, and older plaintext backups.
- Define supported IBKR account types, base currencies, instruments, report
  formats, and historical coverage limits before the importer can claim a
  complete result.
- Decide whether source and release builds will run only in an isolated CI
  environment or also through a reproducible local build environment. Pin
  dependencies and review licenses before shipping.

## Required verification

- Audit the full public Git history and every release artifact, not only the
  working tree. Use synthetic fixtures; do not run CI with live tokens or data.
- Test wrong-account, malformed, oversized, repeated, and partial reports.
- Test direct HTTPS behavior, redirect rejection, token redaction, expiry, and
  revocation handling.
- Test schema migrations, encrypted backup and restore, clean install, upgrade,
  offline startup, and uninstall data retention on a clean Windows account.
- Check backups made from plaintext databases and the residual copies, logs,
  status files, and exports left after migration or a changed data directory.
- Review the build dependency list, sign release packages, verify provenance,
  and inspect the exact files shipped.
