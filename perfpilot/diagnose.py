"""Portable-layout and USB-driver checks for frozen Windows clients."""

from __future__ import annotations

import importlib
import importlib.metadata
import os
import platform
import socket
import subprocess
from pathlib import Path
from typing import Any

from .runtime import adb_cwd, adb_env, adb_executable, app_dir, is_frozen, resource_dir, subprocess_kwargs, vendor_platform_tools


def _tcp_open(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def layout_ok() -> tuple[bool, str]:
    if not is_frozen():
        return True, "source"
    # On Windows one-dir builds keep data in ``app_dir/_internal``. macOS
    # app bundles expose the matching PyInstaller data directory via
    # ``sys._MEIPASS`` (resource_dir), not beside Contents/MacOS/PerfPilot.
    internal = resource_dir()
    if not internal.is_dir():
        return False, "缺少 _internal 文件夹。请把 PerfPilot.exe 和 _internal 放在同一目录后再运行。"
    return True, str(internal)


def _adb_probe(adb: str) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            [adb, "version"],
            cwd=adb_cwd() or None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            env=adb_env(),
            **subprocess_kwargs(),
        )
        detail = (result.stdout or result.stderr or "").strip().splitlines()
        return result.returncode == 0, (detail[0] if detail else f"exit {result.returncode}")
    except (OSError, subprocess.SubprocessError) as error:
        return False, str(error)


def _module_probes() -> dict[str, dict[str, Any]]:
    probes = {}
    for name in (
        "pymobiledevice3.usbmux",
        "pymobiledevice3.lockdown",
        "pymobiledevice3.services.installation_proxy",
        "Crypto.Cipher.AES",
    ):
        try:
            importlib.import_module(name)
            probes[name] = {"ok": True, "error": None}
        except Exception as error:
            probes[name] = {"ok": False, "error": f"{type(error).__name__}: {error}"}
    return probes


def diagnose() -> dict[str, Any]:
    adb = adb_executable()
    adb_path = Path(adb)
    tools = vendor_platform_tools()
    wintun = resource_dir() / "pytun_pmd3" / "wintun" / "bin" / "amd64" / "wintun.dll"
    usbmux = _tcp_open("127.0.0.1", 27015)
    ok, layout = layout_ok()
    adb_usable, adb_version = _adb_probe(adb)
    modules = _module_probes()
    ios_runtime_ok = all(item["ok"] for item in modules.values())
    needs_wintun = os.name == "nt"
    portable_ready = ok and adb_path.is_file() and adb_usable and ios_runtime_ok and (not needs_wintun or wintun.is_file())
    try:
        importlib.import_module("sslpsk_pmd3")
        tcp_tunnel = {"ok": True, "error": None}
    except Exception as error:
        tcp_tunnel = {"ok": False, "error": f"{type(error).__name__}: {error}"}
    hints: list[str] = []
    if not ok:
        hints.append(layout)
    if is_frozen() and not adb_path.is_file():
        hints.append("未找到打包的 adb.exe。请确认 _internal\\vendor\\platform-tools 完整。")
    elif not adb_usable:
        hints.append(f"adb.exe 无法运行：{adb_version}")
    if not ios_runtime_ok:
        missing = ", ".join(name for name, item in modules.items() if not item["ok"])
        hints.append(f"iOS 运行库不完整：{missing}")
    if needs_wintun and not wintun.is_file():
        hints.append("缺少 wintun.dll，iOS 17+ 无线隧道采集可能不可用。")
    if not usbmux:
        hints.append("未检测到 Apple USB 通道。连接 iPhone 前请安装 Apple Devices 或 iTunes，解锁并点「信任」。")
    try:
        pymd3_version = importlib.metadata.version("pymobiledevice3")
    except importlib.metadata.PackageNotFoundError:
        pymd3_version = None
    log_path = os.path.join(os.environ.get("LOCALAPPDATA") or "", "PerfPilot", "logs", "agent.log")
    return {
        "frozen": is_frozen(),
        "portableReady": portable_ready,
        "layoutOk": ok,
        "layout": layout,
        "appDir": str(app_dir()),
        "resourceDir": str(resource_dir()),
        "pythonArchitecture": platform.architecture()[0],
        "adb": adb,
        "adbExists": adb_path.is_file(),
        "adbUsable": adb_usable,
        "adbVersion": adb_version,
        "adbWinApi": (tools / "AdbWinApi.dll").is_file(),
        "appleUsbmux": usbmux,
        "wintun": wintun.is_file(),
        "pymobiledevice3Version": pymd3_version,
        "pythonModules": modules,
        "iosTcpTunnel": tcp_tunnel,
        "hints": hints,
        "logFile": log_path,
    }
