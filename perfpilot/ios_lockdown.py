"""Read iOS devices and apps in-process so the frozen exe does not depend on CLI."""

from __future__ import annotations

import asyncio
import inspect
import threading
from typing import Any

from .logutil import get_logger

_LOCK = threading.RLock()
_INFO_CACHE: dict[str, dict[str, str]] = {}
_LAST_DEVICES: list[dict[str, Any]] = []


def _safe_text(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.encode("utf-8", "replace").decode("utf-8").replace("\x00", "").strip()


def _run(result: Any) -> Any:
    if not inspect.isawaitable(result):
        return result
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(result)

    done: dict[str, Any] = {}

    def worker() -> None:
        try:
            done["value"] = asyncio.run(result)
        except Exception as error:
            done["error"] = error

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(timeout=45)
    if thread.is_alive():
        raise TimeoutError("iOS 操作超时")
    if "error" in done:
        raise done["error"]
    return done.get("value")


async def _awaitable(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _close(resource: Any) -> None:
    closer = getattr(resource, "close", None)
    if closer is None:
        return
    try:
        await _awaitable(closer())
    except Exception:
        pass


async def _list_mux_devices() -> list[Any]:
    from pymobiledevice3.usbmux import list_devices

    devices = await asyncio.wait_for(_awaitable(list_devices()), timeout=8)
    return list(devices or [])


async def _open_lockdown(udid: str):
    from pymobiledevice3.lockdown import create_using_usbmux

    try:
        return await asyncio.wait_for(_awaitable(create_using_usbmux(serial=udid)), timeout=20)
    except TypeError:
        return await asyncio.wait_for(_awaitable(create_using_usbmux(udid)), timeout=20)


async def _list_ios_devices_async() -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for item in await _list_mux_devices():
        serial = _safe_text(getattr(item, "serial", "") or "")
        if not serial:
            continue
        cached = _INFO_CACHE.get(serial) or {}
        connection = _safe_text(getattr(item, "connection_type", None) or cached.get("connection") or "USB")
        found.append(
            {
                "id": serial,
                "name": cached.get("name") or "iPhone",
                "model": cached.get("model") or "iPhone",
                "platform": "ios",
                "version": cached.get("version") or "iOS",
                "connection": connection,
                "status": "connected",
            }
        )
    return found


async def _list_ios_apps_async(udid: str) -> tuple[list[dict[str, str]], str]:
    log = get_logger("ios")
    log.info("[IosApps] list start udid=%s", udid)
    try:
        from pymobiledevice3.services.installation_proxy import InstallationProxyService
    except Exception as error:
        return [], f"未打包 iOS 采集库：{error}"
    lockdown = None
    raw: Any = None
    try:
        lockdown = await _open_lockdown(udid)
        values = getattr(lockdown, "all_values", None) or {}
        if isinstance(values, dict):
            _INFO_CACHE[udid] = {
                "name": _safe_text(values.get("DeviceName") or "iPhone"),
                "model": _safe_text(values.get("ProductType") or "iPhone"),
                "version": _safe_text(values.get("ProductVersion") or "iOS"),
                "connection": "USB",
            }
        last_error: Exception | None = None
        async with InstallationProxyService(lockdown=lockdown) as service:
            for kwargs in ({"application_type": "User"}, {}):
                try:
                    raw = await asyncio.wait_for(_awaitable(service.get_apps(**kwargs)), timeout=25)
                    if raw:
                        break
                except Exception as error:
                    last_error = error
                    raw = None
        if raw is None and last_error:
            raise last_error
        if raw is None:
            return [], "无法读取 iOS 应用列表"
    except Exception as error:
        text = str(error)
        log.warning("[IosApps] list failed udid=%s err=%s", udid, text)
        if "Pairing" in text or "pair" in text.lower() or "trust" in text.lower():
            return [], "手机尚未完成配对。请在 iPhone 上点「信任」，必要时拔插数据线后重试。"
        if "Password" in text or "passcode" in text.lower():
            return [], "请先解锁 iPhone，再点信任此电脑。"
        if "timeout" in text.lower() or "Timeout" in text:
            return [], "读取 iOS 应用超时。请解锁手机、点信任后重试。"
        return [], f"无法读取 iOS 应用：{error}"
    finally:
        if lockdown is not None:
            await _close(lockdown)
    apps: list[dict[str, str]] = []
    if isinstance(raw, dict):
        for bundle, info in raw.items():
            data = info if isinstance(info, dict) else {}
            name = _safe_text(data.get("CFBundleDisplayName") or data.get("CFBundleName") or bundle)
            apps.append(
                {
                    "bundle": _safe_text(bundle),
                    "name": name or _safe_text(bundle),
                    "version": _safe_text(data.get("CFBundleShortVersionString") or ""),
                    "versionCode": _safe_text(data.get("CFBundleVersion") or ""),
                    "versionLabel": "",
                }
            )
            apps[-1]["versionLabel"] = (
                f"{apps[-1]['version']} ({apps[-1]['versionCode']})"
                if apps[-1]["version"] and apps[-1]["versionCode"] and apps[-1]["versionCode"] not in apps[-1]["version"]
                else (apps[-1]["version"] or apps[-1]["versionCode"])
            )
    apps.sort(key=lambda item: item["name"].lower())
    log.info("[IosApps] list done udid=%s count=%s", udid, len(apps))
    if not apps:
        return [], "已连上 iPhone，但没有读到用户应用。请确认已信任此电脑，并安装 Apple 设备支持（Apple Devices / iTunes）。"
    return apps, ""


def list_ios_devices() -> list[dict[str, Any]]:
    log = get_logger("ios")
    acquired = _LOCK.acquire(timeout=0.3)
    if not acquired:
        if _LAST_DEVICES:
            log.info("[IosScan] skip busy cached=%s", len(_LAST_DEVICES))
            return list(_LAST_DEVICES)
        _LOCK.acquire()
    try:
        found = _run(_list_ios_devices_async())
        _LAST_DEVICES[:] = found
        return found
    finally:
        _LOCK.release()


def list_ios_apps(udid: str) -> tuple[list[dict[str, str]], str]:
    with _LOCK:
        return _run(_list_ios_apps_async(udid))
