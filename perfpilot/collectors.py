"""Wrap existing Android / iOS collector scripts as subprocess adapters."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

from .paths import ROOT


ANDROID_SAMPLE = re.compile(
    r"FPS=\s*([\d.-]+).*?AppCPU=\s*([\d.-]+)%.*?AppPSS=\s*([\d.-]+)\s*MB"
)
IOS_SAMPLE = re.compile(
    r"FPS=\s*([\d.-]+).*?AppCPU=\s*([\d.-]+)%.*?(?:Memory|AppPSS)=\s*([\d.-]+)\s*MiB.*?GPU=\s*([\d.-]+)"
)


def _optional(value: str) -> Optional[float]:
    if value in ("-", ""):
        return None
    try:
        return float(value)
    except ValueError:
        return None


STRING_KEYS = {"scene", "scriptFn", "script", "function"}
_SAMPLE_GLUED_LOGGED = False


def parse_collector_line(line: str) -> Optional[dict[str, Any]]:
    text = line.strip()
    marker = "PERFPILOT_SAMPLE"
    sample_at = text.find(marker)
    if sample_at >= 0:
        global _SAMPLE_GLUED_LOGGED
        if sample_at > 0 and not _SAMPLE_GLUED_LOGGED:
            _SAMPLE_GLUED_LOGGED = True
            from .logutil import get_logger

            get_logger("collector").info(
                "[AndroidSample] sample glued prefix=%s",
                text[:sample_at][:80],
            )
        text = text[sample_at:]
        values: dict[str, Any] = {"fps": None, "cpu": None, "memory": None, "gpu": None}
        for part in text.split("\t")[1:]:
            if "=" not in part:
                continue
            key, raw = part.split("=", 1)
            if raw in ("none", "-", ""):
                values[key] = None
            elif key in STRING_KEYS:
                values[key] = raw
            else:
                values[key] = _optional(raw)
        return values
    ios_match = IOS_SAMPLE.search(line)
    if ios_match:
        fps, cpu, memory, gpu = ios_match.groups()
        return {
            "fps": _optional(fps),
            "cpu": _optional(cpu),
            "memory": _optional(memory),
            "gpu": _optional(gpu),
        }
    android_match = ANDROID_SAMPLE.search(line)
    if android_match:
        fps, cpu, memory = android_match.groups()
        return {
            "fps": _optional(fps),
            "cpu": _optional(cpu),
            "memory": _optional(memory),
            "gpu": None,
        }
    return None


def ios_backend(version: str) -> str:
    if getattr(sys, "frozen", False):
        return "pymobiledevice3"
    try:
        parts = tuple(int(part) for part in str(version).split(".")[:2])
    except ValueError:
        return "pymobiledevice3"
    return "tidevice" if parts < (17, 0) else "pymobiledevice3"


def collector_command(
    device: dict[str, Any],
    bundle: str,
    report: Path,
    stop_file: Path,
    extras_file: Optional[Path] = None,
) -> list[str]:
    python = os.fspath(Path(sys.executable))
    if getattr(sys, "frozen", False):
        android = [python, "--collect", "android"]
        ios = [python, "--collect", "ios"]
    else:
        android = [python, os.fspath(ROOT / "adb_fps.py")]
        ios = [python, os.fspath(ROOT / "ios_perf.py")]
    if device.get("platform") == "android":
        command = [
            *android,
            "--package",
            bundle,
            "--serial",
            device["id"],
            "--interval",
            "1",
            "--report",
            os.fspath(report),
            "--stop-file",
            os.fspath(stop_file),
        ]
        if extras_file:
            command.extend(["--extras-file", os.fspath(extras_file)])
        return command
    backend = ios_backend(str(device.get("version") or "0"))
    return [
        *ios,
        "--bundle",
        bundle,
        "--udid",
        device["id"],
        "--backend",
        backend,
        "--report",
        os.fspath(report),
        "--stop-file",
        os.fspath(stop_file),
    ]


def decode_collector_line(raw: bytes | str) -> str:
    if isinstance(raw, str):
        return raw.replace("\r", "\n").strip()
    chunk = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n").strip()
    if not chunk:
        return ""
    utf8 = chunk.decode("utf-8", errors="replace")
    if "\ufffd" not in utf8:
        return utf8
    return chunk.decode("gb18030", errors="replace")


def start_collector(command: list[str]) -> subprocess.Popen[bytes]:
    from .logutil import get_logger
    from .runtime import app_dir, is_frozen, process_env

    log = get_logger("collector")
    options: dict[str, Any] = {}
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP
        options["creationflags"] = flags
    env = process_env()
    env["PERFPILOT_COLLECT"] = "1"
    log.info(
        "[Collector] spawn frozen=%s cwd=%s reset=%s cmd=%s",
        is_frozen(),
        app_dir(),
        env.get("PYINSTALLER_RESET_ENVIRONMENT"),
        " ".join(command),
    )
    return subprocess.Popen(
        command,
        cwd=os.fspath(app_dir()),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
        env=env,
        **options,
    )
