"""Discover connected Android / iOS devices and installed apps."""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from typing import Any

from .paths import IOS_TOOL, ROOT, TIDEVICE
from .runtime import (
    adb_cwd,
    adb_executable,
    adb_env,
    is_frozen,
    minimal_child_env,
    process_env,
    pymobiledevice3_cmd,
    subprocess_kwargs,
)
from .logutil import get_logger

log = get_logger("devices")


class ScanError(Exception):
    """Platform device scan failed; caller should keep the last good list."""


def _run(command: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    cmd = list(command)
    cwd = ROOT
    if cmd and cmd[0] == "adb":
        cmd[0] = adb_executable()
        cwd = adb_cwd() or ROOT
    log.debug("exec %s cwd=%s timeout=%s", cmd, cwd, timeout)
    try:
        env = adb_env() if str(cmd[0]).lower().endswith("adb.exe") else process_env()
    except Exception as error:
        log.exception("[Agent] env build failed cmd=%s err=%s", cmd[:4], error)
        env = minimal_child_env()
        log.warning("[Agent] env fallback short PATH cmd=%s pathChars=%s", cmd[:4], len(env.get("PATH", "")))
    result = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
        **subprocess_kwargs(),
    )
    if result.returncode != 0:
        log.warning(
            "exec failed rc=%s cmd=%s stderr=%s stdout=%s",
            result.returncode,
            cmd,
            (result.stderr or "").strip()[:500],
            (result.stdout or "").strip()[:300],
        )
    else:
        log.debug("exec ok rc=0 stdout_chars=%s", len(result.stdout or ""))
    return result


_IOS_SKIP_NATIVE = False
_IOS_SKIP_ALL = False


def ios_devices(timeout: int) -> list[dict[str, Any]]:
    global _IOS_SKIP_NATIVE, _IOS_SKIP_ALL
    if _IOS_SKIP_ALL:
        return []
    if not _IOS_SKIP_NATIVE:
        try:
            from .ios_lockdown import list_ios_devices

            native = list_ios_devices()
            if native:
                return native
        except Exception as error:
            log.warning("[IosScan] native failed err=%s frozen=%s", error, is_frozen())
            if "No module named 'pymobiledevice3'" in str(error):
                _IOS_SKIP_NATIVE = True
                log.info("[IosScan] disable native missing pymobiledevice3 tool=%s", IOS_TOOL)
                if not IOS_TOOL.is_file() and not TIDEVICE.is_file():
                    _IOS_SKIP_ALL = True
                    log.info("[IosScan] disable all missing pymobiledevice3 and tidevice")
                    return []
    found: list[dict[str, Any]] = []
    errors: list[str] = []
    pymd3 = pymobiledevice3_cmd()
    try:
        result = _run([*pymd3, "usbmux", "list"], timeout=timeout)
        if result.returncode == 0:
            for item in json.loads(result.stdout):
                found.append(
                    {
                        "id": item.get("UniqueDeviceID") or item.get("Identifier"),
                        "name": item.get("DeviceName") or "iPhone",
                        "model": item.get("ProductType"),
                        "platform": "ios",
                        "version": item.get("ProductVersion"),
                        "connection": item.get("ConnectionType", "USB"),
                        "status": "connected",
                    }
                )
            if found:
                return [item for item in found if item.get("id")]
        errors.append(result.stderr.strip() or f"usbmux list exit {result.returncode}")
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        errors.append(str(error))
    if IOS_TOOL.is_file() and pymd3 != [str(IOS_TOOL)]:
        try:
            result = _run([str(IOS_TOOL), "usbmux", "list"], timeout=timeout)
            if result.returncode == 0:
                for item in json.loads(result.stdout):
                    found.append(
                        {
                            "id": item.get("UniqueDeviceID") or item.get("Identifier"),
                            "name": item.get("DeviceName") or "iPhone",
                            "model": item.get("ProductType"),
                            "platform": "ios",
                            "version": item.get("ProductVersion"),
                            "connection": item.get("ConnectionType", "USB"),
                            "status": "connected",
                        }
                    )
                return [item for item in found if item.get("id")]
            errors.append(result.stderr.strip() or f"usbmux list exit {result.returncode}")
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            errors.append(str(error))
    if TIDEVICE.is_file():
        try:
            result = _run([str(TIDEVICE), "list"], timeout=min(timeout, 20))
            for line in result.stdout.splitlines()[1:]:
                columns = line.split()
                if len(columns) >= 5 and columns[0] != "-":
                    found.append(
                        {
                            "id": columns[0],
                            "name": columns[2],
                            "model": columns[3],
                            "platform": "ios",
                            "version": columns[4],
                            "connection": columns[5] if len(columns) > 5 else "USB",
                            "status": "connected",
                        }
                    )
            return [item for item in found if item.get("id")]
        except (OSError, subprocess.SubprocessError) as error:
            errors.append(str(error))
    if errors:
        raise ScanError("; ".join(errors))
    return [item for item in found if item.get("id")]


def android_devices(timeout: int) -> list[dict[str, Any]]:
    try:
        result = _run(["adb", "devices", "-l"], timeout=min(timeout, 10))
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        log.exception("[AndroidScan] adb devices failed err=%s pathChars=%s", error, len(os.environ.get("PATH", "")))
        raise ScanError(str(error)) from error
    if result.returncode != 0:
        raise ScanError(result.stderr.strip() or f"adb devices exit {result.returncode}")
    found: list[dict[str, Any]] = []
    for line in result.stdout.splitlines()[1:]:
        columns = line.split()
        if len(columns) >= 2 and columns[1] == "device":
            found.append(
                {
                    "id": columns[0],
                    "name": next(
                        (x.split(":", 1)[1] for x in columns[2:] if x.startswith("model:")),
                        columns[0],
                    ),
                    "model": "Android device",
                    "platform": "android",
                    "version": "Android",
                    "connection": "USB" if ":" not in columns[0] else "Wi-Fi",
                    "status": "connected",
                }
            )
    return [item for item in found if item.get("id")]


def devices(timeout: int | None = None) -> list[dict[str, Any]]:
    ios_timeout = timeout if timeout is not None else 30
    adb_timeout = timeout if timeout is not None else 10
    buckets: list[list[dict[str, Any]]] = [[], []]

    def scan_ios() -> None:
        try:
            buckets[0] = ios_devices(ios_timeout)
        except ScanError:
            buckets[0] = []

    def scan_android() -> None:
        try:
            buckets[1] = android_devices(adb_timeout)
        except ScanError:
            buckets[1] = []

    ios_thread = threading.Thread(target=scan_ios, daemon=True)
    android_thread = threading.Thread(target=scan_android, daemon=True)
    ios_thread.start()
    android_thread.start()
    ios_thread.join()
    android_thread.join()
    return [item for item in [*buckets[0], *buckets[1]] if item.get("id")]


def _ios_apps_pymd3(device_id: str) -> list[dict[str, str]]:
    try:
        result = _run([*pymobiledevice3_cmd(), "apps", "list", "--udid", device_id], timeout=45)
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    try:
        payload = json.loads(result.stdout)
    except ValueError:
        return []
    apps: list[dict[str, str]] = []
    items = payload.items() if isinstance(payload, dict) else []
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                bundle = str(item.get("CFBundleIdentifier") or item.get("bundle") or "")
                name = str(item.get("CFBundleDisplayName") or item.get("CFBundleName") or bundle)
                if bundle:
                    apps.append(_ios_app_item(bundle, name, item))
        return apps
    for bundle, item in items:
        if not isinstance(item, dict):
            apps.append({"bundle": str(bundle), "name": str(bundle), "version": ""})
            continue
        name = str(item.get("CFBundleDisplayName") or item.get("CFBundleName") or bundle)
        apps.append(_ios_app_item(str(bundle), name, item))
    return apps


_PACKAGE_HEAD = re.compile(r"^\s*Package \[([^\]]+)\]")
_VERSION_NAME = re.compile(r"\bversionName=(\S+)")
_VERSION_CODE = re.compile(r"\bversionCode=(\d+)")


def _clean_version(value: str) -> str:
    text = (value or "").strip().strip("'\"")
    if text.lower() in {"", "null", "none", "undefined"}:
        return ""
    return text


def format_app_version(version: str = "", version_code: str = "") -> str:
    name = _clean_version(version)
    code = _clean_version(version_code)
    if name and code and code not in name:
        return f"{name} ({code})"
    return name or code


def _ios_app_item(bundle: str, name: str, item: dict[str, Any]) -> dict[str, str]:
    version = _clean_version(str(item.get("CFBundleShortVersionString") or ""))
    version_code = _clean_version(str(item.get("CFBundleVersion") or ""))
    return {
        "bundle": bundle,
        "name": name or bundle,
        "version": version,
        "versionCode": version_code,
        "versionLabel": format_app_version(version, version_code),
    }


def _parse_dumpsys_packages(text: str) -> dict[str, dict[str, str]]:
    found: dict[str, dict[str, str]] = {}
    current = ""
    pending: dict[str, str] = {}

    def flush() -> None:
        if current and current not in found and (pending.get("version") or pending.get("versionCode")):
            found[current] = dict(pending)

    for raw in (text or "").replace("\r", "\n").splitlines():
        head = _PACKAGE_HEAD.match(raw)
        if head:
            flush()
            current = head.group(1).strip()
            pending = {}
            continue
        if not current:
            continue
        if "version" not in pending:
            match = _VERSION_NAME.search(raw)
            if match:
                value = _clean_version(match.group(1))
                if value:
                    pending["version"] = value
        if "versionCode" not in pending:
            match = _VERSION_CODE.search(raw)
            if match:
                pending["versionCode"] = match.group(1)
    flush()
    return found


def android_package_identity(device_id: str, bundle: str) -> dict[str, str]:
    if not device_id or not bundle:
        return {}
    try:
        result = _run(["adb", "-s", device_id, "shell", "dumpsys", "package", bundle], timeout=20)
    except subprocess.TimeoutExpired:
        log.warning("[AppVersion] dumpsys timeout serial=%s bundle=%s", device_id, bundle)
        return {}
    except (OSError, subprocess.SubprocessError) as error:
        log.warning("[AppVersion] dumpsys failed serial=%s bundle=%s err=%s", device_id, bundle, error)
        return {}
    parsed = _parse_dumpsys_packages(f"{result.stdout or ''}\n{result.stderr or ''}")
    info = parsed.get(bundle) or {}
    if not info:
        names = _VERSION_NAME.findall(result.stdout or "")
        codes = _VERSION_CODE.findall(result.stdout or "")
        if names:
            info["version"] = _clean_version(names[0])
        if codes:
            info["versionCode"] = codes[0]
    log.info(
        "[AppVersion] android bundle=%s versionName=%s versionCode=%s rc=%s",
        bundle,
        info.get("version") or "",
        info.get("versionCode") or "",
        result.returncode,
    )
    return info


def package_identity(device: dict[str, Any] | None, bundle: str) -> dict[str, str]:
    platform = str((device or {}).get("platform") or "").lower()
    device_id = str((device or {}).get("id") or "")
    if platform == "android":
        return android_package_identity(device_id, bundle)
    return {}


def _fill_android_versions(device_id: str, apps: list[dict[str, str]]) -> None:
    if not apps:
        return
    try:
        result = _run(["adb", "-s", device_id, "shell", "dumpsys", "package"], timeout=50)
    except subprocess.TimeoutExpired:
        log.warning("[AppVersion] dumpsys-all timeout serial=%s", device_id)
        return
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        log.warning("[AppVersion] dumpsys-all failed serial=%s err=%s", device_id, error)
        return
    parsed = _parse_dumpsys_packages(f"{result.stdout or ''}\n{result.stderr or ''}")
    filled = 0
    for app in apps:
        info = parsed.get(app.get("bundle") or "") or {}
        version = info.get("version") or ""
        version_code = info.get("versionCode") or ""
        if version:
            app["version"] = version
            filled += 1
        if version_code:
            app["versionCode"] = version_code
        app["versionLabel"] = format_app_version(app.get("version") or "", app.get("versionCode") or "")
    log.info(
        "[AppVersion] list fill serial=%s filled=%s/%s dumpsysChars=%s rc=%s",
        device_id,
        filled,
        len(apps),
        len(result.stdout or ""),
        result.returncode,
    )


def _parse_android_packages(text: str) -> list[dict[str, str]]:
    apps: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in (text or "").replace("\r", "\n").splitlines():
        line = raw.strip()
        if not line.startswith("package:"):
            continue
        rest = line[8:].strip()
        if "=" in rest:
            rest = rest.rsplit("=", 1)[-1].strip()
        bundle = rest.split()[0] if rest else ""
        if not bundle or bundle in seen:
            continue
        seen.add(bundle)
        apps.append({"bundle": bundle, "name": bundle, "version": ""})
    return apps


def _android_apps(device_id: str) -> dict[str, Any]:
    log.info("[AndroidApps] list start serial=%s adb=%s pathChars=%s", device_id, adb_executable(), len(os.environ.get("PATH", "")))
    try:
        state = _run(["adb", "-s", device_id, "get-state"], timeout=10)
        state_text = (state.stdout or "").strip() or (state.stderr or "").strip()
        log.info("[AndroidApps] get-state serial=%s rc=%s out=%s err=%s", device_id, state.returncode, (state.stdout or "").strip(), (state.stderr or "").strip()[:300])
        if state.returncode != 0 or state_text.splitlines()[-1:] != ["device"]:
            log.warning("[AndroidApps] device not ready serial=%s rc=%s detail=%s", device_id, state.returncode, state_text[:300])
            return {
                "applications": [],
                "error": f"adb 看不到这台设备（{state_text or 'not found'}）。请检查数据线、USB 调试授权，并在手机上点「允许」。",
            }
    except ValueError as error:
        path_chars = len(os.environ.get("PATH", ""))
        log.exception("[AndroidApps] env failed serial=%s pathChars=%s err=%s", device_id, path_chars, error)
        return {
            "applications": [],
            "error": f"本机 PATH 过长（{path_chars}），adb 无法启动。请重启 Agent。",
        }
    except (OSError, subprocess.SubprocessError) as error:
        log.warning("[AndroidApps] get-state failed serial=%s err=%s", device_id, error)
        return {"applications": [], "error": f"adb 无法连接该设备：{error}"}

    queries = (
        ["cmd", "package", "list", "packages", "-3"],
        ["pm", "list", "packages", "-3"],
        ["cmd", "package", "list", "packages"],
        ["pm", "list", "packages"],
    )
    errors: list[str] = []
    for remote in queries:
        try:
            result = _run(["adb", "-s", device_id, "shell", *remote], timeout=45)
        except subprocess.TimeoutExpired:
            message = f"timeout: adb shell {' '.join(remote)}"
            log.warning("[AndroidApps] shell timeout serial=%s cmd=%s", device_id, " ".join(remote))
            errors.append(message)
            continue
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            log.warning("[AndroidApps] shell failed serial=%s cmd=%s err=%s", device_id, remote, error)
            errors.append(str(error))
            continue
        combined = f"{result.stdout or ''}\n{result.stderr or ''}"
        apps = _parse_android_packages(combined)
        log.info(
            "[AndroidApps] packages serial=%s cmd=%s rc=%s count=%s stdoutChars=%s stderr=%s",
            device_id,
            " ".join(remote),
            result.returncode,
            len(apps),
            len(result.stdout or ""),
            (result.stderr or "").strip()[:400],
        )
        if apps:
            _fill_android_versions(device_id, apps)
            return {"applications": apps, "error": None}
        snippet = (result.stderr or result.stdout or "").strip()
        if snippet:
            errors.append(snippet.splitlines()[0][:200])
        elif result.returncode != 0:
            errors.append(f"adb shell {' '.join(remote)} exit {result.returncode}")
    detail = "; ".join(dict.fromkeys(errors)) if errors else "adb 未返回 package: 列表"
    log.warning("[AndroidApps] list empty serial=%s detail=%s", device_id, detail)
    return {"applications": [], "error": f"无法读取 Android 应用列表。{detail}"}


def applications(device_id: str, platform: str) -> dict[str, Any]:
    tag = "[IosApps]" if platform == "ios" else "[AndroidApps]"
    log.info("%s api start device=%s platform=%s", tag, device_id, platform)
    if platform == "ios":
        try:
            from .ios_lockdown import list_ios_apps

            apps, error = list_ios_apps(device_id)
            log.info("[IosApps] native count=%s error=%s", len(apps), error)
            if apps:
                return {"applications": apps, "error": None}
            cli_apps = _ios_apps_pymd3(device_id)
            if cli_apps:
                return {"applications": cli_apps, "error": None}
            from .diagnose import diagnose as collect_diagnose

            diag = collect_diagnose()
            if not diag.get("appleUsbmux"):
                error = (error + "；" if error else "") + "未检测到 Apple USB 服务，请安装 Apple Devices 或 iTunes"
            return {"applications": [], "error": error or "无法读取 iOS 应用列表"}
        except Exception as error:
            log.warning("[IosApps] list failed device=%s err=%s", device_id, error)
            return {"applications": [], "error": str(error)}
    try:
        return _android_apps(device_id)
    except Exception as error:
        log.exception("[AndroidApps] list crashed serial=%s err=%s", device_id, error)
        return {"applications": [], "error": f"读取应用列表失败：{error}"}


def logs_capability(platform: Any, bundle: str) -> dict[str, str]:
    android = str(platform or "").lower() == "android"
    package = str(bundle or "").strip()
    if android:
        source = f"需勾选后采集 · logcat 过滤包名 {package}" if package else "需勾选后采集 logcat"
        return {"id": "logs", "label": "日志", "state": "degraded", "source": source}
    return {"id": "logs", "label": "日志", "state": "degraded", "source": "iOS 无 logcat"}


def capabilities(device: dict[str, Any], bundle: str) -> dict[str, Any]:
    platform = device.get("platform")
    indicators = [
        {"id": "fps", "label": "FPS", "state": "available", "source": "View / DVT"},
        {"id": "cpu", "label": "App CPU", "state": "available", "source": "Process monitor"},
        {"id": "memory", "label": "App Memory", "state": "available", "source": "RSS / PSS"},
        {"id": "gpu", "label": "GPU", "state": "degraded" if platform == "android" else "available", "source": "Device dependent"},
    ]
    if platform == "android":
        indicators[0]["source"] = "SurfaceFlinger / gfxinfo"
        indicators[3]["source"] = "未接入 Android GPU 计数器"
    else:
        indicators[0]["source"] = "DVT graphics"
        indicators[3]["source"] = "DVT Device Utilization"
    logs_cap = logs_capability(platform, bundle)
    indicators.append(logs_cap)
    log.info(
        "[Logcat] capability device=%s package=%s state=%s source=%s",
        device.get("id"),
        bundle,
        logs_cap["state"],
        logs_cap["source"],
    )
    return {"deviceId": device.get("id"), "bundle": bundle, "indicators": indicators, "ready": True}
