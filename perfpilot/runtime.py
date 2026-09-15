"""Resolve bundled resources when running as a frozen Windows client."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resource_dir() -> Path:
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return app_dir()
    return Path(__file__).resolve().parent.parent


def vendor_platform_tools() -> Path:
    for candidate in (
        app_dir() / "vendor" / "platform-tools",
        resource_dir() / "vendor" / "platform-tools",
    ):
        if candidate.is_dir():
            return candidate
    return resource_dir() / "vendor" / "platform-tools"


def adb_executable() -> str:
    name = "adb.exe" if os.name == "nt" else "adb"
    bundled = vendor_platform_tools() / name
    if bundled.is_file():
        return os.fspath(bundled)
    found = shutil.which("adb")
    return found or name


def adb_cwd() -> str | None:
    tools = vendor_platform_tools()
    if tools.is_dir():
        return os.fspath(tools)
    return None


def subprocess_kwargs() -> dict[str, Any]:
    options: dict[str, Any] = {}
    if os.name == "nt":
        options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return options


_environment_ready = False
_ENV_LOCK = threading.Lock()
_CHILD_ENV_KEYS = (
    "SYSTEMROOT",
    "WINDIR",
    "SYSTEMDRIVE",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "USERNAME",
    "HOMEDRIVE",
    "HOMEPATH",
    "APPDATA",
    "LOCALAPPDATA",
    "PROCESSOR_ARCHITECTURE",
    "NUMBER_OF_PROCESSORS",
    "OS",
    "HOME",
    "USER",
    "ANDROID_ADB_SERVER_PORT",
)


def _compact_path(raw: str, extra_front: list[str] | None = None) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    for item in [*(extra_front or []), *str(raw or "").split(os.pathsep)]:
        text = str(item or "").strip().strip('"')
        if not text:
            continue
        key = os.path.normcase(os.path.normpath(text))
        if key in seen:
            continue
        seen.add(key)
        parts.append(text)
    joined = os.pathsep.join(parts)
    if len(joined) <= 32000:
        return joined
    kept: list[str] = []
    size = 0
    for item in parts:
        extra = len(item) + (1 if kept else 0)
        if size + extra > 32000:
            break
        kept.append(item)
        size += extra
    return os.pathsep.join(kept)


def _minimal_path(extra_front: list[str] | None = None) -> str:
    windir = os.environ.get("WINDIR") or os.environ.get("SystemRoot") or r"C:\Windows"
    essentials = [
        os.path.join(windir, "System32"),
        windir,
        os.path.join(windir, "System32", "Wbem"),
        os.path.join(windir, "System32", "WindowsPowerShell", "v1.0"),
    ]
    return _compact_path(os.pathsep.join(essentials), extra_front)


def child_path(raw: str | None = None, extra_front: list[str] | None = None) -> str:
    compact = _compact_path(raw if raw is not None else os.environ.get("PATH", ""), extra_front)
    if len(compact) <= 32000:
        return compact
    return _minimal_path(extra_front)


def prepare_environment() -> None:
    global _environment_ready
    from .win_compat import apply as apply_win_compat

    apply_win_compat()
    with _ENV_LOCK:
        if _environment_ready:
            return
        for key, value in (
            ("PYTHONIOENCODING", "utf-8"),
            ("PYTHONUTF8", "1"),
            ("PYTHONUNBUFFERED", "1"),
            ("PYTHONNOUSERSITE", "1"),
        ):
            try:
                os.environ.setdefault(key, value)
            except ValueError as error:
                from .logutil import get_logger

                get_logger("runtime").warning("[Agent] skip process env write key=%s err=%s", key, error)
        for stream in (sys.stdout, sys.stderr):
            reconfigure = getattr(stream, "reconfigure", None)
            if reconfigure:
                try:
                    reconfigure(encoding="utf-8", errors="replace")
                except (OSError, ValueError):
                    pass
        tools = vendor_platform_tools()
        extras = [os.fspath(tools)] if tools.is_dir() else []
        raw = os.environ.get("PATH", "")
        compact = child_path(raw, extras)
        from .logutil import get_logger

        get_logger("runtime").info(
            "[Agent] path ready pid=%s processChars=%s childChars=%s extras=%s writeProcessPath=False",
            os.getpid(),
            len(raw),
            len(compact),
            extras,
        )
        _environment_ready = True


def _copy_environ() -> dict[str, str]:
    env: dict[str, str] = {}
    for key in _CHILD_ENV_KEYS:
        try:
            value = os.environ.get(key)
        except Exception:
            continue
        if value and len(value) < 8000:
            env[key] = value
    return env


def minimal_child_env() -> dict[str, str]:
    extras: list[str] = []
    tools = vendor_platform_tools()
    if tools.is_dir():
        extras.append(os.fspath(tools))
    env = _copy_environ()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONNOUSERSITE"] = "1"
    env["NO_COLOR"] = "1"
    env["TERM"] = "dumb"
    env["PATH"] = _minimal_path(extras)
    return env


def process_env() -> dict[str, str]:
    """Environment for child processes. Frozen children must reset PyInstaller state."""
    prepare_environment()
    extras: list[str] = []
    tools = vendor_platform_tools()
    if tools.is_dir():
        extras.append(os.fspath(tools))
    try:
        env = _copy_environ()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONNOUSERSITE"] = "1"
        env["NO_COLOR"] = "1"
        env["TERM"] = "dumb"
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
        env.pop("_MEIPASS2", None)
        if is_frozen():
            env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
            extras.append(os.fspath(resource_dir()))
            for key in list(env):
                if key.startswith("_PYI") or (key.startswith("PYINSTALLER_") and key != "PYINSTALLER_RESET_ENVIRONMENT"):
                    env.pop(key, None)
        env["PATH"] = child_path(os.environ.get("PATH", ""), extras)
        if len(env["PATH"]) > 32000:
            env["PATH"] = _minimal_path(extras)
        return env
    except Exception as error:
        from .logutil import get_logger

        get_logger("runtime").exception("[Agent] process_env fallback err=%s", error)
        return minimal_child_env()


def ensure_adb_server() -> None:
    from .logutil import get_logger

    log = get_logger("adb")
    adb = adb_executable()
    log.info("adb path=%s cwd=%s frozen=%s", adb, adb_cwd(), is_frozen())
    if not Path(adb).is_file() and not shutil.which(adb):
        log.warning("adb executable not found")
        return
    try:
        result = subprocess.run(
            [adb, "start-server"],
            cwd=adb_cwd() or None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            env=adb_env(),
            **subprocess_kwargs(),
        )
        log.info(
            "adb start-server rc=%s stdout=%s stderr=%s",
            result.returncode,
            (result.stdout or "").strip()[:400],
            (result.stderr or "").strip()[:400],
        )
        if result.returncode != 0:
            killed = subprocess.run(
                [adb, "kill-server"],
                cwd=adb_cwd() or None,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                env=adb_env(),
                **subprocess_kwargs(),
            )
            log.info("adb kill-server rc=%s stderr=%s", killed.returncode, (killed.stderr or "").strip()[:200])
            result = subprocess.run(
                [adb, "start-server"],
                cwd=adb_cwd() or None,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                env=adb_env(),
                **subprocess_kwargs(),
            )
            log.info("adb start-server retry rc=%s stderr=%s", result.returncode, (result.stderr or "").strip()[:400])
    except (OSError, subprocess.SubprocessError) as error:
        log.warning("adb start-server failed: %s", error)


def adb_env() -> dict[str, str]:
    env = process_env()
    env.pop("PYINSTALLER_RESET_ENVIRONMENT", None)
    return env


def pymobiledevice3_cmd() -> list[str]:
    if is_frozen():
        return [os.fspath(Path(sys.executable)), "--collect", "pymobiledevice3"]
    from .paths import IOS_TOOL

    if IOS_TOOL.is_file():
        return [os.fspath(IOS_TOOL)]
    found = shutil.which("pymobiledevice3.exe") or shutil.which("pymobiledevice3")
    if found:
        return [found]
    return [sys.executable, "-m", "pymobiledevice3"]
