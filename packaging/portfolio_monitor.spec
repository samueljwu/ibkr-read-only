"""One-folder desktop bundle for Windows x64 and macOS Apple silicon."""

import os
import re
import sys
from pathlib import Path

root = Path(SPECPATH).resolve().parent
sys.path.insert(0, str(root))
from app.version import VERSION

if sys.platform not in ("win32", "darwin"):
    raise SystemExit("Build the desktop bundle on Windows or macOS")

analysis = Analysis(
    [str(root / "main.py")],
    pathex=[str(root)],
    binaries=[],
    datas=[
        (str(root / "app" / "assets"), "app/assets"),
        (str(root / "app" / "views" / "help_assets"), "app/views/help_assets"),
        (str(root / "LICENSE"), "."),
        (str(root / "THIRD_PARTY_NOTICES.md"), "."),
        (str(root / "build" / "release-licenses"), "licenses"),
    ],
    hiddenimports=["keyring.backends.macOS"] if sys.platform == "darwin" else [],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pandas", "numpy", "lxml", "PIL"],
    noarchive=False,
)
# PyInstaller searches PATH for native dependencies. Keep a developer machine's
# unrelated DLLs out of the installer; the hosted release runners use the same
# self-contained Python environment and OS libraries.
allowed_binary_roots = [Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve(), root]
if sys.platform == "win32":
    allowed_binary_roots.append(Path(os.environ.get("WINDIR", "C:/Windows")).resolve())
else:
    allowed_binary_roots += [Path("/usr/lib"), Path("/System/Library")]
bundled_binaries = [
    entry for entry in analysis.binaries
    if any(Path(entry[1]).resolve().is_relative_to(base) for base in allowed_binary_roots)
]
# The Widgets app does not render PDFs or use Qt's touch keyboard. Their
# plugins otherwise pull in Qt PDF, QML, and Quick libraries on both targets.
unused_qt_module = re.compile(
    r"qt(?:6)?(?:pdf|qml(?:meta|models|workerscript)?|quick|virtualkeyboard)"
    r"(?:\.framework|\.dll|\.dylib|\.so(?:\.\d+)*)?$",
    re.I,
)


def unused_qt(entry):
    destination = entry[0].replace("\\", "/").lower()
    if not destination.startswith("pyside6/"):
        return False
    if "/plugins/imageformats/qpdf." in destination:
        return True
    if "/plugins/platforminputcontexts/qtvirtualkeyboard" in destination:
        return True
    return any(unused_qt_module.fullmatch(part) for part in destination.split("/"))


bundled_binaries = [entry for entry in bundled_binaries if not unused_qt(entry)]
print(f"Excluded {len(analysis.binaries) - len(bundled_binaries)} unrelated native libraries")
analysis.binaries = bundled_binaries
analysis.datas = [entry for entry in analysis.datas if not unused_qt(entry)]
pyz = PYZ(analysis.pure)
exe = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="PortfolioMonitor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=str(root / "app" / "assets" / "app_icon.ico") if sys.platform == "win32" else None,
)
bundle = COLLECT(
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="PortfolioMonitor",
)
if sys.platform == "darwin":
    app = BUNDLE(
        bundle,
        name="PortfolioMonitor.app",
        bundle_identifier="com.customibmonitor.portfoliomonitor",
        version=VERSION,
        info_plist={"CFBundleName": "Portfolio Monitor", "NSHighResolutionCapable": True},
    )
