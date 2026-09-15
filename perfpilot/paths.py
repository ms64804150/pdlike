"""Local data directories for the Agent."""

from __future__ import annotations

import os
from pathlib import Path

from .runtime import resource_dir

ROOT = resource_dir()
WEB_ROOT = ROOT / "web"
IOS_TOOL = ROOT / ".venv-ios" / "Scripts" / "pymobiledevice3.exe"
TIDEVICE = ROOT / ".venv-ios" / "Scripts" / "tidevice.exe"


def data_root() -> Path:
    override = os.environ.get("PERFPILOT_DATA")
    if override:
        path = Path(override)
    elif os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        path = Path(local) / "PerfPilot" if local else ROOT / "data"
    else:
        path = Path.home() / ".perfpilot"
    path.mkdir(parents=True, exist_ok=True)
    return path


def runs_root() -> Path:
    path = data_root() / "runs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def logs_root() -> Path:
    path = data_root() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path
