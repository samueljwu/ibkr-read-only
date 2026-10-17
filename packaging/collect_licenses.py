"""Collect license texts and exact runtime versions for a desktop bundle."""

from __future__ import annotations

import json
import re
import shutil
import sys
from importlib import metadata
from pathlib import Path
from urllib.request import urlopen

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "build" / "release-licenses"
ROOT_PACKAGES = [
    "PySide6-Addons",
    "ibapi",
    "openpyxl",
    "certifi",
    "sqlcipher3",
]
if sys.platform == "win32":
    ROOT_PACKAGES.append("tzdata")
elif sys.platform == "darwin":
    ROOT_PACKAGES.append("keyring")
else:
    raise SystemExit("Release licenses can only be collected on Windows or macOS")

# These packages are distributed under the GPLv3 option, whose text is bundled
# once at the top level. Their wheels do not carry that text in dist-info.
GPL_PACKAGES = {"pyside6-addons", "pyside6-essentials", "shiboken6", "ibapi"}
LICENSE_NAME = re.compile(r"^(?:licen[cs]e|copying|notice|authors)(?:[._-]|$)", re.I)


def runtime_distributions() -> list[metadata.Distribution]:
    pending = list(ROOT_PACKAGES)
    found: dict[str, metadata.Distribution] = {}
    while pending:
        name = pending.pop()
        key = canonicalize_name(name)
        if key in found:
            continue
        dist = metadata.distribution(name)
        found[key] = dist
        for raw in dist.requires or ():
            requirement = Requirement(raw)
            if requirement.marker is None or requirement.marker.evaluate({"extra": ""}):
                pending.append(requirement.name)
    return [found[key] for key in sorted(found)]


def license_files(dist: metadata.Distribution) -> dict[str, bytes]:
    result = {}
    for item in dist.files or ():
        path = Path(str(item))
        if not any(part.endswith(".dist-info") for part in path.parts):
            continue
        if not LICENSE_NAME.match(path.name) or path.name == "LicenseRef-Qt-Commercial.txt":
            continue
        source = Path(dist.locate_file(item))
        if source.is_file():
            result[path.name] = source.read_bytes()
    for name in dist.metadata.get_all("License-File") or ():
        if name in result or name == "LicenseRef-Qt-Commercial.txt":
            continue
        content = dist.read_text(name) or dist.read_text(f"licenses/{name}")
        if content:
            result[Path(name).name] = content.encode("utf-8")
    return result


def python_license() -> bytes:
    for base in (Path(sys.prefix), Path(sys.base_prefix)):
        for name in ("LICENSE.txt", "LICENSE", "LICENSE.rst"):
            candidate = base / name
            if candidate.is_file():
                return candidate.read_bytes()
    # The macOS python.org framework used by setup-python omits LICENSE from
    # its installation. Fetch the text for the exact CPython tag used to build.
    version = ".".join(map(str, sys.version_info[:3]))
    url = f"https://raw.githubusercontent.com/python/cpython/v{version}/LICENSE"
    with urlopen(url, timeout=20) as response:
        content = response.read(256 * 1024)
    if not content.startswith(b"A. HISTORY OF THE SOFTWARE"):
        raise SystemExit(f"Unexpected Python license from {url}")
    return content


def main() -> None:
    if not OUTPUT.resolve().is_relative_to(ROOT.resolve()):
        raise SystemExit("License output must stay inside the repository")
    OUTPUT.mkdir(parents=True, exist_ok=True)
    # File providers can prevent removal of empty placeholder directories. Clearing
    # files is enough to prevent an old license from entering a new bundle.
    for old_file in OUTPUT.rglob("*"):
        if old_file.is_file():
            old_file.unlink()
    shutil.copyfile(ROOT / "LICENSE", OUTPUT / "GPL-3.0.txt")
    shutil.copyfile(ROOT / "packaging" / "licenses" / "Apache-2.0.txt", OUTPUT / "Apache-2.0.txt")
    (OUTPUT / "Python-LICENSE.txt").write_bytes(python_license())
    inventory = []
    for dist in runtime_distributions():
        name = dist.metadata["Name"]
        key = canonicalize_name(name)
        files = license_files(dist)
        if not files and key not in GPL_PACKAGES:
            raise SystemExit(f"No license text found for bundled package {name} {dist.version}")
        directory = OUTPUT / f"{key}-{dist.version}"
        directory.mkdir(exist_ok=True)
        for filename, content in sorted(files.items()):
            (directory / filename).write_bytes(content)
        inventory.append(
            {
                "name": name,
                "version": dist.version,
                "declared_license": dist.metadata.get("License-Expression")
                or dist.metadata.get("License")
                or "see included license text",
                "selected_license": "GPL-3.0" if key in GPL_PACKAGES else None,
                "license_files": [f"{directory.name}/{filename}" for filename in sorted(files)]
                or ["GPL-3.0.txt"],
            }
        )
    (OUTPUT / "inventory.json").write_text(
        json.dumps({"platform": sys.platform, "packages": inventory}, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Collected {len(inventory)} runtime packages in {OUTPUT}")


if __name__ == "__main__":
    main()
