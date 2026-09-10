#!/usr/bin/env python3
"""通过 adb shell 采集前台 Android 应用的实时 FPS 和会话平均 FPS。"""

import argparse
import html
import re
import shlex
import statistics
import subprocess
import sys
import time
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


PROFILE_MARKER = "---PROFILEDATA---"
MAX_TIMESTAMP = 9_000_000_000_000_000_000


def adb_shell(serial: Optional[str], *args: str) -> str:
    adb_args = ["adb"]
    if serial:
        adb_args.extend(["-s", serial])
    remote_command = " ".join(shlex.quote(arg) for arg in args)
    result = subprocess.run(
        [*adb_args, "shell", remote_command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(message or f"adb shell 退出码 {result.returncode}")
    return result.stdout


def foreground_package(serial: Optional[str]) -> Optional[str]:
    output = adb_shell(serial, "dumpsys", "window")
    for pattern in (
        r"mCurrentFocus=.*?\s([\w.]+)/[\w.$]+",
        r"mFocusedApp=.*?\s([\w.]+)/[\w.$]+",
    ):
        match = re.search(pattern, output)
        if match:
            return match.group(1)
    return None


def parse_gfx_timestamps(output: str) -> list[int]:
    timestamps: set[int] = set()
    in_profile = False
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if line == PROFILE_MARKER:
            in_profile = not in_profile
            continue
        if not in_profile or not line or line.lower().startswith("flags"):
            continue
        columns = line.split(",")
        if len(columns) < 14:
            continue
        try:
            completed = int(columns[13])
        except ValueError:
            continue
        if 0 < completed < MAX_TIMESTAMP:
            timestamps.add(completed)
    return sorted(timestamps)


def parse_surface_timestamps(output: str) -> tuple[list[int], Optional[int]]:
    lines = output.splitlines()
    if not lines:
        return [], None
    try:
        refresh_ns = int(lines[0].strip())
    except ValueError:
        refresh_ns = None

    # SurfaceFlinger reports desired, actual-present, and frame-ready times.
    # actual-present can repeat the same app buffer on every display vsync;
    # frame-ready is the app submission cadence and matches FPS tools better.
    ready_timestamps: set[int] = set()
    present_timestamps: set[int] = set()
    for line in lines[1:]:
        columns = line.split()
        if len(columns) < 3:
            continue
        try:
            actual_present = int(columns[1])
            frame_ready = int(columns[2])
        except ValueError:
            continue
        if 0 < actual_present < MAX_TIMESTAMP:
            present_timestamps.add(actual_present)
        if 0 < frame_ready < MAX_TIMESTAMP:
            ready_timestamps.add(frame_ready)
    return sorted(ready_timestamps or present_timestamps), refresh_ns


def parse_layers(output: str, package: str) -> list[str]:
    layers: list[str] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if package not in line:
            continue
        if line.startswith("RequestedLayerState{"):
            line = line[len("RequestedLayerState{"):]
            line = re.split(r"\s+parentId=", line, maxsplit=1)[0]
        elif line.startswith("Layer{"):
            line = line[len("Layer{"):].rsplit("}", 1)[0]
        if line and line not in layers:
            layers.append(line)
    return sorted(
        layers,
        key=lambda name: (
            "SurfaceView" not in name,
            "BLAST" not in name,
            name.startswith(("Background for ", "Bounds for ")),
        ),
    )


@dataclass
class FrameSnapshot:
    source: str
    timestamps: list[int]
    refresh_ns: Optional[int]


@dataclass
class ProcessMetrics:
    cpu_pct: Optional[float]
    pss_mb: Optional[float]
    pid: Optional[int]


@dataclass
class SampleRecord:
    elapsed_seconds: float
    fps: float
    average_fps: float
    cpu_pct: Optional[float]
    pss_mb: Optional[float]
    jank: int


def write_report(
    path: str,
    package: str,
    records: list[SampleRecord],
    total_frames: int,
    total_seconds: float,
    cpu_sum: float,
    cpu_samples: int,
    cpu_peak: float,
    pss_sum: float,
    pss_samples: int,
    pss_peak: float,
    fps_ge18_samples: int,
    fps_ge25_samples: int,
) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    average_fps = total_frames / total_seconds if total_seconds else 0.0
    fps_ge18_pct = fps_ge18_samples / len(records) * 100 if records else 0.0
    fps_ge25_pct = fps_ge25_samples / len(records) * 100 if records else 0.0
    avg_cpu = cpu_sum / cpu_samples if cpu_samples else 0.0
    avg_pss = pss_sum / pss_samples if pss_samples else 0.0

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
                f"{index / max(len(values) - 1, 1) * width:.1f},{height - 20 - float(value) / maximum * (height - 45):.1f}"
                for index, value in enumerate(values)
                if value is not None
            )
            scale_text = f"0 - {maximum:.1f}{unit}"
        return (
            f'<section><h2>{html.escape(title)}</h2>'
            f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(title)}">'
            f'<rect width="100%" height="100%" fill="#f8fafc" />'
            f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="3" />'
            f'<text x="12" y="20" fill="#475569">{html.escape(scale_text)}</text>'
            f'</svg></section>'
        )

    rows = []
    for record in records:
        rows.append(
            "<tr>"
            f"<td>{record.elapsed_seconds:.1f}</td><td>{record.fps:.1f}</td>"
            f"<td>{record.average_fps:.1f}</td>"
            f"<td>{record.cpu_pct:.1f}</td>" if record.cpu_pct is not None else
            "<tr>"
            f"<td>{record.elapsed_seconds:.1f}</td><td>{record.fps:.1f}</td>"
            f"<td>{record.average_fps:.1f}</td><td>-</td>"
        )
        if record.cpu_pct is not None:
            rows[-1] += f"<td>{record.pss_mb:.1f}</td>" if record.pss_mb is not None else "<td>-</td>"
        else:
            rows[-1] += f"<td>{record.pss_mb:.1f}</td>" if record.pss_mb is not None else "<td>-</td>"
        rows[-1] += f"<td>{record.jank}</td></tr>"
    document = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>ADB 性能检测报告</title>
<style>body{{font:14px system-ui,sans-serif;color:#172033;max-width:1000px;margin:32px auto;padding:0 20px}}h1{{margin-bottom:4px}}section{{margin:24px 0}}svg{{width:100%;border:1px solid #dbe3ee;border-radius:8px}}table{{border-collapse:collapse;width:100%}}th,td{{border-bottom:1px solid #e2e8f0;padding:7px;text-align:right}}th{{background:#f1f5f9}}.summary{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}}.metric{{background:#f8fafc;border:1px solid #e2e8f0;padding:12px;border-radius:8px}}.metric b{{display:block;font-size:20px;margin-top:5px}}</style></head>
<body><h1>ADB 性能检测报告</h1><p>应用：{html.escape(package)}　生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
<div class="summary"><div class="metric">平均 FPS<b>{average_fps:.1f}</b></div><div class="metric">累计帧数<b>{total_frames}</b></div><div class="metric">FPS ≥ 18<b>{fps_ge18_pct:.1f}%</b></div><div class="metric">FPS ≥ 25<b>{fps_ge25_pct:.1f}%</b></div><div class="metric">平均 CPU<b>{avg_cpu:.1f}%</b></div><div class="metric">平均内存<b>{avg_pss:.1f} MB</b></div><div class="metric">峰值内存<b>{pss_peak:.1f} MB</b></div></div>
{chart_svg('FPS 趋势', 'fps', '#0f766e', '')}{chart_svg('App CPU 趋势', 'cpu_pct', '#c2410c', '%')}{chart_svg('App PSS 趋势', 'pss_mb', '#2563eb', ' MB')}
<h2>采样明细</h2><table><thead><tr><th>时间(s)</th><th>FPS</th><th>平均 FPS</th><th>CPU(%)</th><th>PSS(MB)</th><th>Jank</th></tr></thead><tbody>{''.join(rows)}</tbody></table></body></html>"""
    output_path.write_text(document, encoding="utf-8")


class LiveCharts:
    def __init__(self, package: str) -> None:
        try:
            import tkinter as tk
        except ImportError as error:
            raise RuntimeError("当前 Python 未提供 tkinter，无法启用 --visualize") from error
        self.tk = tk
        self.root = tk.Tk()
        self.root.title(f"ADB 性能监控 - {package}")
        self.root.geometry("900x620")
        self.labels = tk.Label(self.root, anchor="w", justify="left", padx=12, pady=8)
        self.labels.pack(fill="x")
        self.canvas = tk.Canvas(self.root, background="#f8fafc", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.closed = False
        self.root.protocol("WM_DELETE_WINDOW", self.close)

    def close(self) -> None:
        self.closed = True
        self.root.destroy()

    def update(self, records: list[SampleRecord]) -> None:
        if self.closed:
            return
        self.root.update_idletasks()
        self.root.update()
        if self.closed:
            return
        width = max(self.canvas.winfo_width(), 400)
        height = max(self.canvas.winfo_height(), 300)
        self.canvas.delete("all")
        self.canvas.create_text(12, 12, anchor="nw", text="实时趋势（绿色 FPS / 橙色 CPU / 蓝色 PSS）", fill="#334155")
        if not records:
            return
        chart_top, chart_bottom = 38, height - 16
        chart_height = chart_bottom - chart_top
        maximum = max(max((record.fps for record in records), default=1), 1)
        maximum = max(maximum, max((record.cpu_pct or 0 for record in records), default=0))
        maximum = max(maximum, max((record.pss_mb or 0 for record in records), default=0))
        points_by_color = {"#0f766e": [], "#c2410c": [], "#2563eb": []}
        for index, record in enumerate(records):
            x = 12 + index / max(len(records) - 1, 1) * (width - 24)
            for value, color in ((record.fps, "#0f766e"), (record.cpu_pct, "#c2410c"), (record.pss_mb, "#2563eb")):
                if value is not None:
                    points_by_color[color].extend((x, chart_bottom - value / maximum * chart_height))
        for color, points in points_by_color.items():
            if len(points) >= 4:
                self.canvas.create_line(*points, fill=color, width=2, smooth=True)
        latest = records[-1]
        cpu_text = f"{latest.cpu_pct:.1f}%" if latest.cpu_pct is not None else "-"
        pss_text = f"{latest.pss_mb:.1f} MB" if latest.pss_mb is not None else "-"
        self.labels.configure(text=f"FPS {latest.fps:.1f}   平均 {latest.average_fps:.1f}   CPU {cpu_text}   PSS {pss_text}")


class ProcessMonitor:
    def __init__(self, serial: Optional[str]) -> None:
        self.serial = serial
        self.pid: Optional[int] = None
        self.last_process_ticks: Optional[int] = None
        self.last_system_ticks: Optional[int] = None

    def sample(self, package: str) -> ProcessMetrics:
        pid = self._find_pid(package)
        if pid != self.pid:
            self.pid = pid
            self.last_process_ticks = None
            self.last_system_ticks = None
        if pid is None:
            return ProcessMetrics(None, self._read_pss(package), None)

        process_ticks = self._read_process_ticks(pid)
        system_ticks = self._read_system_ticks()
        cpu_pct: Optional[float] = None
        if (
            process_ticks is not None
            and system_ticks is not None
            and self.last_process_ticks is not None
            and self.last_system_ticks is not None
        ):
            process_delta = process_ticks - self.last_process_ticks
            system_delta = system_ticks - self.last_system_ticks
            if process_delta >= 0 and system_delta > 0:
                cpu_pct = process_delta / system_delta * 100.0
        self.last_process_ticks = process_ticks
        self.last_system_ticks = system_ticks
        return ProcessMetrics(cpu_pct, self._read_pss(package), pid)

    def _find_pid(self, package: str) -> Optional[int]:
        output = adb_shell(self.serial, "pidof", package).strip()
        try:
            return int(output.split()[0]) if output else None
        except ValueError:
            return None

    def _read_process_ticks(self, pid: int) -> Optional[int]:
        output = adb_shell(self.serial, "cat", f"/proc/{pid}/stat").strip()
        closing_parenthesis = output.rfind(")")
        if closing_parenthesis < 0:
            return None
        fields = output[closing_parenthesis + 1 :].split()
        # After the command name, fields[11] and fields[12] are utime/stime.
        if len(fields) < 13:
            return None
        try:
            return int(fields[11]) + int(fields[12])
        except ValueError:
            return None

    def _read_system_ticks(self) -> Optional[int]:
        output = adb_shell(self.serial, "cat", "/proc/stat")
        first_cpu = next(
            (line for line in output.splitlines() if line.startswith("cpu ")), None
        )
        if not first_cpu:
            return None
        try:
            return sum(int(value) for value in first_cpu.split()[1:])
        except ValueError:
            return None

    def _read_pss(self, package: str) -> Optional[float]:
        output = adb_shell(self.serial, "dumpsys", "meminfo", package)
        for line in output.splitlines():
            if not line.strip().startswith("TOTAL"):
                continue
            values = re.findall(r"\d+", line)
            if values:
                # dumpsys meminfo reports TOTAL PSS in KB.
                return int(values[0]) / 1024.0
        return None


class AdbFrameSource:
    def __init__(self, serial: Optional[str], mode: str) -> None:
        self.serial = serial
        self.mode = mode
        self.surface_layer: Optional[str] = None

    def sample(self, package: str) -> FrameSnapshot:
        if self.mode in ("auto", "surface"):
            snapshot = self._sample_surface(package)
            if snapshot:
                return snapshot
            if self.mode == "surface":
                return FrameSnapshot("surface", [], None)

        if self.mode in ("auto", "gfxinfo"):
            output = adb_shell(self.serial, "dumpsys", "gfxinfo", package, "framestats")
            timestamps = parse_gfx_timestamps(output)
            return FrameSnapshot("gfxinfo", timestamps, None)

        return FrameSnapshot(self.mode, [], None)

    def _sample_surface(self, package: str) -> Optional[FrameSnapshot]:
        if self.surface_layer:
            latency = adb_shell(
                self.serial,
                "dumpsys",
                "SurfaceFlinger",
                "--latency",
                self.surface_layer,
            )
            timestamps, refresh_ns = parse_surface_timestamps(latency)
            if timestamps:
                return FrameSnapshot(
                    f"surface:{self.surface_layer}", timestamps, refresh_ns
                )
            self.surface_layer = None

        output = adb_shell(self.serial, "dumpsys", "SurfaceFlinger", "--list")
        for layer in parse_layers(output, package):
            latency = adb_shell(
                self.serial, "dumpsys", "SurfaceFlinger", "--latency", layer
            )
            timestamps, refresh_ns = parse_surface_timestamps(latency)
            if timestamps:
                self.surface_layer = layer
                return FrameSnapshot(f"surface:{layer}", timestamps, refresh_ns)
        self.surface_layer = None
        return None


def count_jank(timestamps: list[int], refresh_ns: Optional[int]) -> int:
    intervals = [
        current - previous
        for previous, current in zip(timestamps, timestamps[1:])
        if current > previous
    ]
    if not intervals:
        return 0
    frame_period_ns = int(statistics.median(intervals))
    if refresh_ns and refresh_ns > frame_period_ns:
        frame_period_ns = refresh_ns
    return sum(
        interval > frame_period_ns * 2
        for interval in intervals
    )


def run(args: argparse.Namespace) -> int:
    package = args.package or foreground_package(args.serial)
    if not package:
        print("无法识别前台应用，请通过 --package 指定包名。", file=sys.stderr)
        return 2

    source = AdbFrameSource(args.serial, args.mode)
    process_monitor = ProcessMonitor(args.serial)
    last_timestamp: Optional[int] = None
    last_source: Optional[str] = None
    last_sample_time = time.monotonic()
    total_frames = 0
    total_seconds = 0.0
    fps_samples = 0
    fps_ge18_samples = 0
    fps_ge25_samples = 0
    cpu_sum = 0.0
    cpu_samples = 0
    cpu_le60_samples = 0
    cpu_peak = 0.0
    pss_sum = 0.0
    pss_samples = 0
    pss_peak = 0.0
    records: list[SampleRecord] = []
    charts: Optional[LiveCharts] = None
    if args.visualize:
        try:
            charts = LiveCharts(package)
        except RuntimeError as error:
            print(f"无法启动实时图表：{error}", file=sys.stderr)
            return 1

    print(f"监控 {package}，按 Ctrl+C 停止；结束后报告：{args.report}")
    try:
        while True:
            if args.stop_file and Path(args.stop_file).is_file():
                break
            if charts and charts.closed:
                break
            snapshot = source.sample(package)
            process_metrics = process_monitor.sample(package)
            now = time.monotonic()
            elapsed = now - last_sample_time

            if snapshot.source != last_source:
                last_timestamp = snapshot.timestamps[-1] if snapshot.timestamps else None
                last_source = snapshot.source
                last_sample_time = now
                print(f"数据源: {snapshot.source}")
                time.sleep(args.interval)
                continue

            new_timestamps = [
                timestamp
                for timestamp in snapshot.timestamps
                if last_timestamp is not None and timestamp > last_timestamp
            ]
            if snapshot.timestamps:
                last_timestamp = snapshot.timestamps[-1]

            frame_span_seconds = (
                (new_timestamps[-1] - new_timestamps[0]) / 1_000_000_000
                if len(new_timestamps) > 1
                else 0.0
            )
            # The latency dump is a rolling history, not a one-second sample.
            # Measure the returned frame timestamps themselves so old history or
            # ADB transport time cannot inflate or deflate the FPS result.
            frames = max(len(new_timestamps) - 1, 0)
            measurement_seconds = frame_span_seconds if frame_span_seconds > 0 else args.interval
            fps = frames / measurement_seconds if measurement_seconds > 0 else 0.0
            total_frames += frames
            total_seconds += measurement_seconds
            average_fps = total_frames / total_seconds if total_seconds > 0 else 0.0
            fps_samples += 1
            if fps >= 18:
                fps_ge18_samples += 1
            if fps >= 25:
                fps_ge25_samples += 1
            jank = count_jank(new_timestamps, snapshot.refresh_ns)
            if process_metrics.cpu_pct is not None:
                cpu_sum += process_metrics.cpu_pct
                cpu_samples += 1
                if process_metrics.cpu_pct <= 60:
                    cpu_le60_samples += 1
                cpu_peak = max(cpu_peak, process_metrics.cpu_pct)
            if process_metrics.pss_mb is not None:
                pss_sum += process_metrics.pss_mb
                pss_samples += 1
                pss_peak = max(pss_peak, process_metrics.pss_mb)
            source_name = snapshot.source.split(":", 1)[0]
            cpu_text = (
                f"{process_metrics.cpu_pct:5.1f}%"
                if process_metrics.cpu_pct is not None
                else "  -  "
            )
            pss_text = (
                f"{process_metrics.pss_mb:7.1f}MB"
                if process_metrics.pss_mb is not None
                else "      -"
            )
            fps_ge18_pct = fps_ge18_samples / fps_samples * 100
            fps_ge25_pct = fps_ge25_samples / fps_samples * 100
            cpu_le60_pct = cpu_le60_samples / cpu_samples * 100 if cpu_samples else 0
            records.append(
                SampleRecord(
                    elapsed,
                    fps,
                    average_fps,
                    process_metrics.cpu_pct,
                    process_metrics.pss_mb,
                    jank,
                )
            )
            if charts:
                charts.update(records)
            status = (
                f"[{package}][{source_name}] FPS={fps:5.1f}  AVG={average_fps:5.1f}  "
                f"Frames={total_frames:6d}  Jank={jank:3d}  "
                f"AppCPU={cpu_text}  AppPSS={pss_text}  "
                f"FPS>=18={fps_ge18_pct:5.1f}%  FPS>=25={fps_ge25_pct:5.1f}%"
            )
            sys.stdout.write("\r" + status.ljust(100))
            sys.stdout.flush()
            last_sample_time = now
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    except (FileNotFoundError, subprocess.TimeoutExpired, RuntimeError) as error:
        print(f"\n采样失败：{error}", file=sys.stderr)
        return 1
    finally:
        if charts and not charts.closed:
            charts.close()
        write_report(
            args.report,
            package,
            records,
            total_frames,
            total_seconds,
            cpu_sum,
            cpu_samples,
            cpu_peak,
            pss_sum,
            pss_samples,
            pss_peak,
            fps_ge18_samples,
            fps_ge25_samples,
        )
        print(
            f"\n已停止：平均 FPS={total_frames / total_seconds if total_seconds else 0.0:.1f}，"
            f"累计帧数={total_frames}，统计时长={total_seconds:.1f}s，"
            f"Avg(AppCPU)={cpu_sum / cpu_samples if cpu_samples else 0.0:.1f}%、"
            f"Peak(Memory)={pss_peak:.1f}MB、Peak(AppCPU)={cpu_peak:.1f}%"
        )
        print(f"报告已生成：{args.report}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="通过 ADB 获取应用实时 FPS 和平均 FPS")
    parser.add_argument("--package", "-p", help="目标包名；不传则读取当前前台应用")
    parser.add_argument("--serial", "-s", help="ADB 设备序列号")
    parser.add_argument(
        "--mode",
        choices=("auto", "gfxinfo", "surface"),
        default="auto",
        help="采样通道，默认自动选择",
    )
    parser.add_argument(
        "--interval", type=float, default=1.0, help="采样间隔秒数，默认 1.0"
    )
    parser.add_argument(
        "--visualize", action="store_true", help="打开实时 FPS、CPU、内存趋势图"
    )
    parser.add_argument(
        "--report",
        default="adb_fps_report.html",
        help="停止时生成的 HTML 报告路径，默认 adb_fps_report.html",
    )
    parser.add_argument("--stop-file", help="Web Agent 创建该文件后正常结束并生成报告")
    args = parser.parse_args()
    if args.interval <= 0:
        parser.error("--interval 必须大于 0")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())