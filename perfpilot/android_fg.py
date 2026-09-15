"""Detect the Android app currently in the foreground."""

from __future__ import annotations

import re
import subprocess
import time
from typing import Optional

from .runtime import adb_cwd, adb_executable, adb_env, subprocess_kwargs

_PREFERRED_PATTERNS = (
    r"topResumedActivity[=:].*?([\w.]+)/",
    r"mResumedActivity[=:].*?([\w.]+)/",
    r"mFocusedActivity[=:].*?([\w.]+)/",
    r"ACTIVITY\s+([\w.]+)/",
    r"topActivity[=:].*?ComponentInfo\{([\w.]+)/",
    r"mCurrentFocus=.*?([\w.]+)/",
    r"mFocusedApp=.*?([\w.]+)/",
)

_SKIP = {"null", "N/A", "none", "android"}


def _adb_shell(serial: Optional[str], remote: str, timeout: int = 3) -> str:
    command = [adb_executable()]
    if serial:
        command.extend(["-s", serial])
    command.extend(["shell", remote])
    last_error: Optional[BaseException] = None
    for attempt in range(3):
        try:
            result = subprocess.run(
                command,
                cwd=adb_cwd() or None,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=adb_env(),
                **subprocess_kwargs(),
            )
        except OSError as error:
            last_error = error
            if getattr(error, "winerror", None) == 8 and attempt < 2:
                time.sleep(0.4 * (attempt + 1))
                continue
            return ""
        except subprocess.TimeoutExpired:
            return ""
        return (result.stdout or "") + "\n" + (result.stderr or "")
    if last_error is not None:
        return ""
    return ""


def _package_from_text(text: str) -> Optional[str]:
    for pattern in _PREFERRED_PATTERNS:
        match = re.search(pattern, text)
        if not match:
            continue
        package = match.group(1)
        if package in _SKIP or "/" in package:
            continue
        if package.startswith(("com.android.systemui", "com.android.launcher")):
            continue
        return package
    return None


def foreground_package(serial: Optional[str]) -> Optional[str]:
    commands = (
        "dumpsys activity activities",
        "dumpsys window",
        "dumpsys activity top",
    )
    chunks: list[str] = []
    for remote in commands:
        output = _adb_shell(serial, remote)
        if not output.strip():
            continue
        package = _package_from_text(output)
        if package:
            return package
        chunks.append(output)
    return _package_from_text("\n".join(chunks))


def foreground_info(device: dict) -> dict:
    platform = (device or {}).get("platform") or "android"
    if platform == "ios":
        return {"supported": False, "package": None, "label": None}
    serial = str((device or {}).get("id") or "")
    package = foreground_package(serial)
    return {"supported": True, "package": package, "label": package}
