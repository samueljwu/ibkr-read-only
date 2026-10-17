# Desktop installers

Use Python 3.12 on a Windows x64 machine or an Apple silicon Mac. From the
repository root, create a build environment:

```text
python -m venv .venv-release
```

Then run these commands in PowerShell on Windows:

```powershell
.\.venv-release\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv-release\Scripts\python.exe packaging/check_release.py
.\.venv-release\Scripts\python.exe packaging/collect_licenses.py
.\.venv-release\Scripts\python.exe -m pytest -q
.\.venv-release\Scripts\python.exe -m PyInstaller --noconfirm packaging/portfolio_monitor.spec
./packaging/windows/build_installer.ps1
```

Or run these commands on macOS:

```sh
.venv-release/bin/python -m pip install -r requirements-dev.txt
.venv-release/bin/python packaging/check_release.py
.venv-release/bin/python packaging/collect_licenses.py
.venv-release/bin/python -m pytest -q
.venv-release/bin/python -m PyInstaller --noconfirm packaging/portfolio_monitor.spec
version=$(.venv-release/bin/python -c 'from app.version import VERSION; print(VERSION)')
sh packaging/macos/build_dmg.sh dist/PortfolioMonitor.app "dist/installers/PortfolioMonitor-${version}-macos-arm64.dmg"
```

Windows additionally requires [Inno Setup 6](https://jrsoftware.org/isinfo.php)
(`ISCC.exe`) on `PATH`, at its default installation path, or specified by
`ISCC_PATH`. The Windows output is
`dist/installers/PortfolioMonitor-<version>-windows-x64-setup.exe`; it installs
per user and adds a Start menu entry. The macOS output is
`dist/installers/PortfolioMonitor-<version>-macos-arm64.dmg`; drag the app to
Applications. Each app bundle is also available at `dist/PortfolioMonitor/`
or `dist/PortfolioMonitor.app` before installer creation. Run its executable with
`--bundle-check` and `QT_QPA_PLATFORM=offscreen` to verify bundled Qt images,
SQLCipher, and worker imports without creating a portfolio or accessing a
credential store. Then launch the app normally and exercise a synthetic
portfolio, including the guide, exports, and background sync.

The GitHub Actions **Desktop bundles** workflow builds both native targets on
demand and uploads the installer files and a matching application source archive.
Installer creation uses the version in
`app/version.py`. Install, upgrade, and uninstall leave portfolio data in the
per-user data directory. These builds are unsigned and the macOS app is not
notarized, so the operating system may require a manual security override to
open them. Keep the matching source tree and `LICENSE` available with any
distributed installer. The `licenses` folder inside each app includes an exact
runtime dependency inventory and the license files available from the installed
packages. Review that inventory and the upstream source links in
`THIRD_PARTY_NOTICES.md` before publishing a release.
