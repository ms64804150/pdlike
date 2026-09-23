"""GitHub Releases update discovery for the public PerfPilot distribution."""

from __future__ import annotations

import json
import os
import platform
import re
import urllib.error
import urllib.request
from typing import Any

from . import __version__

REPOSITORY = os.environ.get("PERFPILOT_UPDATE_REPOSITORY", "ms64804150/pdlike")
LATEST_RELEASE_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"


def _version_key(value: str) -> tuple[int, ...]:
    """Parse the numeric portion of a release tag (``v1.2.3`` -> ``(1,2,3)``)."""
    numbers = re.findall(r"\d+", value or "")
    return tuple(int(item) for item in numbers) or (0,)


def _is_newer(candidate: str, current: str = __version__) -> bool:
    left, right = _version_key(candidate), _version_key(current)
    size = max(len(left), len(right))
    return left + (0,) * (size - len(left)) > right + (0,) * (size - len(right))


def _asset_name() -> str:
    if os.name == "nt":
        return "PerfPilot-portable-x64.zip"
    machine = platform.machine().lower()
    arch = "arm64" if machine in {"arm64", "aarch64"} else "x86_64"
    return f"PerfPilot-macos-{arch}.dmg"


def check_for_update(timeout: float = 5) -> dict[str, Any]:
    """Return public update metadata without downloading or installing anything."""
    request = urllib.request.Request(
        LATEST_RELEASE_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"PerfPilot/{__version__}",
            "X-GitHub-Api-Version": "2026-03-10",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError) as error:
        return {"ok": False, "currentVersion": __version__, "error": str(error)}

    tag = str(payload.get("tag_name") or "")
    asset_name = _asset_name()
    asset = next((item for item in payload.get("assets") or [] if item.get("name") == asset_name), None)
    available = bool(tag and asset and _is_newer(tag))
    digest = str((asset or {}).get("digest") or "")
    return {
        "ok": True,
        "currentVersion": __version__,
        "latestVersion": tag.lstrip("v"),
        "available": available,
        "assetName": asset_name,
        "downloadUrl": (asset or {}).get("browser_download_url"),
        "sha256": digest.removeprefix("sha256:") or None,
        "releaseNotes": str(payload.get("body") or "").strip(),
        "releaseUrl": payload.get("html_url"),
        "error": None if asset or not _is_newer(tag) else f"Release is missing {asset_name}",
    }
