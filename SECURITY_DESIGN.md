# Security design and release gates

Status: design in progress. New portfolio databases and new backups use
SQLCipher. Existing databases stay plaintext until converted, and migration
retains a plaintext original. Old backups and exports may also remain plaintext.
The application must not be described as production secure.

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
same user's access while the portfolio is open. SQLCipher uses a random
per-portfolio key stored in Windows Credential Manager for automatic unlock.
The user must keep the recovery code separately to restore on another profile.
This approach and its packaged dependency still require release review.

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
- Review the build dependency list, sign release packages, verify provenance,
  and inspect the exact files shipped.
