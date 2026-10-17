"""Reject bundles using a pre-GPL or third-party TWS API package."""

from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path


try:
    api = distribution("ibapi")
except PackageNotFoundError as exc:
    raise SystemExit("Release builds require official IBKR ibapi 10.49 or newer") from exc

api_version = api.version
major, minor = (int(part) for part in api_version.split(".")[:2])
if (major, minor) < (10, 49):
    raise SystemExit(f"IB API {api_version} predates its GPL release")
if api.metadata.get("License") != "GPL-3.0-or-later":
    raise SystemExit("IB API package does not declare the official GPL license")

import ibapi.client  # noqa: E402 - check the installed import target as well

if not (Path(__file__).resolve().parent.parent / "LICENSE").is_file():
    raise SystemExit("GNU GPL v3 license text is missing")
