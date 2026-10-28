"""Read-only Android battery and thermal telemetry, isolated from frame sampling."""

from __future__ import annotations

import math
import re
import subprocess
import threading
import time
from collections.abc import Callable
from typing import Optional


POWER_FIELDS = {
    "batteryLevelPct": ("电量", "%"),
    "batteryTemperatureC": ("电池温度", "°C"),
    "cpuTemperatureC": ("CPU 温度（最高温区）", "°C"),
    "gpuTemperatureC": ("GPU 温度（最高温区）", "°C"),
    "skinTemperatureC": ("机身温度（最高温区）", "°C"),
    "thermalStatus": ("温控等级（0–6）", ""),
    "batteryStatus": ("电池状态（Android 枚举）", ""),
    "batteryCurrentMa": ("电池电流（驱动原始方向）", "mA"),
    "batteryVoltageV": ("电池电压", "V"),
    "batteryPowerMw": ("整机电池侧功率（绝对值）", "mW"),
    "batteryDropPct": ("会话净电量下降", "百分点"),
    "dischargeEnergyMwh": ("会话放电能量（有效区间积分）", "mWh"),
    "energyCoverageSec": ("放电能量有效覆盖时长", "s"),
    "deviceEstimatedMah": ("系统估算整机耗电（自充电统计起点）", "mAh"),
    "appEstimatedMah": ("系统估算目标 UID 耗电（自充电统计起点）", "mAh"),
    "batterySampleAgeSec": ("电池数据年龄", "s"),
    "thermalSampleAgeSec": ("温控数据年龄", "s"),
    "batteryStatsAgeSec": ("耗电统计年龄", "s"),
}
BATTERY_KEYS = (
    "batteryLevelPct", "batteryTemperatureC", "batteryStatus",
    "batteryCurrentMa", "batteryVoltageV", "batteryPowerMw",
)
THERMAL_KEYS = ("cpuTemperatureC", "gpuTemperatureC", "skinTemperatureC", "thermalStatus")
STATS_KEYS = ("deviceEstimatedMah", "appEstimatedMah")
POWER_POLL_INTERVAL_SEC = 30
POWER_STALE_AFTER_SEC = 90
POWER_MAX_ENERGY_GAP_SEC = 45

# One remote shell avoids spawning an ADB process per battery node.
BATTERY_COMMAND = (
    "dumpsys battery; "
    "for n in temp current_now voltage_now; do "
    'echo "PERFPILOT_NODE_$n"; cat /sys/class/power_supply/battery/$n || echo unavailable; done'
)


def _number(raw: str) -> Optional[float]:
    try:
        value = float(raw.strip())
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def parse_battery(output: str) -> dict[str, Optional[float]]:
    result: dict[str, Optional[float]] = dict.fromkeys(BATTERY_KEYS)
    fields = dict(re.findall(r"^\s*([\w ]+):\s*(-?\d+(?:\.\d+)?)\s*$", output, re.M))
    level = _number(fields.get("level", ""))
    scale = _number(fields.get("scale", "100"))
    if level is not None and scale is not None and scale > 0 and 0 <= level <= scale:
        result["batteryLevelPct"] = level / scale * 100
    status = _number(fields.get("status", ""))
    result["batteryStatus"] = status if status in (1, 2, 3, 4, 5) else None
    nodes = {
        name: _number(raw)
        for name, raw in re.findall(r"^PERFPILOT_NODE_(\w+)\r?\n([^\r\n]*)", output, re.M)
    }
    temp = _number(fields.get("temperature", ""))
    if temp is None:
        temp = nodes.get("temp")
    if temp is not None and -500 <= temp <= 1500:
        result["batteryTemperatureC"] = temp / 10
    current = nodes.get("current_now")
    voltage = nodes.get("voltage_now")
    if current is not None:
        result["batteryCurrentMa"] = current / 1000
    # voltage_now is microvolts; dumpsys battery voltage is millivolts.
    volts = voltage / 1e6 if voltage is not None else None
    if volts is None:
        fallback = _number(fields.get("voltage", ""))
        volts = fallback / 1000 if fallback is not None else None
    if volts is not None and 0 < volts <= 30:
        result["batteryVoltageV"] = volts
        if current is not None:
            result["batteryPowerMw"] = abs(current) * volts / 1000
    return result


def parse_thermal(output: str) -> dict[str, Optional[float]]:
    result: dict[str, Optional[float]] = dict.fromkeys(THERMAL_KEYS)
    status = re.search(r"Thermal Status:\s*(\d+)", output)
    if status and 0 <= int(status[1]) <= 6:
        result["thermalStatus"] = float(status[1])
    # Only current readings, never cached readings or throttling thresholds.
    current = re.search(
        r"Current temperatures(?: from HAL)?:\s*(.*?)(?:\n\s*Current cooling devices(?: from HAL)?:|\Z)",
        output, re.S,
    )
    if not current:
        return result
    for record in re.findall(r"Temperature\{([^}]+)\}", current[1]):
        value = re.search(r"mValue=([-\d.eE+]+)", record)
        kind = re.search(r"mType=(\d+)", record)
        if not value or not kind:
            continue
        key = {0: "cpuTemperatureC", 1: "gpuTemperatureC", 3: "skinTemperatureC"}.get(int(kind[1]))
        temp = _number(value[1])
        if key and temp is not None and -50 <= temp <= 200:
            previous = result[key]
            result[key] = max(previous, temp) if previous is not None else temp
    return result


def package_uid(output: str, package: str) -> Optional[int]:
    matches = {
        int(uid) for name, uid in re.findall(r"package:(\S+)\s+uid:(\d+)", output)
        if name == package
    }
    return matches.pop() if len(matches) == 1 else None


def parse_batterystats(output: str, uid: Optional[int]) -> dict[str, Optional[float]]:
    result: dict[str, Optional[float]] = dict.fromkeys(STATS_KEYS)
    section = re.search(r"Estimated power use \(mAh\):\s*(.*?)(?:\n\s*\n|\Z)", output, re.S)
    if not section:
        return result
    total = re.search(r"Computed drain:\s*([\d.eE+-]+)", section[1])
    if total:
        value = _number(total[1])
        result["deviceEstimatedMah"] = value if value is not None and value >= 0 else None
    if uid is not None:
        user, app = divmod(uid, 100000)
        token = f"u{user}a{app - 10000}" if app >= 10000 else f"u{user}s{app}"
        match = re.search(
            rf"^\s*Uid\s+(?:{re.escape(token)}|{uid}):\s*([\d.eE+-]+)",
            section[1], re.M,
        )
        if match:
            value = _number(match[1])
            result["appEstimatedMah"] = value if value is not None and value >= 0 else None
    return result


class AndroidPowerMonitor:
    def __init__(self, shell: Callable[..., str], package: str) -> None:
        self.shell = shell
        self.package = package
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._groups: dict[str, tuple[float, dict[str, Optional[float]]]] = {}
        self._warnings: dict[str, str] = {}
        self._initial_level: Optional[float] = None
        self._previous: Optional[tuple[float, float]] = None
        self._energy = 0.0
        self._coverage = 0.0
        self._uid: Optional[int] = None
        self._thread = threading.Thread(target=self._run, name="android-power", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=21)
        if self._thread.is_alive():
            print("[AndroidPower] warning: telemetry command still stopping", flush=True)

    def _warn(self, group: str, message: str) -> None:
        if self._warnings.get(group) != message:
            print(f"[AndroidPower] {group}: {message}", flush=True)
            self._warnings[group] = message

    def _read(self, group: str, command: tuple[str, ...],
              parser: Callable[[str], dict[str, Optional[float]]]) -> None:
        try:
            values = parser(self.shell(*command))
        except (OSError, subprocess.SubprocessError, RuntimeError) as error:
            self._warn(group, f"采集不可用: {error}")
            values = {}
        else:
            if not any(value is not None for value in values.values()):
                self._warn(group, "无可读数据（设备接口不支持、权限受限或服务不可用）")
            else:
                missing = [key for key, value in values.items() if value is None]
                if missing:
                    self._warn(group, "部分指标不可用: " + ", ".join(missing))
                else:
                    self._warnings.pop(group, None)
        now = time.monotonic()
        with self._lock:
            if group == "battery":
                level = values.get("batteryLevelPct")
                if self._initial_level is None and level is not None:
                    self._initial_level = level
                values["batteryDropPct"] = (
                    self._initial_level - level
                    if self._initial_level is not None and level is not None else None
                )
                power = values.get("batteryPowerMw")
                if power is not None and values.get("batteryStatus") == 3:
                    if self._previous:
                        previous_time, previous_power = self._previous
                        seconds = now - previous_time
                        if 0 < seconds <= POWER_MAX_ENERGY_GAP_SEC:
                            self._energy += (previous_power + power) / 2 * seconds / 3600
                            self._coverage += seconds
                    self._previous = (now, power)
                else:
                    self._previous = None
                values["dischargeEnergyMwh"] = self._energy if self._coverage else None
                values["energyCoverageSec"] = self._coverage
            self._groups[group] = (now, values)

    def snapshot(self) -> dict[str, Optional[float]]:
        result: dict[str, Optional[float]] = dict.fromkeys(POWER_FIELDS)
        now = time.monotonic()
        with self._lock:
            for group, age_key in (
                ("battery", "batterySampleAgeSec"),
                ("thermal", "thermalSampleAgeSec"),
                ("stats", "batteryStatsAgeSec"),
            ):
                if group in self._groups:
                    timestamp, values = self._groups[group]
                    age = max(0.0, now - timestamp)
                    result[age_key] = age
                    if age <= POWER_STALE_AFTER_SEC:
                        result.update(values)
        return result

    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            self._read("battery", ("sh", "-c", BATTERY_COMMAND), parse_battery)
            if self._stop.is_set():
                break
            self._read("thermal", ("dumpsys", "thermalservice"), parse_thermal)
            if self._stop.is_set():
                break
            if self._uid is None:
                try:
                    self._uid = package_uid(
                        self.shell("cmd", "package", "list", "packages", "-U", "--user", "current", self.package),
                        self.package,
                    )
                except (OSError, subprocess.SubprocessError, RuntimeError) as error:
                    self._warn("uid", f"无法读取目标 UID: {error}")
                if self._uid is None:
                    self._warn("uid", "目标 UID 不可用，不归因应用耗电")
            if self._stop.is_set():
                break
            self._read(
                "stats", ("dumpsys", "batterystats", "--charged", self.package),
                lambda output: parse_batterystats(output, self._uid),
            )
            self._stop.wait(max(0.0, POWER_POLL_INTERVAL_SEC - (time.monotonic() - started)))
