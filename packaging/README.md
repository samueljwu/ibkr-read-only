# Desktop installers

Run the [Desktop bundles workflow](../.github/workflows/bundles.yml) to build
both installers and a matching source archive.

For a local build, use Python 3.12 from the repository root.

**Windows x64:** Install [Inno Setup 6](https://jrsoftware.org/isinfo.php).
Use its default location, put `ISCC.exe` on `PATH`, or set `ISCC_PATH`.

```powershell
python -m venv .venv-release
.\.venv-release\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv-release\Scripts\python.exe packaging/check_release.py
.\.venv-release\Scripts\python.exe packaging/collect_licenses.py
.\.venv-release\Scripts\python.exe -m pytest -q
.\.venv-release\Scripts\python.exe -m PyInstaller --noconfirm packaging/ibkr_dashboard.spec
./packaging/windows/build_installer.ps1
```

**Apple silicon macOS:**

```sh
python3 -m venv .venv-release
.venv-release/bin/python -m pip install -r requirements-dev.txt
.venv-release/bin/python packaging/check_release.py
.venv-release/bin/python packaging/collect_licenses.py
.venv-release/bin/python -m pytest -q
.venv-release/bin/python -m PyInstaller --noconfirm packaging/ibkr_dashboard.spec
version=$(.venv-release/bin/python -c 'from app.version import VERSION; print(VERSION)')
sh packaging/macos/build_dmg.sh dist/IBKRDashboard.app "dist/installers/IBKRDashboard-${version}-macos-arm64.dmg"
```

## Release checklist

- Windows: `dist/installers/IBKRDashboard-<version>-windows-x64-setup.exe`.
- macOS: `dist/installers/IBKRDashboard-<version>-macos-arm64.dmg`.
- Run the bundled executable with `--bundle-check` and `QT_QPA_PLATFORM=offscreen`.
- Test a synthetic portfolio, including exports and background sync.
- Review bundled `licenses` and [third-party notices](../THIRD_PARTY_NOTICES.md).
- Distribute the matching source tree and [LICENSE](../LICENSE).

Versions come from `app/version.py`. Install, upgrade, and uninstall leave
portfolio files untouched. The renamed app uses `IBKRDashboard` for new data;
existing `CustomIBMonitor` portfolios are opened explicitly as described in
the [data section](../README.md#your-data). Builds are unsigned; macOS is not
notarized and may need a security override to open.
