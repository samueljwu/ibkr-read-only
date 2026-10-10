"""Build an allowlisted, data-free Windows candidate without changing Git."""

import argparse
import hashlib
import importlib.metadata
import json
import os
import struct
from pathlib import Path
import re
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
APP_MODULES = {
    'broker': ('__init__', 'broker_fees', 'broker_lots', 'broker_sync', 'market_data'),
    'finance': ('__init__', 'analytics', 'calcs', 'calculations', 'exposure', 'instrument_names', 'returns', 'services', 'trade_costs'),
    'imports': ('__init__', 'activity_statement_trades', 'activity_statement_transfers', 'analytics_flex', 'cash_flex', 'excel_io', 'flex_import', 'import_contract'),
    'platform': ('__init__', 'diagnostics', 'package_check', 'runtime'),
    'storage': ('__init__', 'database', 'database_keys', 'encrypted_backup', 'local_files', 'migration', 'paths', 'recovery', 'recovery_code', 'secret_store', 'security_storage', 'storage_operations'),
    'sync': ('__init__', 'history_coverage', 'history_status', 'sync_service', 'sync_timing', 'workers'),
    'ui': ('__init__', 'analytics_ui', 'calcs_ui', 'cash_ui', 'clipboard_ui', 'exposure_custom_ui', 'exposure_ui', 'header_icons', 'help_visuals', 'returns_ui', 'recovery_ui', 'settings_ui', 'table_ui', 'themed_checkbox', 'ui_theme', 'window'),
    'updates': ('__init__', 'release_policy', 'update_handoff', 'update_ui', 'updater'),
}
APP_PACKAGES = tuple(APP_MODULES)
WORKER_FILES = ("__init__.py", "sync_trades.py", "sync_analytics.py", "sync_cash.py",
                "history_worker.py", "import_history.py", "audit_activity_cash.py")
DOCUMENTS = ("README.md", "HISTORICAL_REPORTS.md", "SECURITY_DESIGN.md", "DEVELOPMENT.md")
DEPENDENCIES = ("PySide6", "PySide6_Addons", "PySide6_Essentials", "shiboken6", "pandas",
                "numpy", "openpyxl", "ibapi", "sqlcipher3", "python-dateutil", "pytz", "tzdata", "six", "et_xmlfile")


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).strip()


def source_files(root=ROOT):
    # Every application module is deliberately reviewed and enumerated.
    files = [root / name for name in ("main.py", "requirements.txt", "requirements-release.txt", LICENSE_REVIEW, *DOCUMENTS)]
    files += [root / "app" / name for name in ("__init__.py", "resources.py")]
    files += [root / "app" / package / (module + ".py")
              for package, modules in APP_MODULES.items() for module in modules]
    files += [root / "app" / "assets" / "doge.ico"]
    files += [root / "scripts" / name for name in (*WORKER_FILES, "build_windows.py", "windows_installer.iss")]
    for path in files:
        validate_source_input(path, root)
    # Refuse new/nested Python modules instead of implicitly shipping them.
    unexpected = set((root / "app").rglob("*.py")) - set(files)
    if unexpected:
        raise ValueError("Unallowlisted app source: " + ", ".join(sorted(p.relative_to(root).as_posix() for p in unexpected)))
    for path in (root / "app").rglob("*"):
        if path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
            raise ValueError(f"Linked/reparse app entry requires review: {path.relative_to(root)}")
    return files


def source_manifest(files, root=ROOT):
    result = []
    for path in files:
        relative = path.relative_to(root).as_posix()
        content = path.read_bytes()
        if path.suffix in (".py", ".md", ".txt", ".json"):
            text = content.decode("utf-8-sig")
            if re.search(r"(?<![A-Za-z0-9])[UD]\d{6,}(?!\d)", text):
                raise ValueError(f"Possible personal account identifier in {relative}")
            if re.search(r"(?i)(?:token|password|secret)\s*=\s*['\"]\d{16,}['\"]", text):
                raise ValueError(f"Possible embedded credential in {relative}")
        result.append(dict(path=relative, bytes=len(content), sha256=hashlib.sha256(content).hexdigest()))
    return sorted(result, key=lambda entry: entry["path"])


def audit_bundle(bundle):
    forbidden_parts = {"data", "backups", "reports", "logs", "secrets", ".git", "tests", "__pycache__"}
    forbidden_suffixes = {".sqlite3", ".db", ".xlsx", ".xls", ".csv", ".xml", ".log", ".pem", ".key", ".pmbackup", ".pending"}
    entries = []
    for path in sorted(bundle.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(bundle)
        if path.is_symlink() or forbidden_parts.intersection(part.lower() for part in relative.parts) or path.suffix.lower() in forbidden_suffixes:
            raise ValueError(f"Unexpected data-like file in package: {relative}")
        if (path.name.lower() in ("local_config.json", "history-import-status.json")
                or path.name.lower().startswith((".env", ".storage-"))
                or path.name.lower().endswith(("-wal", "-journal", "-shm", ".identity.json"))):
            raise ValueError(f"Unexpected local configuration in package: {relative}")
        entries.append(dict(path=relative.as_posix(), bytes=path.stat().st_size,
                            sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
    return entries


CANDIDATE_PYTHON = (3, 11, 4)
LICENSE_REVIEW = "release-license-review.json"
SQLCIPHER_WHEEL = "sqlcipher3-0.6.3-cp311-cp311-win_amd64.whl"
SQLCIPHER_WHEEL_SHA256 = "136b04e6e481c0a2a0b63f13d16b05419714a4368355c93ab10f9c4d99aab4fb"
SQLCIPHER_EXTENSION = "sqlcipher3/_sqlite3.cp311-win_amd64.pyd"
SQLCIPHER_EXTENSION_SHA256 = "de34b44f0d16078064d050c68268852946fdabd5c58aadb4c561aa365379233f"
NATIVE_ENGINE = {"binding": "0.6.3", "cipher_version": "4.12.0 community",
                 "cipher_provider": "openssl", "cipher_provider_version": "OpenSSL 3.6.0 1 Oct 2025",
                 "sqlite_version": "3.51.1", "threadsafe": "THREADSAFE=1"}


def validate_native_engine(wheel, *, extension=None, native=None):
    """Bind the installed native engine to the reviewed Windows wheel bytes."""
    wheel = Path(wheel)
    if wheel.name != SQLCIPHER_WHEEL or hashlib.sha256(wheel.read_bytes()).hexdigest() != SQLCIPHER_WHEEL_SHA256:
        raise ValueError("Unreviewed SQLCipher wheel bytes")
    with zipfile.ZipFile(wheel) as archive:
        binaries = [name for name in archive.namelist() if name.lower().endswith((".pyd", ".dll", ".so"))]
        if binaries != [SQLCIPHER_EXTENSION] or hashlib.sha256(archive.read(SQLCIPHER_EXTENSION)).hexdigest() != SQLCIPHER_EXTENSION_SHA256:
            raise ValueError("Unreviewed SQLCipher native binary closure")
    if extension is None:
        import sqlcipher3._sqlite3 as engine
        extension = engine.__file__
        if not Path(extension).resolve().is_relative_to(Path(sys.prefix).resolve()):
            raise ValueError("SQLCipher extension is outside the isolated build environment")
    extension = Path(extension).resolve()
    if hashlib.sha256(extension.read_bytes()).hexdigest() != SQLCIPHER_EXTENSION_SHA256:
        raise ValueError("Installed SQLCipher extension differs from the reviewed wheel")
    if native is None:
        from sqlcipher3 import dbapi2 as cipher
        connection = cipher.connect(":memory:")
        try:
            connection.execute('PRAGMA key="build-provenance-memory-probe"')
            native = {name: connection.execute("PRAGMA " + name).fetchone()[0]
                      for name in ("cipher_version", "cipher_provider", "cipher_provider_version")}
            native.update(binding=importlib.metadata.version("sqlcipher3"),
                          sqlite_version=connection.execute("SELECT sqlite_version()").fetchone()[0],
                          threadsafe=next((row[0] for row in connection.execute("PRAGMA compile_options")
                                           if row[0].startswith("THREADSAFE=")), "missing"))
        finally:
            connection.close()
    if native != NATIVE_ENGINE:
        raise ValueError("Unreviewed SQLCipher engine/provider/SQLite/thread configuration")
    return dict(native, wheel=SQLCIPHER_WHEEL, wheel_sha256=SQLCIPHER_WHEEL_SHA256,
                extension=SQLCIPHER_EXTENSION, extension_sha256=SQLCIPHER_EXTENSION_SHA256,
                binary_closure=[SQLCIPHER_EXTENSION], journal_mode="DELETE", temp_store="MEMORY",
                trusted_schema=False, cell_size_check=True, load_extensions=False,
                cipher_memory_security=False)


def validate_environment(*, prefix=None, base_prefix=None, executable=None,
                         version=None, platform=None, bits=None, environment=None):
    prefix = Path(sys.prefix if prefix is None else prefix).resolve()
    base_prefix = Path(sys.base_prefix if base_prefix is None else base_prefix).resolve()
    executable = Path(sys.executable if executable is None else executable).resolve()
    version = tuple(sys.version_info[:3] if version is None else version)
    platform = sys.platform if platform is None else platform
    bits = struct.calcsize("P") * 8 if bits is None else bits
    environment = os.environ if environment is None else environment
    if platform != "win32" or bits != 64 or version != CANDIDATE_PYTHON:
        raise ValueError("Candidate build requires Windows x64 Python 3.11.4; update the reviewed runtime pin before shipping")
    if prefix == base_prefix or not executable.is_relative_to(prefix) or not executable.is_file():
        raise ValueError("Build requires an isolated virtual environment")
    cfg = prefix / "pyvenv.cfg"
    fields = dict(line.lower().split("=", 1) for line in cfg.read_text().splitlines() if "=" in line) if cfg.is_file() else {}
    fields = {key.strip(): value.strip() for key, value in fields.items()}
    if fields.get("include-system-site-packages") != "false" or fields.get("version") != "3.11.4":
        raise ValueError("Virtual environment must pin Python 3.11.4 and exclude system site packages")
    if any(environment.get(name, "").strip() for name in ("PYTHONHOME", "PYTHONPATH")):
        raise ValueError("Clear PYTHONHOME and PYTHONPATH before building")
    return dict(python="3.11.4", architecture="x64", isolated=True)


def dependency_pins(root=ROOT):
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
    pins = {}
    for filename in ("requirements.txt", "requirements-release.txt"):
        for line in (root / filename).read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line == "-r requirements.txt":
                continue
            requirement = Requirement(line)
            if requirement.marker and not requirement.marker.evaluate({"extra": ""}):
                continue
            specs = list(requirement.specifier)
            if requirement.url or requirement.extras or len(specs) != 1 or specs[0].operator != "==" or "*" in specs[0].version:
                raise ValueError(f"Exact dependency pin required: {line}")
            name = canonicalize_name(requirement.name)
            version = specs[0].version
            if name in pins and pins[name] != version:
                raise ValueError(f"Conflicting dependency pins: {name}")
            pins[name] = version
    return pins


def validate_dependencies(root=ROOT, *, complete_environment=False):
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
    pins = dependency_pins(root)
    for name, expected in pins.items():
        installed = importlib.metadata.version(name)
        if installed != expected:
            raise ValueError(f"Dependency drift: {name} {installed}; expected =={expected}")
    if complete_environment:
        seen = set()
        for distribution in importlib.metadata.distributions():
            name = canonicalize_name(distribution.metadata["Name"])
            if name in seen:
                raise ValueError(f"Duplicate installed dependency: {name}")
            seen.add(name)
            if name == "pip":  # Bootstrap tool, never included in the payload.
                continue
            if name not in pins:
                raise ValueError(f"Unpinned installed dependency: {name}")
            for line in distribution.requires or ():
                required = Requirement(line)
                if required.marker and not required.marker.evaluate({"extra": ""}):
                    continue
                target = canonicalize_name(required.name)
                if required.url or required.extras:
                    raise ValueError(f"Unreviewed dependency URL or extras: {line}")
                if target not in pins or pins[target] not in required.specifier:
                    raise ValueError(f"Missing or incompatible transitive pin: {name} requires {line}")
        if not set(pins).issubset(seen):
            raise ValueError("Pinned dependency is absent from the installed distribution inventory")
    return pins


def validate_license_review(root, pins, *, publication=False, deferral=None):
    if deferral is not None and (not publication or not isinstance(deferral, str) or
                                 not deferral.strip() or len(deferral) > 1000):
        raise ValueError("A distribution review deferral requires publication and a nonempty reason of at most 1000 characters")
    review = json.loads((root / LICENSE_REVIEW).read_text(encoding="utf-8"))
    from packaging.utils import canonicalize_name
    versions = {}
    for entry in review.get("dependencies", ()):
        name = canonicalize_name(entry["name"])
        if name in versions:
            raise ValueError(f"Duplicate license inventory dependency: {name}")
        versions[name] = entry["version"]
    if versions != pins or review.get("python") != ".".join(map(str, CANDIDATE_PYTHON)):
        raise ValueError("License inventory must match the exact runtime and dependency pins")
    if publication and deferral is None and (review.get("status") != "approved" or review.get("unresolved") != [] or not review.get("approved_by") or not review.get("evidence")):
        raise ValueError("Distribution license/runtime review is blocked; resolve and document every gate before publication")
    return review


def validate_source_input(path, root):
    relative = path.relative_to(root)
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"Escaping packaging input: {relative}")
    # Check ancestor links too: a regular file under a junction can escape.
    current = root
    for part in relative.parts:
        current = current / part
        if not current.exists() or current.is_symlink():
            raise ValueError(f"Missing or linked packaging input: {relative}")
        if getattr(current.lstat(), "st_file_attributes", 0) & 0x400:
            raise ValueError(f"Reparse-point packaging input requires review: {relative}")
    if not path.is_file():
        raise ValueError(f"Missing packaging input: {relative}")


def validate_staged_publication(stage, inputs, commit, tag):
    """Bind published bytes to the selected commit, including Git clean filters."""
    def check_revision():
        if (git("rev-parse", "HEAD") != commit or
                git("rev-parse", f"{tag}^{{commit}}") != commit or
                git("status", "--porcelain", "--untracked-files=normal")):
            raise ValueError("Publication source changed after preflight; require clean HEAD at the exact tag")

    check_revision()
    for item in inputs:
        relative = item["path"]
        # --path applies the checkout's clean filters (e.g. CRLF normalization)
        # to the copied bytes. No Git object is written without -w.
        actual = git("hash-object", f"--path={relative}", str(stage / relative))
        if actual != git("rev-parse", f"{commit}:{relative}"):
            raise ValueError(f"Staged publication input differs from committed source: {relative}")
    check_revision()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", choices=("stable", "dev"), default="dev")
    parser.add_argument("--iscc", type=Path, required=True, help="Path to pinned Inno Setup 6.7.3 ISCC.exe")
    parser.add_argument("--sqlcipher-wheel", type=Path,
                        default=ROOT / "build" / "security-qa" / "wheels" / SQLCIPHER_WHEEL,
                        help="Reviewed pinned Windows SQLCipher wheel used by this build environment")
    publication_mode = parser.add_mutually_exclusive_group()
    publication_mode.add_argument("--release", action="store_true", help="Require clean source and matching stable tag")
    publication_mode.add_argument("--dev-release", type=int, metavar="N", help="Publish vBASE_VERSION-dev.N prerelease")
    parser.add_argument("--defer-distribution-review", metavar="REASON",
                        help="Record an explicit owner decision to publish with unresolved review findings; does not approve them")
    args = parser.parse_args(argv)
    publishing = args.release or args.dev_release is not None
    try:
        environment_info = validate_environment()
        pins = validate_dependencies(complete_environment=True)
        validate_license_review(ROOT, pins, publication=publishing, deferral=args.defer_distribution_review)
        native_info = validate_native_engine(args.sqlcipher_wheel)
    except (ValueError, OSError, importlib.metadata.PackageNotFoundError) as exc:
        parser.error(str(exc))
    if not args.iscc.is_file():
        parser.error("Inno compiler not found")
    if importlib.metadata.version("pyinstaller") != "6.16.0":
        parser.error("Install the pinned requirements-release.txt")
    # ISCC's PE file version may be 0.0.0.0. Check the compiler's own constant.
    preflight = subprocess.run([str(args.iscc.resolve()), "/O-", "-"], input=
        '#if Ver != 0x06070300\n#error Use pinned Inno Setup 6.7.3\n#endif\n'
        '[Setup]\nAppName=Compiler check\nAppVersion=0\nDefaultDirName={tmp}\\CompilerCheck\n',
        text=True, capture_output=True)
    if preflight.returncode:
        parser.error("Inno compiler preflight failed: " + preflight.stdout[-1500:] + preflight.stderr[-1500:])
    from app.platform.runtime import BASE_VERSION
    commit = git("rev-parse", "HEAD")
    dirty = bool(git("status", "--porcelain", "--untracked-files=normal"))
    from app.updates.release_policy import publication
    try:
        published_label, tag, kind, asset_name, numeric = publication(
            BASE_VERSION, args.channel, commit, dirty, git,
            release=args.release, dev_release=args.dev_release)
    except (ValueError, subprocess.CalledProcessError) as exc:
        parser.error(str(exc))
    inputs = source_manifest(source_files())
    snapshot = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
    label = published_label or f"{BASE_VERSION}{'-dev' if args.channel == 'dev' else ''}+local.{snapshot[:12]}"
    workspace = ROOT / "build" / args.channel / snapshot[:12]
    if workspace.exists():
        parser.error(f"Build already exists; preserve or explicitly remove it before rebuilding: {workspace}")
    stage = workspace / "source"
    stage.mkdir(parents=True)
    for item in inputs:
        target = stage / item["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / item["path"], target)
        if hashlib.sha256(target.read_bytes()).hexdigest() != item["sha256"]:
            raise ValueError(f"Input changed while staging: {item['path']}; retry from a consistent source")
    if publishing:
        try:
            validate_staged_publication(stage, inputs, commit, tag)
        except (ValueError, subprocess.CalledProcessError) as exc:
            parser.error(str(exc))
    if dependency_pins(stage) != pins:
        raise ValueError("Dependency inputs changed while staging")
    review = validate_license_review(stage, pins, publication=publishing, deferral=args.defer_distribution_review)
    license_info = dict(status=review["status"],
                        sha256=hashlib.sha256((stage / LICENSE_REVIEW).read_bytes()).hexdigest())
    if args.defer_distribution_review is not None:
        license_info["owner_deferral"] = args.defer_distribution_review.strip()
    info = dict(channel=args.channel, version=label, base_version=BASE_VERSION, commit=commit,
                dirty=dirty, release=args.release, publication_kind=kind, publication_tag=tag,
                platform="windows", architecture="x64", source_sha256=snapshot,
                build_environment=environment_info, license_review=license_info, encrypted_storage=native_info)
    (stage / "app" / "_build_info.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    # Import only from the allowlisted tree before freezing. This catches a
    # runtime helper omitted from staging without launching UI or opening data.
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    subprocess.run([sys.executable, "-c", "import main; from app.sync.workers import run_worker; "
                    "assert run_worker('invalid', []) == 2"], cwd=stage, env=environment, check=True)
    name = "PortfolioMonitor" if args.channel == "stable" else "PortfolioMonitorDev"
    version_file = workspace / "windows-version.txt"
    from PyInstaller.utils.win32.versioninfo import (VSVersionInfo, FixedFileInfo, StringFileInfo,
                                                   StringTable, StringStruct, VarFileInfo, VarStruct)
    version_file.write_text(str(VSVersionInfo(ffi=FixedFileInfo(filevers=numeric, prodvers=numeric,
                                                               OS=0x40004, fileType=1), kids=[
        StringFileInfo([StringTable("040904B0", [StringStruct("ProductName", name),
            StringStruct("FileDescription", "Portfolio Monitor" + (" Dev" if args.channel == "dev" else "")),
            StringStruct("ProductVersion", label), StringStruct("FileVersion", label),
            StringStruct("OriginalFilename", name + ".exe")])]),
        VarFileInfo([VarStruct("Translation", [1033, 1200])])])), encoding="utf-8")
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--windowed",
               "--name", name, "--distpath", str(workspace / "bundle"),
               "--workpath", str(workspace / "pyinstaller"), "--specpath", str(workspace),
               "--version-file", str(version_file), "--icon", str(stage / "app" / "assets" / "doge.ico"),
               "--add-data", f"{stage / 'app' / '_build_info.json'};app",
               "--add-data", f"{stage / 'app' / 'assets' / 'doge.ico'};app/assets",
               "--collect-data", "tzdata"]
    for dependency in DEPENDENCIES:
        command += ["--copy-metadata", dependency]
    command += [str(stage / "main.py")]
    with (workspace / "pyinstaller.log").open("w", encoding="utf-8") as log:
        subprocess.run(command, cwd=stage, check=True, stdout=log, stderr=subprocess.STDOUT)
    bundle = workspace / "bundle" / name
    # Docs and licenses remain reviewable beside the executable.
    for doc in (*DOCUMENTS, LICENSE_REVIEW):
        shutil.copyfile(stage / doc, bundle / doc)
    payload = audit_bundle(bundle)
    native_payload = [item for item in payload if item["path"].replace("\\", "/").endswith(SQLCIPHER_EXTENSION)]
    if len(native_payload) != 1 or native_payload[0]["sha256"] != SQLCIPHER_EXTENSION_SHA256:
        raise ValueError("Frozen SQLCipher extension differs from the reviewed wheel")
    output_name = asset_name.removesuffix(".exe") if asset_name else f"{name}-{label.replace('+', '-')}-windows-x64-setup"
    output = ROOT / "dist" / args.channel
    output.mkdir(parents=True, exist_ok=True)
    installer = output / f"{output_name}.exe"
    if installer.exists():
        raise FileExistsError(installer)
    inno_args = [str(args.iscc.resolve()), f"/DChannel={args.channel}", f"/DDisplayVersion={label}",
                 f"/DNumericVersion={'.'.join(map(str, numeric))}", f"/DBundleDir={bundle}", f"/DExeName={name}",
                 f"/DOutputDir={output}", f"/DOutputName={output_name}", str(stage / "scripts" / "windows_installer.iss")]
    with (workspace / "inno.log").open("w", encoding="utf-8") as log:
        subprocess.run(inno_args, cwd=stage, check=True, stdout=log, stderr=subprocess.STDOUT)
    manifest = dict(build=info, source_files=inputs, bundle_files=payload, pinned_dependencies=pins,
                    dependencies={package: importlib.metadata.version(package) for package in DEPENDENCIES},
                    tools={"pyinstaller": importlib.metadata.version("pyinstaller"), "inno_setup": "6.7.3",
                           "iscc_sha256": hashlib.sha256(args.iscc.read_bytes()).hexdigest(),
                           "python": sys.version.split()[0]},
                    installer=dict(filename=installer.name, bytes=installer.stat().st_size,
                                   sha256=hashlib.sha256(installer.read_bytes()).hexdigest()))
    installer.with_suffix(".manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    installer.with_suffix(".sha256").write_text(f"{manifest['installer']['sha256']}  {installer.name}\n", encoding="utf-8")
    print(installer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
