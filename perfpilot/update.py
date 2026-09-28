"""GitHub Releases update discovery for the public PerfPilot distribution."""

from __future__ import annotations

import json
import os
import platform
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

from . import __version__

REPOSITORY = os.environ.get("PERFPILOT_UPDATE_REPOSITORY", "ms64804150/pdlike")
LATEST_RELEASE_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
UPDATE_CACHE_TTL_SECONDS = 10 * 60
_cache_lock = threading.Lock()
_cache_until = 0.0
_cache_result: dict[str, Any] | None = None


class UpdateDownloadError(RuntimeError):
    """The latest release cannot be downloaded safely."""


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


def open_update_download(timeout: float = 60) -> tuple[dict[str, Any], Any]:
    """Open the verified latest-release asset for streaming to the local UI."""
    result = check_for_update()
    if not result.get("ok"):
        raise UpdateDownloadError(str(result.get("error") or "无法检查更新"))
    if not result.get("available"):
        raise UpdateDownloadError("当前没有可下载的新版本")
    download_url = str(result.get("downloadUrl") or "")
    parsed = urlparse(download_url)
    expected_prefix = f"/{REPOSITORY}/releases/download/"
    if parsed.scheme != "https" or parsed.netloc.lower() != "github.com" or not parsed.path.startswith(expected_prefix):
        raise UpdateDownloadError("更新包地址无效")
    request = urllib.request.Request(
        download_url,
        headers={"User-Agent": f"PerfPilot/{__version__}", "Accept": "application/octet-stream"},
    )
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except (OSError, urllib.error.URLError) as error:
        raise UpdateDownloadError(f"下载更新包失败：{error}") from error
    return result, response


def check_for_update(timeout: float = 5) -> dict[str, Any]:
    """Return public update metadata without downloading or installing anything."""
    global _cache_result, _cache_until
    now = time.monotonic()
    with _cache_lock:
        if _cache_result is not None and now < _cache_until:
            result = dict(_cache_result)
            result["cached"] = True
            return result

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
    except urllib.error.HTTPError as error:
        if error.code == 403:
            reset = error.headers.get("X-RateLimit-Reset")
            reset_text = ""
            if reset and str(reset).isdigit():
                reset_text = f"，预计 {time.strftime('%H:%M:%S', time.localtime(int(reset)))} 后恢复"
            result = {
                "ok": False,
                "currentVersion": __version__,
                "error": f"GitHub 更新接口触发访问频率限制{reset_text}，请稍后再试",
            }
        else:
            result = {"ok": False, "currentVersion": __version__, "error": str(error)}
    except (OSError, ValueError, urllib.error.URLError) as error:
        result = {"ok": False, "currentVersion": __version__, "error": str(error)}
    else:
        tag = str(payload.get("tag_name") or "")
        asset_name = _asset_name()
        asset = next((item for item in payload.get("assets") or [] if item.get("name") == asset_name), None)
        available = bool(tag and asset and _is_newer(tag))
        digest = str((asset or {}).get("digest") or "")
        result = {
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

    with _cache_lock:
        _cache_result = dict(result)
        _cache_until = time.monotonic() + UPDATE_CACHE_TTL_SECONDS
    result["cached"] = False
    return result
