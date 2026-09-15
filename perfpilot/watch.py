"""Background USB / ADB / usbmux device presence watcher."""

from __future__ import annotations

import subprocess
import threading
import time
from queue import Queue
from typing import Any

from .devices import ScanError, android_devices, ios_devices
from .runtime import adb_cwd, adb_env, adb_executable

MISS_LIMIT = 2


def _fingerprint(items: list[dict[str, Any]]) -> tuple[str, ...]:
    return tuple(sorted(str(item.get("id") or "") for item in items if item.get("id")))


class DeviceRegistry:
    def __init__(self) -> None:
        self._state_lock = threading.Lock()
        self._scan_lock = threading.Lock()
        self._devices: list[dict[str, Any]] = []
        self._ids: tuple[str, ...] = ()
        self._by_platform: dict[str, list[dict[str, Any]]] = {"ios": [], "android": []}
        self._miss: dict[str, int] = {}
        self._subscribers: list[Queue[dict[str, Any]]] = []
        self._ready = threading.Event()
        self._wakeup = threading.Event()
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._loop, name="device-watch", daemon=True).start()
        threading.Thread(target=self._track_adb, name="adb-track", daemon=True).start()

    def snapshot(self, wait: bool = True) -> list[dict[str, Any]]:
        if wait:
            self._ready.wait(timeout=20)
        with self._state_lock:
            return list(self._devices)

    def refresh(self, timeout: int = 8) -> list[dict[str, Any]]:
        self._scan(timeout, blocking=True)
        return self.snapshot(wait=False)

    def kick(self) -> None:
        self._wakeup.set()

    def subscribe(self) -> Queue[dict[str, Any]]:
        queue: Queue[dict[str, Any]] = Queue()
        self._ready.wait(timeout=20)
        with self._state_lock:
            self._subscribers.append(queue)
            queue.put(
                {
                    "type": "devices",
                    "devices": list(self._devices),
                    "added": [],
                    "removed": [],
                }
            )
        return queue

    def unsubscribe(self, queue: Queue[dict[str, Any]]) -> None:
        with self._state_lock:
            if queue in self._subscribers:
                self._subscribers.remove(queue)

    def _publish(self, payload: dict[str, Any]) -> None:
        with self._state_lock:
            subscribers = list(self._subscribers)
        for queue in subscribers:
            queue.put(payload)

    def _merge_platform(self, platform: str, found: list[dict[str, Any]]) -> list[str]:
        previous = self._by_platform.get(platform, [])
        found_ids = {str(item.get("id")) for item in found if item.get("id")}
        removed: list[str] = []
        stale: list[dict[str, Any]] = []
        for device in previous:
            device_id = str(device.get("id") or "")
            if not device_id:
                continue
            if device_id in found_ids:
                self._miss.pop(device_id, None)
                continue
            self._miss[device_id] = self._miss.get(device_id, 0) + 1
            if self._miss[device_id] >= MISS_LIMIT:
                removed.append(device_id)
                self._miss.pop(device_id, None)
            else:
                stale.append(device)
        self._by_platform[platform] = [item for item in found if item.get("id")] + stale
        return removed

    def _combined(self) -> list[dict[str, Any]]:
        combined: list[dict[str, Any]] = []
        seen: set[str] = set()
        for platform in ("ios", "android"):
            for device in self._by_platform.get(platform, []):
                device_id = str(device.get("id") or "")
                if not device_id or device_id in seen:
                    continue
                seen.add(device_id)
                combined.append(device)
        return combined

    def _scan(self, timeout: int, blocking: bool = False) -> None:
        acquired = self._scan_lock.acquire(timeout=20) if blocking else self._scan_lock.acquire(blocking=False)
        if not acquired:
            return
        try:
            removed: list[str] = []
            scanners = (("ios", ios_devices), ("android", android_devices))
            for platform, scanner in scanners:
                try:
                    found = scanner(timeout)
                except ScanError:
                    continue
                removed.extend(self._merge_platform(platform, found))
            found = self._combined()
            ids = _fingerprint(found)
            with self._state_lock:
                previous = self._ids
                changed = ids != previous or bool(removed)
                self._devices = found
                self._ids = ids
            self._ready.set()
            if changed:
                added = [item for item in ids if item not in previous]
                self._publish(
                    {
                        "type": "devices",
                        "devices": found,
                        "added": added,
                        "removed": removed,
                    }
                )
        except (OSError, ValueError, subprocess.SubprocessError):
            self._ready.set()
        finally:
            self._scan_lock.release()

    def _loop(self) -> None:
        while True:
            self._scan(8)
            self._wakeup.wait(timeout=1)
            self._wakeup.clear()

    def _track_adb(self) -> None:
        while True:
            try:
                process = subprocess.Popen(
                    [adb_executable(), "track-devices"],
                    cwd=adb_cwd() or None,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    env=adb_env(),
                )
                assert process.stdout is not None
                while process.stdout.read(1):
                    process.stdout.read(4096)
                    self.kick()
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                from .logutil import get_logger

                get_logger("watch").warning("[AndroidScan] track-devices failed err=%s", error)
            time.sleep(3)


registry = DeviceRegistry()
