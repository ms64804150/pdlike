#!/usr/bin/env python3
"""通过 tidevice perf 采集 iOS 应用的 FPS、CPU、内存和网络数据。"""

import argparse
import html
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


PERF_TYPES = ("cpu", "memory", "fps", "network")


@dataclass
class PerfEvent:
    kind: str
    data: dict[str, Any]


@dataclass
class SampleRecord:
    elapsed_seconds: float
    fps: Optional[float]
    cpu_pct: Optional[float]
    memory_mb: Optional[float]
    gpu_pct: Optional[float]
    network_down_mb: Optional[float]
    network_up_mb: Optional[float]


def _child_environment() -> dict[str, str]:
    try:
        from perfpilot.runtime import is_frozen, process_env

        environment = process_env()
        if not is_frozen():
            project_root = str(Path(__file__).resolve().parent)
            python_path = environment.get("PYTHONPATH")
            environment["PYTHONPATH"] = (
                f"{project_root}{os.pathsep}{python_path}" if python_path else project_root
            )
        return environment
    except Exception:
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        environment.pop("PYTHONHOME", None)
        environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        environment["PYTHONNOUSERSITE"] = "1"
        environment["PYTHONUTF8"] = "1"
        environment["PYTHONUNBUFFERED"] = "1"
        return environment


def _parse_ios_version(raw: str | None) -> tuple[int, ...] | None:
    if not raw:
        return None
    match = re.match(r"^(\d+(?:\.\d+)*)", str(raw).strip())
    if not match:
        return None
    try:
        return tuple(int(part) for part in match.group(1).split("."))
    except ValueError:
        return None


def _cli_error_text(result: subprocess.CompletedProcess[str]) -> str:
    raw = f"{result.stderr or ''}\n{result.stdout or ''}"
    exceptions: list[str] = []
    cleaned_lines: list[str] = []
    for raw_line in raw.splitlines():
        text = re.sub(r"[│┃┌┐└┘─━╭╮╰╯]+", " ", raw_line)
        text = re.sub(r"\s+", " ", text).strip(" -")
        if not text:
            continue
        lowered = text.lower()
        if "sitecustomize" in lowered or "pythonverbose" in lowered:
            continue
        if "traceback" in lowered or text.startswith("in ") or "during handling of" in lowered:
            continue
        cleaned_lines.append(text)
        if re.search(r"(Error|Exception|Warning):", text):
            exceptions.append(text)
    if exceptions:
        last = exceptions[-1]
        if "Failed to load dynlib" in last or "PyInstallerImportError" in last:
            for item in cleaned_lines:
                if ".pyd" in item.lower() or ".dll" in item.lower():
                    last = item
                    break
            return f"打包缺少原生库：{last}"
        if "pyimg4" in last.lower() and "metadata" in last.lower():
            return "打包缺少 pyimg4 元数据，无法挂载 iOS Developer Image。请使用最新 PerfPilot 安装包。"
        return last
    return "\n".join(cleaned_lines[-8:])


def optional_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_tidevice_cpu(data: dict[str, Any]) -> Optional[float]:
    """Convert tidevice's process CPU ratio to total-device CPU percent."""
    value = optional_float(data.get("value"))
    cpu_count = optional_float(data.get("count"))
    if value is None:
        return None
    if cpu_count is None or cpu_count <= 0:
        return value * 100.0
    # Normal sysmontap values are ratios whose maximum is approximately CPUCount.
    if value <= cpu_count * 1.5:
        return value / cpu_count * 100.0
    # Some tidevice/device combinations expose a per-core value already in percent.
    return value / cpu_count


def normalize_pymobiledevice_cpu(data: dict[str, Any]) -> Optional[float]:
    """Convert pymobiledevice3's per-core CPU percent to device CPU percent."""
    value = optional_float(data.get("value"))
    cpu_count = optional_float(data.get("count"))
    if value is None:
        return None
    if cpu_count is None or cpu_count <= 0:
        return min(value, 100.0)
    return value / cpu_count


def normalize_cpu_event(data: dict[str, Any]) -> Optional[float]:
    if data.get("unit") == "per_core_percent":
        return normalize_pymobiledevice_cpu(data)
    return normalize_tidevice_cpu(data)


def parse_perf_line(line: str) -> Optional[PerfEvent]:
    """解析 tidevice 多指标的 ``type {json}`` 输出。"""
    text = line.strip()
    if not text:
        return None

    kind = ""
    payload = text
    if not text.startswith("{"):
        kind, separator, payload = text.partition(" ")
        if not separator or kind not in PERF_TYPES:
            return None

    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None

    if not kind:
        if "fps" in data:
            kind = "fps"
        elif "rss_value" in data:
            kind = "memory"
        elif "sys_value" in data:
            kind = "cpu"
        elif any(key in data for key in ("rx.bytes", "tx.bytes", "downFlow", "upFlow")):
            kind = "network"
        else:
            return None
    return PerfEvent(kind, data)


class TidevicePerf:
    def __init__(self, bundle_id: str, udid: Optional[str]) -> None:
        self.bundle_id = bundle_id
        self.udid = udid
        self.process: Optional[subprocess.Popen[str]] = None
        self.events: queue.Queue[PerfEvent] = queue.Queue()
        self.errors: queue.Queue[str] = queue.Queue()
        self.reader: Optional[threading.Thread] = None
        self.error_reader: Optional[threading.Thread] = None
        self.error_lines: list[str] = []

    @staticmethod
    def _find_executable() -> str:
        candidates = [
            Path(__file__).resolve().parent / ".venv-ios" / "Scripts" / "tidevice.exe",
            Path(sys.executable).with_name("tidevice.exe"),
        ]
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
        executable = shutil.which("tidevice")
        if executable:
            return executable
        raise RuntimeError("未找到 tidevice，请先安装 tidevice 到项目虚拟环境")

    @staticmethod
    def _process_environment() -> dict[str, str]:
        return _child_environment()

    def start(self) -> None:
        executable = self._find_executable()

        command = [executable]
        if self.udid:
            command.extend(["-u", self.udid])
        environment = self._process_environment()
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["PYTHONUNBUFFERED"] = "1"
        app_info = subprocess.run(
            [*command, "appinfo", self.bundle_id, "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            env=environment,
        )
        if app_info.returncode != 0:
            detail = app_info.stderr.strip()
            raise RuntimeError(
                detail
                or f"设备上未找到 Bundle ID：{self.bundle_id}；请通过 tidevice applist 确认"
            )
        command.extend(
            ["perf", "-B", self.bundle_id, "-o", ",".join(PERF_TYPES), "--json"]
        )
        self.process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=environment,
        )
        self.reader = threading.Thread(target=self._read_output, daemon=True)
        self.error_reader = threading.Thread(target=self._read_errors, daemon=True)
        self.reader.start()
        self.error_reader.start()

    def _read_output(self) -> None:
        assert self.process is not None
        assert self.process.stdout is not None
        for line in self.process.stdout:
            event = parse_perf_line(line)
            if event:
                self.events.put(event)
            elif line.strip():
                self.errors.put(line.strip())

    def _read_errors(self) -> None:
        assert self.process is not None
        assert self.process.stderr is not None
        for line in self.process.stderr:
            if line.strip():
                self.errors.put(line.strip())

    def _drain_errors(self) -> None:
        while True:
            try:
                self.error_lines.append(self.errors.get_nowait())
            except queue.Empty:
                break
        if len(self.error_lines) > 30:
            self.error_lines = self.error_lines[-30:]

    def diagnostic_tail(self) -> str:
        self._drain_errors()
        return "\n".join(self.error_lines[-8:])

    def drain_events(self) -> list[PerfEvent]:
        events: list[PerfEvent] = []
        while True:
            try:
                events.append(self.events.get_nowait())
            except queue.Empty:
                return events

    def failure_message(self) -> Optional[str]:
        self._drain_errors()
        details = "\n".join(self.error_lines)
        if "DeveloperImage not found" in details:
            return (
                "tidevice 无法找到或挂载当前 iOS 版本的 Developer Image。"
                "当前 tidevice 版本可能不支持该 iOS 系统。\n"
                + self.diagnostic_tail()
            )
        if "DevicePair required lib" in details:
            return "tidevice 缺少设备配对依赖：pyOpenSSL、pyasn1。\n" + self.diagnostic_tail()
        if self.process is None or self.process.poll() is None:
            return None
        detail = self.diagnostic_tail()
        return detail or f"tidevice perf 已退出，退出码 {self.process.returncode}"

    def stop(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=2)


class Pymobiledevice3Perf:
    def __init__(
        self,
        bundle_id: str,
        udid: Optional[str],
        rsd_host: Optional[str] = None,
        rsd_port: Optional[int] = None,
    ) -> None:
        self.bundle_id = bundle_id
        self.udid = udid
        self.rsd_host = rsd_host
        self.rsd_port = rsd_port
        self.events: queue.Queue[PerfEvent] = queue.Queue()
        self.errors: queue.Queue[str] = queue.Queue()
        self.error_lines: list[str] = []
        self.processes: list[subprocess.Popen[str]] = []
        self.cli = self._find_cli()
        self.tunnel_device: Optional[str] = None
        self.cpu_count: Optional[int] = None
        self.ios_version: Optional[tuple[int, ...]] = None

    @staticmethod
    def _find_cli() -> list[str]:
        try:
            from perfpilot.runtime import pymobiledevice3_cmd

            return pymobiledevice3_cmd()
        except Exception:
            pass
        names = ("pymobiledevice3.exe", "pymobiledevice3")
        candidates = [Path(sys.executable).with_name(names[0])]
        candidates.append(Path(__file__).parent / ".venv-ios" / "Scripts" / names[0])
        for candidate in candidates:
            if candidate.is_file():
                return [str(candidate)]
        for name in names:
            executable = shutil.which(name)
            if executable:
                return [executable]
        raise RuntimeError(
            "未找到 pymobiledevice3；请使用 64 位 Python 安装：pip install pymobiledevice3"
        )

    def _device_args(self) -> list[str]:
        return ["--udid", self.udid] if self.udid else []

    @staticmethod
    def _process_environment() -> dict[str, str]:
        return _child_environment()

    def _device_details(self) -> Optional[tuple[str, tuple[int, ...]]]:
        result = subprocess.run(
            [ *self.cli, "usbmux", "list"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            env=self._process_environment(),
        )
        if result.returncode != 0:
            return None
        try:
            devices = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None
        for device in devices if isinstance(devices, list) else []:
            if not isinstance(device, dict):
                continue
            identifier = device.get("UniqueDeviceID") or device.get("Identifier")
            if self.udid and identifier != self.udid:
                continue
            version = device.get("ProductVersion")
            parsed = _parse_ios_version(version if isinstance(version, str) else None)
            if parsed:
                return (str(identifier), parsed)
        return None

    def _device_version(self) -> Optional[tuple[int, ...]]:
        if self.ios_version is None:
            details = self._device_details()
            if details:
                self.udid = self.udid or details[0]
                self.ios_version = details[1]
        return self.ios_version

    def _needs_admin_tunnel(self) -> bool:
        version = self._device_version()
        return version is not None and (17, 0) <= version < (17, 4)

    def _needs_userspace(self) -> bool:
        version = self._device_version()
        return version is not None and version >= (17, 0)

    @staticmethod
    def _tunneld_available() -> bool:
        try:
            with socket.create_connection(("127.0.0.1", 49151), timeout=0.3):
                return True
        except OSError:
            return False

    def _tunneld_has_device(self) -> bool:
        if not self._tunneld_available():
            return False
        try:
            with urllib.request.urlopen("http://127.0.0.1:49151", timeout=1) as response:
                devices = json.load(response)
        except (OSError, ValueError, urllib.error.URLError):
            return False
        if not isinstance(devices, dict):
            return False
        if self.udid:
            return bool(devices.get(self.udid))
        return bool(devices)

    def _ensure_legacy_tunnel(self) -> None:
        if self.rsd_host:
            return
        version = self._device_version()
        print(f"[IosPerf] device version={version} udid={self.udid}", flush=True)
        if not self._needs_admin_tunnel():
            print("[IosPerf] skip admin tunneld, not iOS 17.0-17.3", flush=True)
            return

        if self._tunneld_has_device():
            self.tunnel_device = self.udid
            return

        if os.name == "nt":
            file_path = str(Path(self.cli[0]).resolve()).replace("'", "''")
            rest = " ".join([*self.cli[1:], "remote", "tunneld", "--protocol", "tcp"])
            elevated = (
                "Start-Process -FilePath 'powershell.exe' "
                "-ArgumentList @('-NoProfile','-NoExit','-Command', "
                f'"& \'{file_path}\' {rest}") '
                "-Verb RunAs"
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", elevated],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                env=self._process_environment(),
            )
        else:
            command = [*self.cli, "remote", "tunneld", "--daemonize", "--protocol", "tcp"]
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=30, env=self._process_environment()
            )
        deadline = time.monotonic() + 15
        while not self._tunneld_has_device() and time.monotonic() < deadline:
            time.sleep(0.5)
        if result.returncode != 0 and not self._tunneld_has_device():
            detail = _cli_error_text(result)
            raise RuntimeError(
                "iOS 17.4 以下版本需要管理员权限启动 remote tunneld；"
                "请接受 UAC 提示后重试。"
                + (f"\n{detail}" if detail else "")
            )
        if not self._tunneld_has_device():
            raise RuntimeError(
                "remote tunneld 已监听 49151，但未建立目标设备隧道；"
                "请确认已接受 UAC 提示，并保持管理员隧道窗口运行。"
            )
        self.tunnel_device = self.udid

    def _device_options(self, userspace: bool = False) -> list[str]:
        if self.rsd_host and self.rsd_port:
            return ["--rsd", self.rsd_host, str(self.rsd_port)]
        if self.tunnel_device:
            return ["--tunnel", self.tunnel_device]
        options = ["--userspace"] if userspace or self._needs_userspace() else []
        options.extend(self._device_args())
        return options

    def _run_check(self, arguments: list[str], timeout: int = 60) -> subprocess.CompletedProcess[str]:
        command = [*self.cli, *arguments, *self._device_args()]
        print(f"[IosPerf] cli start cmd={' '.join(command)} timeout={timeout}", flush=True)
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=self._process_environment(),
        )
        err = _cli_error_text(result)
        print(
            f"[IosPerf] cli done cmd={arguments[0] if arguments else ''} rc={result.returncode} err={err[:400]!r}",
            flush=True,
        )
        return result

    def start(self) -> None:
        started = time.monotonic()
        print(f"[IosPerf] start bundle={self.bundle_id} udid={self.udid} cli={' '.join(self.cli)}", flush=True)
        self._ensure_legacy_tunnel()
        version = self._device_version()
        if version is not None and version < (17, 0):
            print("检查 iOS Developer Disk Image...", flush=True)
            mount = self._run_check(["mounter", "auto-mount"], timeout=180)
            if mount.returncode != 0:
                err = _cli_error_text(mount)
                lowered = err.lower()
                if "already mounted" in lowered or "alreadymounted" in lowered:
                    print("[IosPerf] developer image already mounted", flush=True)
                else:
                    raise RuntimeError(err or "Developer Disk Image 挂载失败")
        else:
            print(f"[IosPerf] skip apps-query/mounter/cpuCount version={version}", flush=True)

        self.cpu_count = None
        graphics_command = [
            *self.cli,
            "developer",
            "dvt",
            "graphics",
            *self._device_options(),
        ]
        print(
            f"[IosPerf] spawn graphics elapsed={time.monotonic() - started:.1f}s cmd={' '.join(graphics_command)}",
            flush=True,
        )
        graphics = self._start_process(graphics_command)
        threading.Thread(target=self._read_graphics, args=(graphics,), daemon=True).start()
        threading.Thread(target=self._read_errors, args=(graphics,), daemon=True).start()

        pid_result = self._run_check(
            [
                "developer",
                "dvt",
                "process-id-for-bundle-id",
                self.bundle_id,
                *self._device_options(),
            ],
            timeout=60,
        )
        pid_match = re.search(r"(?m)^\s*(\d+)\s*$", pid_result.stdout)
        if pid_result.returncode != 0 or not pid_match:
            try:
                graphics.kill()
            except OSError:
                pass
            detail = _cli_error_text(pid_result)
            if self.tunnel_device and "Device is not connected" in detail:
                raise RuntimeError(
                    "本地 remote tunneld 服务已启动，但没有建立设备隧道。"
                    "请关闭当前 tunneld，并在管理员 PowerShell 中重新启动：\n"
                    f"{' '.join(self.cli)} remote tunneld --protocol tcp\n"
                    "保持该窗口运行后，再启动本脚本。"
                )
            if "no-root userspace tunnel unavailable" in detail:
                raise RuntimeError(
                    "无法建立 iOS 用户态隧道。当前设备为 iOS 17.0，"
                    "不支持 CoreDeviceProxy；请使用管理员 PowerShell 先启动：\n"
                    f"{' '.join(self.cli)} remote tunneld --daemonize --protocol tcp\n"
                    "启动后重新运行本脚本。也可以让 iPhone 与电脑连接同一 Wi-Fi，"
                    "并确保 Bonjour/RemotePairing 可用。"
                )
            raise RuntimeError(detail or f"目标 App 未运行：{self.bundle_id}")
        pid = pid_match.group(1)
        process_command = [
            *self.cli,
            "developer",
            "dvt",
            "sysmon",
            "process",
            "monitor",
            "process",
            "--filter",
            f"pid={pid}",
            "--choose",
            "last",
            "--interval",
            "500",
            "--key",
            "cpuUsage",
            "--key",
            "physFootprint",
            "--key",
            "pid",
            *self._device_options(),
        ]
        print(
            f"[IosPerf] spawn sysmon elapsed={time.monotonic() - started:.1f}s pid={pid}",
            flush=True,
        )
        sysmon = self._start_process(process_command)
        self.processes = [graphics, sysmon]
        threading.Thread(target=self._read_process, args=(sysmon,), daemon=True).start()
        threading.Thread(target=self._read_errors, args=(sysmon,), daemon=True).start()
        print(f"[IosPerf] start ready elapsed={time.monotonic() - started:.1f}s pid={pid}", flush=True)

    @staticmethod
    def _start_process(command: list[str]) -> subprocess.Popen[str]:
        environment = Pymobiledevice3Perf._process_environment()
        environment["PYTHONUNBUFFERED"] = "1"
        environment["PYTHONIOENCODING"] = "utf-8"
        return subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=environment,
        )

    def _read_cpu_count(self) -> Optional[int]:
        result = subprocess.run(
            [
                *self.cli,
                "developer",
                "dvt",
                "sysmon",
                "system",
                "--fields",
                "CPUCount",
                *self._device_options(),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            env=self._process_environment(),
        )
        if result.returncode != 0:
            return None
        match = re.search(r"(?m)^CPUCount:\s*(\d+)\s*$", result.stdout)
        return int(match.group(1)) if match else None

    def _read_graphics(self, process: subprocess.Popen[str]) -> None:
        assert process.stdout is not None
        first_sample = True
        for data in self._iter_json(process.stdout):
            fps = optional_float(data.get("CoreAnimationFramesPerSecond"))
            gpu = optional_float(data.get("Device Utilization %"))
            if fps is not None and not (first_sample and fps == 0):
                self.events.put(PerfEvent("fps", {"value": fps}))
            if gpu is not None:
                self.events.put(PerfEvent("gpu", {"value": gpu}))
            first_sample = False

    def _read_process(self, process: subprocess.Popen[str]) -> None:
        assert process.stdout is not None
        for data in self._iter_json(process.stdout):
            cpu = optional_float(data.get("cpuUsage"))
            memory_bytes = optional_float(data.get("physFootprint"))
            if cpu is not None:
                self.events.put(
                    PerfEvent(
                        "cpu",
                        {"value": cpu, "count": self.cpu_count, "unit": "per_core_percent"},
                    )
                )
            if memory_bytes is not None:
                self.events.put(PerfEvent("memory", {"value": memory_bytes / 1024 / 1024}))

    @staticmethod
    def _iter_json(stream: Any):
        buffer: list[str] = []
        for line in stream:
            text = line.strip()
            if not buffer and not text.startswith("{"):
                continue
            buffer.append(text)
            try:
                data = json.loads("\n".join(buffer))
            except json.JSONDecodeError:
                continue
            buffer.clear()
            if isinstance(data, dict):
                yield data

    def _read_errors(self, process: subprocess.Popen[str]) -> None:
        assert process.stderr is not None
        for line in process.stderr:
            if line.strip() and "Trying again over a no-root userspace tunnel" not in line:
                self.errors.put(line.strip())

    def _drain_errors(self) -> None:
        while True:
            try:
                self.error_lines.append(self.errors.get_nowait())
            except queue.Empty:
                break
        self.error_lines = self.error_lines[-30:]

    def drain_events(self) -> list[PerfEvent]:
        events: list[PerfEvent] = []
        while True:
            try:
                events.append(self.events.get_nowait())
            except queue.Empty:
                return events

    def diagnostic_tail(self) -> str:
        self._drain_errors()
        return "\n".join(self.error_lines[-8:])

    def failure_message(self) -> Optional[str]:
        self._drain_errors()
        failed = next(
            (
                process
                for process in self.processes
                if process.poll() is not None and process.returncode != 0
            ),
            None,
        )
        if failed is None:
            return None
        return self.diagnostic_tail() or f"pymobiledevice3 采集进程已退出，退出码 {failed.returncode}"

    def stop(self) -> None:
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
        for process in self.processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)


class LiveCharts:
    COLORS = {
        "fps": "#0f766e",
        "cpu_pct": "#c2410c",
        "memory_mb": "#2563eb",
        "gpu_pct": "#a16207",
    }

    def __init__(self, bundle_id: str) -> None:
        try:
            import tkinter as tk
        except ImportError as error:
            raise RuntimeError("当前 Python 未提供 tkinter，无法启用 --visualize") from error
        self.tk = tk
        self.root = tk.Tk()
        self.root.title(f"iOS 性能监控 - {bundle_id}")
        self.root.geometry("900x620")
        self.labels = tk.Label(self.root, anchor="w", justify="left", padx=12, pady=8)
        self.labels.pack(fill="x")
        self.canvas = tk.Canvas(self.root, background="#f8fafc", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.closed = False
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.root.destroy()

    def update(self, records: list[SampleRecord]) -> None:
        if self.closed:
            return
        try:
            self.root.update_idletasks()
            self.root.update()
        except self.tk.TclError:
            self.closed = True
            return
        width = max(self.canvas.winfo_width(), 400)
        height = max(self.canvas.winfo_height(), 300)
        self.canvas.delete("all")
        self.canvas.create_text(
            12,
            12,
            anchor="nw",
            text="实时趋势（绿色 FPS / 橙色 CPU / 蓝色内存 / 黄色 GPU）",
            fill="#334155",
        )
        if not records:
            return
        chart_top, chart_bottom = 38, height - 16
        values = [
            value
            for record in records
            for value in (record.fps, record.cpu_pct, record.memory_mb, record.gpu_pct)
            if value is not None
        ]
        maximum = max(values, default=1.0)
        points_by_color: dict[str, list[float]] = {
            color: [] for color in self.COLORS.values()
        }
        for index, record in enumerate(records):
            x = 12 + index / max(len(records) - 1, 1) * (width - 24)
            for field, color in self.COLORS.items():
                value = getattr(record, field)
                if value is not None:
                    y = chart_bottom - value / maximum * (chart_bottom - chart_top)
                    points_by_color[color].extend((x, y))
        for color, points in points_by_color.items():
            if len(points) >= 4:
                self.canvas.create_line(*points, fill=color, width=2, smooth=True)

        latest = records[-1]
        self.labels.configure(
            text=(
                f"FPS {format_value(latest.fps)}   CPU {format_value(latest.cpu_pct, '%')}   "
                f"Memory {format_value(latest.memory_mb, ' MB')}   "
                f"GPU {format_value(latest.gpu_pct, '%')}   "
                f"Down {format_value(latest.network_down_mb, ' MB')}   "
                f"Up {format_value(latest.network_up_mb, ' MB')}"
            )
        )


def format_value(value: Optional[float], unit: str = "") -> str:
    return f"{value:.1f}{unit}" if value is not None else "-"


def network_megabytes(data: dict[str, Any], direction: str) -> Optional[float]:
    flow_key = "downFlow" if direction == "down" else "upFlow"
    byte_key = "rx.bytes" if direction == "down" else "tx.bytes"
    flow = optional_float(data.get(flow_key))
    if flow is not None:
        return flow / 1024.0
    byte_count = optional_float(data.get(byte_key))
    return byte_count / 1024.0 / 1024.0 if byte_count is not None else None


def average(records: list[SampleRecord], field: str) -> float:
    values = [getattr(record, field) for record in records]
    valid = [value for value in values if value is not None]
    return sum(valid) / len(valid) if valid else 0.0


def peak(records: list[SampleRecord], field: str) -> float:
    values = [getattr(record, field) for record in records]
    return max((value for value in values if value is not None), default=0.0)


def percentage_matching(
    records: list[SampleRecord], field: str, predicate: Any
) -> float:
    values = [getattr(record, field) for record in records]
    valid = [value for value in values if value is not None]
    return sum(predicate(value) for value in valid) / len(valid) * 100 if valid else 0.0


def write_report(path: str, bundle_id: str, udid: Optional[str], records: list[SampleRecord]) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    def chart_svg(title: str, field: str, color: str, unit: str) -> str:
        width, height = 860, 220
        values = [getattr(record, field) for record in records]
        numeric_values = [value for value in values if value is not None]
        if not numeric_values:
            points = ""
            scale_text = "无有效数据"
        else:
            maximum = max(max(numeric_values), 1.0)
            points = " ".join(
                f"{index / max(len(values) - 1, 1) * width:.1f},"
                f"{height - 20 - value / maximum * (height - 45):.1f}"
                for index, value in enumerate(values)
                if value is not None
            )
            scale_text = f"0 - {maximum:.1f}{unit}"
        return (
            f"<section><h2>{html.escape(title)}</h2>"
            f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(title)}">'
            '<rect width="100%" height="100%" fill="#f8fafc" />'
            f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="3" />'
            f'<text x="12" y="20" fill="#475569">{html.escape(scale_text)}</text>'
            "</svg></section>"
        )

    rows = "".join(
        "<tr>"
        f"<td>{record.elapsed_seconds:.1f}</td>"
        f"<td>{format_value(record.fps)}</td>"
        f"<td>{format_value(record.cpu_pct)}</td>"
        f"<td>{format_value(record.memory_mb)}</td>"
        f"<td>{format_value(record.gpu_pct)}</td>"
        f"<td>{format_value(record.network_down_mb)}</td>"
        f"<td>{format_value(record.network_up_mb)}</td>"
        "</tr>"
        for record in records
    )
    fps_ge18 = percentage_matching(records, "fps", lambda value: value >= 18)
    fps_ge25 = percentage_matching(records, "fps", lambda value: value >= 25)
    cpu_le60 = percentage_matching(records, "cpu_pct", lambda value: value <= 60)
    duration = records[-1].elapsed_seconds if records else 0.0
    device = html.escape(udid or "自动选择")
    document = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>iOS 性能检测报告</title>
<style>body{{font:14px system-ui,sans-serif;color:#172033;max-width:1000px;margin:32px auto;padding:0 20px}}h1{{margin-bottom:4px}}section{{margin:24px 0}}svg{{width:100%;border:1px solid #dbe3ee;border-radius:8px}}table{{border-collapse:collapse;width:100%}}th,td{{border-bottom:1px solid #e2e8f0;padding:7px;text-align:right}}th{{background:#f1f5f9}}.summary{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}}.metric{{background:#f8fafc;border:1px solid #e2e8f0;padding:12px;border-radius:8px}}.metric b{{display:block;font-size:20px;margin-top:5px}}</style></head>
<body><h1>iOS 性能检测报告</h1><p>应用：{html.escape(bundle_id)}　设备：{device}　生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
<div class="summary"><div class="metric">平均 FPS<b>{average(records, 'fps'):.1f}</b></div><div class="metric">FPS>=18 [%]<b>{fps_ge18:.1f}%</b></div><div class="metric">FPS>=25 [%]<b>{fps_ge25:.1f}%</b></div><div class="metric">Avg(AppCPU) [%]<b>{average(records, 'cpu_pct'):.1f}%</b></div><div class="metric">AppCPU<=60% [%]<b>{cpu_le60:.1f}%</b></div><div class="metric">峰值 CPU<b>{peak(records, 'cpu_pct'):.1f}%</b></div><div class="metric">平均 GPU<b>{average(records, 'gpu_pct'):.1f}%</b></div><div class="metric">峰值 GPU<b>{peak(records, 'gpu_pct'):.1f}%</b></div><div class="metric">Avg(Memory) [MiB]<b>{average(records, 'memory_mb'):.1f} MiB</b></div><div class="metric">Peak(Memory) [MiB]<b>{peak(records, 'memory_mb'):.1f} MiB</b></div><div class="metric">检测时长<b>{duration:.1f}s</b></div></div>
{chart_svg('FPS 趋势', 'fps', '#0f766e', '')}{chart_svg('App CPU 趋势', 'cpu_pct', '#c2410c', '%')}{chart_svg('Memory Footprint 趋势', 'memory_mb', '#2563eb', ' MiB')}{chart_svg('GPU Device Utilization 趋势', 'gpu_pct', '#a16207', '%')}
<h2>采样明细</h2><table><thead><tr><th>时间(s)</th><th>FPS</th><th>CPU(%)</th><th>Memory(MiB)</th><th>GPU(%)</th><th>下载(MB)</th><th>上传(MB)</th></tr></thead><tbody>{rows}</tbody></table>
<p>说明：各指标异步产生，本报告按采样间隔记录最近值；iOS App CPU 按设备 CPU 核数归一化为总 CPU 百分比，Memory Footprint 不等同于 Android PSS。pymobiledevice3 后端暂不提供 App 网络流量。</p></body></html>"""
    output_path.write_text(document, encoding="utf-8")


def run(args: argparse.Namespace) -> int:
    backend = args.backend
    if getattr(sys, "frozen", False) and backend == "tidevice":
        print("[IosPerf] frozen has no tidevice, backend=pymobiledevice3", flush=True)
        backend = "pymobiledevice3"
    if backend == "tidevice":
        collector = TidevicePerf(args.bundle, args.udid)
    elif backend == "pymobiledevice3":
        collector = Pymobiledevice3Perf(
            args.bundle, args.udid, args.rsd_host, args.rsd_port
        )
    else:
        try:
            collector = Pymobiledevice3Perf(
                args.bundle, args.udid, args.rsd_host, args.rsd_port
            )
        except RuntimeError:
            collector = TidevicePerf(args.bundle, args.udid)
    charts: Optional[LiveCharts] = None
    records: list[SampleRecord] = []
    latest: dict[str, Optional[float]] = {
        "fps": None,
        "cpu": None,
        "memory": None,
        "gpu": None,
        "network_down": None,
        "network_up": None,
    }
    received_event = False
    finalization_error: Optional[str] = None
    try:
        collector.start()
        if args.visualize:
            charts = LiveCharts(args.bundle)
        started = time.monotonic()
        next_sample = started + args.interval
        print(f"监控 {args.bundle}，按 Ctrl+C 停止；结束后报告：{args.report}")
        while True:
            if args.stop_file and Path(args.stop_file).is_file():
                break
            if charts and charts.closed:
                break
            failure = collector.failure_message()
            if failure:
                raise RuntimeError(failure)
            for event in collector.drain_events():
                if not received_event:
                    print(
                        f"[IosPerf] first event kind={event.kind} afterStart={time.monotonic() - started:.1f}s",
                        flush=True,
                    )
                received_event = True
                if event.kind in ("fps", "cpu", "memory", "gpu"):
                    latest[event.kind] = (
                        normalize_cpu_event(event.data)
                        if event.kind == "cpu"
                        else optional_float(event.data.get("value"))
                    )
                elif event.kind == "network":
                    down = network_megabytes(event.data, "down")
                    up = network_megabytes(event.data, "up")
                    if down is not None:
                        latest["network_down"] = down
                    if up is not None:
                        latest["network_up"] = up

            now = time.monotonic()
            if not received_event and now - started >= args.startup_timeout:
                details = collector.diagnostic_tail()
                suffix = f"\ntidevice 输出：\n{details}" if details else ""
                raise RuntimeError(
                    "等待 tidevice 性能数据超时；请确认设备已配对、目标 App 正在运行，"
                    f"并且 Developer Image 可用{suffix}"
                )
            if args.duration and now - started >= args.duration and not received_event:
                raise RuntimeError("检测时长内未收到任何 tidevice 性能数据")
            if now >= next_sample:
                if not received_event:
                    print("[IosPerf] waiting first sample", flush=True)
                    next_sample += args.interval
                    continue
                elapsed = now - started
                record = SampleRecord(
                    elapsed,
                    latest["fps"],
                    latest["cpu"],
                    latest["memory"],
                    latest["gpu"],
                    latest["network_down"],
                    latest["network_up"],
                )
                records.append(record)
                def _fmt(value: Optional[float]) -> str:
                    return f"{value:.1f}" if value is not None else "none"
                print(
                    f"PERFPILOT_SAMPLE\tfps={_fmt(record.fps)}\tcpu={_fmt(record.cpu_pct)}\tmemory={_fmt(record.memory_mb)}\tgpu={_fmt(record.gpu_pct)}",
                    flush=True,
                )
                status = (
                    f"[{args.bundle}] FPS={format_value(record.fps):>5}  "
                    f"AppCPU={format_value(record.cpu_pct, '%'):>7}  "
                    f"Memory={format_value(record.memory_mb, 'MiB'):>10}  "
                    f"GPU={format_value(record.gpu_pct, '%'):>7}  "
                    f"Down={format_value(record.network_down_mb, 'MB'):>10}  "
                    f"Up={format_value(record.network_up_mb, 'MB'):>10}"
                )
                sys.stdout.write("\r" + status.ljust(110))
                sys.stdout.flush()
                if charts:
                    charts.update(records)
                next_sample += args.interval
                if args.duration and elapsed >= args.duration:
                    break
            elif charts:
                charts.update(records)
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    except (OSError, RuntimeError) as error:
        message = " ".join(str(error).split())
        print(f"\n采样失败：{message}", file=sys.stderr, flush=True)
        return 1
    finally:
        try:
            collector.stop()
        except Exception as error:
            finalization_error = f"停止采集失败：{error}"
        if charts and not charts.closed:
            charts.close()
        try:
            write_report(args.report, args.bundle, args.udid, records)
        except (OSError, ValueError, TypeError) as error:
            report_error = f"报告生成失败：{error}"
            finalization_error = (
                f"{finalization_error}；{report_error}"
                if finalization_error
                else report_error
            )

    if finalization_error:
        print(f"\n{finalization_error}", file=sys.stderr)
        return 1

    print(
        f"\n已停止：平均 FPS={average(records, 'fps'):.1f}、"
        f"FPS>=18={percentage_matching(records, 'fps', lambda value: value >= 18):.1f}%、"
        f"FPS>=25={percentage_matching(records, 'fps', lambda value: value >= 25):.1f}%、"
        f"Avg(AppCPU)={average(records, 'cpu_pct'):.1f}%、"
        f"AppCPU<=60%={percentage_matching(records, 'cpu_pct', lambda value: value <= 60):.1f}%、"
        f"Avg(Memory)={average(records, 'memory_mb'):.1f}MiB、"
        f"Peak(Memory)={peak(records, 'memory_mb'):.1f}MiB"
    )
    print(f"报告已生成：{args.report}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="通过 pymobiledevice3 或 tidevice 获取 iOS 应用实时 FPS、CPU、内存和网络数据"
    )
    parser.add_argument("--bundle", "-B", required=True, help="目标应用 Bundle ID")
    parser.add_argument("--udid", "-u", help="iOS 设备 UDID；单设备时可省略")
    parser.add_argument("--rsd-host", help="已建立隧道返回的 RSD 地址")
    parser.add_argument("--rsd-port", type=int, help="已建立隧道返回的 RSD 端口")
    parser.add_argument(
        "--backend",
        choices=("auto", "pymobiledevice3", "tidevice"),
        default="auto",
        help="采集后端；默认优先使用支持新系统的 pymobiledevice3",
    )
    parser.add_argument("--interval", type=float, default=1.0, help="报告采样间隔秒数，默认 1.0")
    parser.add_argument("--duration", type=float, help="自动停止秒数；不传则按 Ctrl+C 停止")
    parser.add_argument(
        "--startup-timeout",
        type=float,
        default=20.0,
        help="等待首个性能事件的超时秒数，默认 20",
    )
    parser.add_argument("--visualize", action="store_true", help="打开实时 FPS、CPU、内存趋势图")
    parser.add_argument(
        "--report",
        default="ios_perf_report.html",
        help="停止时生成的 HTML 报告路径，默认 ios_perf_report.html",
    )
    parser.add_argument("--stop-file", help="Web Agent 创建该文件后正常结束并生成报告")
    args = parser.parse_args()
    if args.interval <= 0:
        parser.error("--interval 必须大于 0")
    if args.duration is not None and args.duration <= 0:
        parser.error("--duration 必须大于 0")
    if args.startup_timeout <= 0:
        parser.error("--startup-timeout 必须大于 0")
    if bool(args.rsd_host) != bool(args.rsd_port):
        parser.error("--rsd-host 和 --rsd-port 必须同时提供")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())