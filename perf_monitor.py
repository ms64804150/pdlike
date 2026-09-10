#!/usr/bin/env python3
"""根据已连接设备选择 Android 或已验证的 iOS 性能采集器。"""

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional


ROOT = Path(__file__).resolve().parent
IOS_TOOL = ROOT / ".venv-ios" / "Scripts" / "pymobiledevice3.exe"
IOS_TIDEVICE_MAX_VERSION = (17, 0)


def parse_version(value: Any) -> Optional[tuple[int, ...]]:
    if not isinstance(value, str):
        return None
    parts = value.split(".")
    try:
        return tuple(int(part) for part in parts)
    except ValueError:
        return None


def android_devices() -> list[str]:
    result = subprocess.run(
        ["adb", "devices"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )
    if result.returncode != 0:
        return []
    devices: list[str] = []
    for line in result.stdout.splitlines()[1:]:
        columns = line.split()
        if len(columns) >= 2 and columns[1] == "device":
            devices.append(columns[0])
    return devices


def ios_devices() -> list[dict[str, Any]]:
    if not IOS_TOOL.is_file():
        return []
    result = subprocess.run(
        [str(IOS_TOOL), "usbmux", "list"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    if result.returncode != 0:
        return []
    try:
        devices = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    if not isinstance(devices, list):
        return []
    unique_devices: dict[str, dict[str, Any]] = {}
    devices_without_id: list[dict[str, Any]] = []
    for device in devices:
        if not isinstance(device, dict):
            continue
        identifier = device.get("UniqueDeviceID") or device.get("Identifier")
        if not identifier:
            devices_without_id.append(device)
            continue
        current = unique_devices.get(identifier)
        if current is None or (
            current.get("ConnectionType") != "USB" and device.get("ConnectionType") == "USB"
        ):
            unique_devices[identifier] = device
    return [*unique_devices.values(), *devices_without_id]


def choose_platform(requested: str, android: list[str], ios: list[dict[str, Any]]) -> str:
    if requested != "auto":
        return requested
    available = []
    if android:
        available.append("android")
    if ios:
        available.append("ios")
    if len(available) == 1:
        return available[0]
    if not available:
        raise RuntimeError("未发现可用设备：请检查 adb 连接或 .venv-ios 中的 pymobiledevice3")
    raise RuntimeError(
        "同时发现 Android 和 iOS 设备，请使用 --platform android 或 --platform ios 明确选择"
    )


def selected_ios(ios: list[dict[str, Any]], udid: Optional[str]) -> dict[str, Any]:
    if udid:
        for device in ios:
            identifier = device.get("UniqueDeviceID") or device.get("Identifier")
            if identifier == udid:
                return device
        raise RuntimeError(f"未找到指定 iOS 设备：{udid}")
    if len(ios) > 1:
        raise RuntimeError("检测到多台 iOS 设备，请通过 --udid 指定目标设备")
    return ios[0]


def run_collector(platform: str, args: argparse.Namespace) -> int:
    if platform == "android":
        if not args.package and args.bundle:
            args.package = args.bundle
        command = [sys.executable, str(ROOT / "adb_fps.py")]
        if args.package:
            command.extend(["--package", args.package])
        if args.serial:
            command.extend(["--serial", args.serial])
        if args.mode:
            command.extend(["--mode", args.mode])
        report = args.report or "adb_fps_report.html"
    else:
        device = selected_ios(args.ios_devices, args.udid)
        selected_udid = args.udid or device.get("UniqueDeviceID") or device.get("Identifier")
        if not selected_udid:
            raise RuntimeError("无法读取 iOS 设备 UDID")
        version = parse_version(device.get("ProductVersion"))
        if version is None:
            raise RuntimeError("无法读取 iOS 设备系统版本")
        if not args.bundle:
            raise RuntimeError("iOS 采集必须通过 --bundle 指定目标 App Bundle ID")
        backend = "tidevice" if version < IOS_TIDEVICE_MAX_VERSION else "pymobiledevice3"
        command = [sys.executable, str(ROOT / "ios_perf.py"), "--bundle", args.bundle]
        command.extend(["--udid", str(selected_udid), "--backend", backend])
        report = args.report or "ios_perf_report.html"

    command.extend(["--interval", str(args.interval), "--report", report])
    if platform == "ios" and args.duration is not None:
        command.extend(["--duration", str(args.duration)])
    if args.visualize:
        command.append("--visualize")
    return subprocess.call(command, cwd=ROOT)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="自动识别已连接设备并调用 Android 或 iOS 性能采集器"
    )
    parser.add_argument("--platform", choices=("auto", "android", "ios"), default="auto")
    parser.add_argument("--package", help="Android 目标包名")
    parser.add_argument("--bundle", help="iOS 目标 App Bundle ID")
    parser.add_argument("--serial", help="Android 设备序列号")
    parser.add_argument("--udid", help="iOS 设备 UDID")
    parser.add_argument("--mode", choices=("auto", "gfxinfo", "surface"), default="auto")
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--duration", type=float)
    parser.add_argument("--visualize", action="store_true")
    parser.add_argument("--report")
    args = parser.parse_args()
    if args.interval <= 0 or (args.duration is not None and args.duration <= 0):
        parser.error("--interval 和 --duration 必须大于 0")

    try:
        android = android_devices()
        ios = ios_devices()
        args.ios_devices = ios
        platform = choose_platform(args.platform, android, ios)
        if platform == "android" and args.serial and args.serial not in android:
            raise RuntimeError(f"未找到指定 Android 设备：{args.serial}")
        print(f"已选择 {platform} 性能采集器")
        return run_collector(platform, args)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"设备选择失败：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
