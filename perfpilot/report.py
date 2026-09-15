"""Generate a local HTML report that matches the live-monitor charts."""

from __future__ import annotations

import html
import json
import math
import re
from datetime import datetime
from typing import Any, Optional

from .logutil import get_logger

FPS_COLOR = "#ed775f"
CPU_COLOR = "#5f91d3"
MEM_COLOR = "#2c9b6d"
NATIVE_COLOR = "#6b5ce7"
SWAP_COLOR = "#e6a84a"


def _title_part(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    text = re.sub(r'[\\/:*?"<>|]+', "-", text).strip(" .-_")
    return text


def _version_for_title(meta: dict[str, Any]) -> str:
    version = _title_part(meta.get("versionName") or "")
    if version:
        return version
    raw = str(meta.get("appVersion") or "").strip()
    if "(" in raw:
        raw = raw.split("(", 1)[0]
    version = _title_part(raw)
    if version:
        return version
    return _title_part(meta.get("versionCode") or "")


def report_display_name(meta: dict[str, Any] | None) -> str:
    payload = meta if isinstance(meta, dict) else {}
    device = payload.get("device") if isinstance(payload.get("device"), dict) else {}
    app = _title_part(payload.get("appName") or payload.get("bundle") or payload.get("packageId") or "App")
    version = _version_for_title(payload)
    model = _title_part(device.get("model") or device.get("name") or device.get("id") or "device")
    parts = [item for item in (app, version, model) if item]
    return "-".join(parts) or "PerfPilot-report"


def report_filename(meta: dict[str, Any] | None) -> str:
    return f"{report_display_name(meta)}.html"


def _fmt(value: Any, suffix: str = "") -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):.1f}{suffix}"
    except (TypeError, ValueError):
        return "-"


def _num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text in ("none", "-", "None") else text


def _field(item: dict[str, Any], *names: str) -> Any:
    metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else {}
    for name in names:
        if item.get(name) not in (None, ""):
            return item.get(name)
        if isinstance(metrics, dict) and metrics.get(name) not in (None, ""):
            return metrics.get(name)
    return None


def _elapsed_ms(item: dict[str, Any], index: int) -> float:
    raw = item.get("elapsedMs")
    value = _num(raw)
    if value is not None:
        return value
    return float(index) * 1000.0


def _clock(ms: float) -> str:
    sec = max(0, int(ms / 1000.0))
    return f"{sec // 60}:{sec % 60:02d}"


def _fps_max(values: list[Optional[float]]) -> float:
    peak = max((v for v in values if v is not None), default=0.0)
    if peak <= 62:
        return 60.0
    if peak <= 90:
        return 90.0
    if peak <= 120:
        return 120.0
    if peak <= 144:
        return 144.0
    if peak <= 165:
        return 165.0
    return float(((int(peak) + 29) // 30) * 30)


def _cpu_max(values: list[Optional[float]]) -> float:
    peak = max((v for v in values if v is not None), default=0.0)
    if peak <= 50:
        return 50.0
    if peak <= 100:
        return 100.0
    return float(min(400, ((int(peak) + 24) // 25) * 25))


def _mem_max(values: list[Optional[float]]) -> float:
    peak = max((v for v in values if v is not None), default=0.0)
    if peak <= 0:
        return 64.0
    raw = peak * 1.08
    mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1.0
    norm = raw / mag
    nice = 10.0 if norm > 5 else 5.0 if norm > 2 else 2.0 if norm > 1 else 1.0
    return nice * mag


def _y_label(value: float, suffix: str) -> str:
    if suffix == "%":
        return f"{int(round(value))}%"
    if suffix == "FPS":
        return str(int(round(value)))
    if value >= 100:
        return f"{value:.0f} {suffix}".strip()
    if value >= 10:
        return f"{value:.0f} {suffix}".strip()
    return f"{value:.1f} {suffix}".strip()


def _ratio(values: list[Optional[float]], threshold: float, *, le: bool = False) -> Optional[float]:
    nums = [v for v in values if v is not None]
    if not nums:
        return None
    matched = sum(1 for v in nums if v <= threshold) if le else sum(1 for v in nums if v >= threshold)
    return matched / len(nums) * 100.0


def _chart(
    series: list[dict[str, Any]],
    times: list[float],
    y_min: float,
    y_max: float,
    y_suffix: str,
    height: int = 200,
) -> str:
    width = 720
    pad_l, pad_r, pad_t, pad_b = 48, 82, 18, 26
    plot_w = max(40, width - pad_l - pad_r)
    plot_h = max(40, height - pad_t - pad_b)
    t0 = times[0] if times else 0.0
    t1 = times[-1] if times else 1000.0
    t_span = max(1000.0, t1 - t0)
    y_span = y_max - y_min if y_max != y_min else 1.0

    def x_of(ms: float) -> float:
        return pad_l + (ms - t0) / t_span * plot_w

    def y_of(value: float) -> float:
        clamped = min(y_max, max(y_min, value))
        return pad_t + (1 - (clamped - y_min) / y_span) * plot_h

    parts: list[str] = [
        f'<svg class="report-chart" viewBox="0 0 {width} {height}" width="100%" height="{height}" role="img" '
        f'data-t0="{t0:.3f}" data-tspan="{t_span:.3f}" data-padl="{pad_l}" data-padt="{pad_t}" '
        f'data-plotw="{plot_w:.1f}" data-ploth="{plot_h:.1f}">'
    ]
    for i in range(5):
        value = y_min + y_span * i / 4
        y = y_of(value)
        label = f"{int(round(value))} FPS" if y_suffix == "FPS" and i == 4 else _y_label(value, y_suffix)
        parts.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{pad_l + plot_w}" y2="{y:.1f}" stroke="#e8efec"/>'
            f'<text x="{pad_l - 6}" y="{y:.1f}" text-anchor="end" dominant-baseline="middle" '
            f'fill="#8a999b" font-size="10" font-family="Cascadia Mono,Consolas,Microsoft YaHei,monospace">'
            f"{html.escape(label)}</text>"
        )
    for i in range(5):
        ms = t0 + t_span * i / 4
        x = x_of(ms)
        parts.append(
            f'<line x1="{x:.1f}" y1="{pad_t}" x2="{x:.1f}" y2="{pad_t + plot_h}" stroke="#e8efec"/>'
            f'<text x="{x:.1f}" y="{pad_t + plot_h + 14}" text-anchor="middle" fill="#8a999b" '
            f'font-size="10" font-family="Cascadia Mono,Consolas,Microsoft YaHei,monospace">'
            f"{_clock(ms)}</text>"
        )

    end_labels: list[tuple[float, float, str, str]] = []
    for item in series:
        values: list[Optional[float]] = item["values"]
        color = item["color"]
        label = item.get("label") or ""
        segment: list[str] = []
        last: Optional[tuple[float, float]] = None

        def flush() -> None:
            if len(segment) >= 2:
                parts.append(
                    f'<polyline fill="none" stroke="{color}" stroke-width="2.5" '
                    f'points="{" ".join(segment)}"/>'
                )
            segment.clear()

        for index, value in enumerate(values):
            if value is None or index >= len(times):
                flush()
                continue
            x, y = x_of(times[index]), y_of(value)
            segment.append(f"{x:.1f},{y:.1f}")
            last = (x, y)
        flush()
        if label and last:
            end_labels.append((last[0], last[1], color, label))

    avg_labels: list[tuple[float, str, str]] = []
    avg_color = "#8a999b"
    for item in series:
        label = item.get("label") or ""
        avg = _series_mean(item["values"])
        if avg is None:
            continue
        y = y_of(avg)
        parts.append(
            f'<line x1="{pad_l}" y1="{y:.1f}" x2="{pad_l + plot_w}" y2="{y:.1f}" '
            f'stroke="{avg_color}" stroke-width="1.4" stroke-dasharray="6 4" opacity="0.95"/>'
        )
        avg_labels.append((y, avg_color, _avg_label(avg, y_suffix, label, len(series))))
    avg_labels.sort(key=lambda row: row[0])
    placed_avg: list[float] = []
    for y, color, text in avg_labels:
        for prev in placed_avg:
            if abs(y - prev) < 14:
                y = prev + 14
        placed_avg.append(y)
        ty = max(pad_t + 10, y - 3)
        parts.append(
            f'<text x="{pad_l + 6:.1f}" y="{ty:.1f}" fill="{color}" font-size="10" font-weight="600" '
            f'font-family="Segoe UI,PingFang SC,Microsoft YaHei,sans-serif">{html.escape(text)}</text>'
        )
    if avg_labels:
        get_logger("report").info(
            "[ChartAvg] svg suffix=%s count=%s labels=%s",
            y_suffix,
            len(avg_labels),
            ",".join(text for _, _, text in avg_labels),
        )

    end_labels.sort(key=lambda row: row[1])
    placed: list[float] = []
    for x, y, color, label in end_labels:
        for prev in placed:
            if abs(y - prev) < 14:
                y = prev + 14
        placed.append(y)
        tx = min(x + 6, width - pad_r + 4)
        parts.append(
            f'<text x="{tx:.1f}" y="{y - 2:.1f}" fill="{color}" font-size="11" font-weight="600" '
            f'font-family="Segoe UI,PingFang SC,Microsoft YaHei,sans-serif">{html.escape(label)}</text>'
        )
    parts.append(
        f'<rect class="chart-hit" x="{pad_l}" y="{pad_t}" width="{plot_w}" height="{plot_h}" fill="transparent"/>'
        f'<g class="inspect-marker" visibility="hidden" pointer-events="none">'
        f'<line class="inspect-line" x1="{pad_l:.1f}" y1="{pad_t:.1f}" x2="{pad_l:.1f}" y2="{pad_t + plot_h:.1f}" '
        f'stroke="#ed775f" stroke-width="1.5" stroke-dasharray="5 4"/>'
        f'<text class="inspect-clock" x="{pad_l:.1f}" y="{pad_t + 12:.1f}" text-anchor="middle" fill="#ed775f" '
        f'font-size="10" font-weight="600" font-family="Cascadia Mono,Consolas,Microsoft YaHei,monospace"></text>'
        f"</g>"
    )
    parts.append("</svg>")
    return "".join(parts)


def _series_mean(values: list[Optional[float]]) -> Optional[float]:
    nums = [v for v in values if v is not None]
    if not nums:
        return None
    return sum(nums) / len(nums)


def _avg_label(avg: float, y_suffix: str, series_label: str, series_count: int) -> str:
    unit = "%" if y_suffix == "%" else "" if y_suffix == "FPS" else f" {y_suffix}" if y_suffix else ""
    short = (series_label or "").replace(" PSS", "").strip()
    name = f"{short}平均 " if series_count > 1 and short else "平均 "
    return f"{name}{avg:.1f}{unit}"


def _compact_series(values: list[Optional[float]]) -> list[Optional[float]]:
    compact: list[Optional[float]] = []
    for value in values:
        compact.append(None if value is None else round(float(value), 2))
    return compact


def _inspect_script(payload: dict[str, Any]) -> str:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return (
        "<script>\nconst REPORT_DATA = "
        + data
        + ";\n"
        + r"""
(function () {
  const data = REPORT_DATA;
  const times = data.times || [];
  if (!times.length) {
    console.info('[ReportInspect] skip empty samples');
    return;
  }
  function clock(ms) {
    const sec = Math.max(0, Math.floor((Number(ms) || 0) / 1000));
    return Math.floor(sec / 60) + ':' + String(sec % 60).padStart(2, '0');
  }
  function fmt(value, suffix, empty) {
    if (value == null || !Number.isFinite(Number(value))) return empty || '--';
    return Number(value).toFixed(1) + (suffix || '');
  }
  function nearestIndex(timeMs) {
    let best = 0;
    let bestDist = Infinity;
    times.forEach(function (time, index) {
      const dist = Math.abs(time - timeMs);
      if (dist < bestDist) {
        bestDist = dist;
        best = index;
      }
    });
    return best;
  }
  function svgX(svg, event) {
    const pt = svg.createSVGPoint();
    pt.x = event.clientX;
    pt.y = event.clientY;
    const ctm = svg.getScreenCTM();
    if (!ctm) return null;
    return pt.matrixTransform(ctm.inverse()).x;
  }
  function setText(id, text) {
    const node = document.getElementById(id);
    if (node) node.textContent = text;
  }
  const originals = {
    fps: (document.getElementById('chart-fps-label') || {}).textContent,
    cpu: (document.getElementById('chart-cpu-label') || {}).textContent,
    mem: (document.getElementById('chart-memory-label') || {}).textContent,
    fpsCard: (document.getElementById('fps-value') || {}).textContent,
    cpuCard: (document.getElementById('cpu-value') || {}).textContent,
    memCard: (document.getElementById('memory-value') || {}).textContent,
    gpuCard: (document.getElementById('gpu-value') || {}).textContent
  };
  function applyInspect(index, source) {
    if (index == null) {
      document.querySelectorAll('.inspect-marker').forEach(function (g) { g.setAttribute('visibility', 'hidden'); });
      const panel = document.getElementById('timeline-inspect');
      if (panel) panel.hidden = true;
      document.querySelectorAll('tr.is-inspect').forEach(function (row) { row.classList.remove('is-inspect'); });
      const grid = document.querySelector('.metric-grid');
      if (grid) grid.classList.remove('is-inspecting');
      setText('chart-fps-label', originals.fps);
      setText('chart-cpu-label', originals.cpu);
      setText('chart-memory-label', originals.mem);
      setText('fps-value', originals.fpsCard);
      setText('cpu-value', originals.cpuCard);
      setText('memory-value', originals.memCard);
      setText('gpu-value', originals.gpuCard);
      console.info('[ReportInspect] clear source=%s', source);
      return;
    }
    const safe = Math.max(0, Math.min(index, times.length - 1));
    const time = times[safe];
    const fps = (data.fps || [])[safe];
    const cpu = (data.cpu || [])[safe];
    const memory = (data.memory || [])[safe];
    const native = (data.native || [])[safe];
    const swap = (data.swap || [])[safe];
    const gpu = (data.gpu || [])[safe];
    document.querySelectorAll('svg.report-chart').forEach(function (svg) {
      const t0 = Number(svg.dataset.t0);
      const tSpan = Number(svg.dataset.tspan);
      const padL = Number(svg.dataset.padl);
      const padT = Number(svg.dataset.padt);
      const plotW = Number(svg.dataset.plotw);
      const plotH = Number(svg.dataset.ploth);
      const x = padL + ((time - t0) / tSpan) * plotW;
      const marker = svg.querySelector('.inspect-marker');
      const line = svg.querySelector('.inspect-line');
      const text = svg.querySelector('.inspect-clock');
      if (!marker || !line || !text) return;
      line.setAttribute('x1', String(x));
      line.setAttribute('x2', String(x));
      text.setAttribute('x', String(Math.max(padL + 18, Math.min(x, padL + plotW - 18))));
      text.setAttribute('y', String(padT + 12));
      text.textContent = clock(time);
      marker.setAttribute('visibility', 'visible');
    });
    const panel = document.getElementById('timeline-inspect');
    if (panel) panel.hidden = false;
    const grid = document.querySelector('.metric-grid');
    if (grid) grid.classList.add('is-inspecting');
    setText('inspect-clock', clock(time));
    setText('inspect-fps', fmt(fps, '', '--'));
    setText('inspect-cpu', fmt(cpu, '%', '--'));
    setText('inspect-memory', fmt(memory, ' MB', '--'));
    setText('inspect-gpu', fmt(gpu, '%', '不支持'));
    if (data.hasNative) setText('inspect-native', fmt(native, ' MB', '--'));
    if (data.hasSwap) setText('inspect-swap', fmt(swap, ' MB', '--'));
    setText('chart-fps-label', fmt(fps, ' FPS', '--'));
    setText('chart-cpu-label', fmt(cpu, '% CPU', '--'));
    const memBits = [];
    if (memory != null && Number.isFinite(Number(memory))) memBits.push(fmt(memory, ' MB'));
    if (data.hasNative && native != null && Number.isFinite(Number(native))) memBits.push('N ' + fmt(native, ''));
    if (data.hasSwap && swap != null && Number.isFinite(Number(swap))) memBits.push('S ' + fmt(swap, ''));
    setText('chart-memory-label', memBits.join(' · ') || '--');
    setText('fps-value', fmt(fps, '', '--'));
    setText('cpu-value', fmt(cpu, '%', '--'));
    setText('memory-value', fmt(memory, ' MB', '--'));
    setText('gpu-value', fmt(gpu, '%', '不支持'));
    document.querySelectorAll('tr.is-inspect').forEach(function (row) { row.classList.remove('is-inspect'); });
    const row = document.querySelector('tr[data-index="' + safe + '"]');
    if (row) {
      row.classList.add('is-inspect');
      row.scrollIntoView({ block: 'nearest' });
    }
    console.info('[ReportInspect] pick index=%s source=%s elapsedMs=%s fps=%s cpu=%s memory=%s', safe, source, time, fps, cpu, memory);
  }
  document.querySelectorAll('svg.report-chart').forEach(function (svg) {
    svg.addEventListener('click', function (event) {
      const x = svgX(svg, event);
      if (x == null) return;
      const padL = Number(svg.dataset.padl);
      const plotW = Number(svg.dataset.plotw);
      if (x < padL || x > padL + plotW) return;
      const t0 = Number(svg.dataset.t0);
      const tSpan = Number(svg.dataset.tspan);
      applyInspect(nearestIndex(t0 + ((x - padL) / plotW) * tSpan), 'chart');
    });
  });
  const table = document.querySelector('.table-wrap tbody');
  if (table) {
    table.addEventListener('click', function (event) {
      const row = event.target.closest('tr[data-index]');
      if (!row) return;
      applyInspect(Number(row.dataset.index), 'table');
    });
  }
  const clear = document.getElementById('inspect-clear');
  if (clear) clear.addEventListener('click', function () { applyInspect(null, 'clear'); });
  console.info('[ReportInspect] ready samples=%s native=%s swap=%s', times.length, data.hasNative, data.hasSwap);
})();
</script>
"""
    )


def _legend(items: list[tuple[str, str]]) -> str:
    bits = []
    for color, name in items:
        bits.append(
            f'<span class="leg" style="color:{color}"><i style="background:{color}"></i>{html.escape(name)}</span>'
        )
    bits.append('<span class="leg avg"><i></i>平均值</span>')
    return '<div class="chart-legend">' + "".join(bits) + "</div>"


def _fmt_datetime(ms: Optional[float]) -> str:
    if not ms:
        return "-"
    try:
        return datetime.fromtimestamp(float(ms) / 1000.0).strftime("%Y-%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return "-"


def _duration_short(ms: Optional[float]) -> str:
    if ms is None or ms < 0:
        return "-"
    total = int(ms / 1000.0)
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{seconds:02d}s"
    return f"{minutes}m{seconds:02d}s"


def _duration_cn(ms: Optional[float]) -> str:
    if ms is None or ms < 0:
        return "-"
    total = int(ms / 1000.0)
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours}小时{minutes}分{seconds}秒"
    if minutes:
        return f"{minutes}分钟{seconds}秒"
    return f"{seconds}秒"


def _count_runs(values: list[Optional[float]], predicate, min_count: int = 3) -> int:
    count = 0
    run = 0
    for value in values:
        if value is not None and predicate(value):
            run += 1
            continue
        if run >= min_count:
            count += 1
        run = 0
    if run >= min_count:
        count += 1
    return count


def _cap_map(raw: Any) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    if not isinstance(raw, list):
        return found
    for item in raw:
        if isinstance(item, dict) and item.get("id"):
            found[str(item.get("id"))] = item
    return found


def _series_state(values: list[Optional[float]], cap: Optional[dict[str, Any]] = None, enabled: Optional[bool] = None) -> str:
    if any(value is not None for value in values):
        return "available"
    if enabled is False:
        return "unsupported"
    if cap:
        state = str(cap.get("state") or "")
        if state == "degraded":
            return "degraded"
        if state in ("unsupported", "unavailable"):
            return "unsupported"
        if state == "available" or enabled is True:
            return "degraded"
    if enabled is True:
        return "degraded"
    return "unsupported"


def _grade_fps(avg: Optional[float], min_v: Optional[float], drops: int) -> tuple[str, str]:
    if avg is None:
        return "na", "不支持"
    if avg >= 50 and (min_v is None or min_v >= 25) and drops == 0:
        return "good", "良好"
    if avg >= 40:
        return "warn", "注意"
    return "bad", "告警"


def _grade_cpu(avg: Optional[float]) -> tuple[str, str]:
    if avg is None:
        return "na", "不支持"
    if avg < 25:
        return "good", "良好"
    if avg < 50:
        return "warn", "注意"
    return "bad", "告警"


def _grade_mem(avg: Optional[float], peak: Optional[float], delta: Optional[float]) -> tuple[str, str]:
    if avg is None:
        return "na", "不支持"
    if (peak is not None and peak >= 1536) or (delta is not None and delta >= 200):
        return "bad", "告警"
    if (peak is not None and peak >= 1024) or (delta is not None and delta >= 80):
        return "warn", "注意"
    return "good", "良好"


def _grade_gpu(avg: Optional[float]) -> tuple[str, str]:
    if avg is None:
        return "na", "不支持"
    if avg < 40:
        return "good", "良好"
    if avg < 70:
        return "warn", "注意"
    return "bad", "告警"


def _coverage_text(
    fps: list[Optional[float]],
    cpu: list[Optional[float]],
    memory: list[Optional[float]],
    gpu: list[Optional[float]],
    native: list[Optional[float]],
    swap: list[Optional[float]],
    extras: dict[str, Any],
    capabilities: Any,
) -> tuple[str, dict[str, str]]:
    caps = _cap_map(capabilities)
    states = {
        "FPS": _series_state(fps, caps.get("fps")),
        "CPU": _series_state(cpu, caps.get("cpu")),
        "Memory": _series_state(memory, caps.get("memory")),
        "GPU": _series_state(gpu, caps.get("gpu")),
        "Native PSS": _series_state(native, enabled=bool(extras.get("nativePss"))),
        "Swap PSS": _series_state(swap, enabled=bool(extras.get("swapPss"))),
    }
    available = sum(1 for state in states.values() if state == "available")
    degraded = sum(1 for state in states.values() if state == "degraded")
    unsupported = sum(1 for state in states.values() if state == "unsupported")
    total = len(states)
    bits = [f"{available}/{total} 指标可用"]
    if degraded:
        bits.append(f"{degraded}项降级")
    if unsupported:
        bits.append(f"{unsupported}项不支持")
    names = "、".join(name for name, state in states.items() if state == "available")
    if names:
        bits.append(f"已采集 {names}")
    return "，".join(bits), states


def _overview_html(
    meta: dict[str, Any],
    samples: list[dict[str, Any]],
    summary: dict[str, Any],
    extras: dict[str, Any],
    fps: list[Optional[float]],
    cpu: list[Optional[float]],
    memory: list[Optional[float]],
    gpu: list[Optional[float]],
    native: list[Optional[float]],
    swap: list[Optional[float]],
    times: list[float],
) -> str:
    log = get_logger("report")
    device = meta.get("device") or {}
    bundle = str(meta.get("packageId") or meta.get("bundle") or "-")
    app_name = str(meta.get("appName") or bundle)
    app_version = str(meta.get("appVersion") or meta.get("versionName") or "") or "-"
    version_code = str(meta.get("versionCode") or "")
    if app_version != "-" and version_code and version_code not in app_version:
        app_version = f"{app_version} ({version_code})"
    device_name = str(device.get("name") or device.get("id") or "-")
    platform = str(device.get("platform") or "")
    os_label = "Android" if platform == "android" else "iOS" if platform == "ios" else "系统"
    os_ver = str(device.get("version") or device.get("osVersion") or "-")
    if os_ver.strip().lower() in {"", "android", "ios"}:
        os_ver = "-"
    started = meta.get("startedAtMs")
    if not started and samples:
        started = samples[0].get("timestampMs")
    ended = meta.get("endedAtMs")
    duration_ms: Optional[float] = None
    if started and ended and float(ended) > float(started):
        duration_ms = float(ended) - float(started)
    elif times:
        duration_ms = float(times[-1])

    fps_stats = summary.get("fps") or {}
    cpu_stats = summary.get("cpu") or {}
    mem_stats = summary.get("memory") or {}
    gpu_stats = summary.get("gpu") or {}
    fps_avg = fps_stats.get("avg")
    fps_min = fps_stats.get("min")
    cpu_avg = cpu_stats.get("avg")
    mem_avg = mem_stats.get("avg")
    mem_peak = mem_stats.get("peak")
    mem_delta = mem_stats.get("delta")
    gpu_avg = gpu_stats.get("avg")
    fps_drops = _count_runs(fps, lambda value: value < 30)
    fps_g, fps_l = _grade_fps(fps_avg, fps_min, fps_drops)
    cpu_g, cpu_l = _grade_cpu(cpu_avg)
    mem_g, mem_l = _grade_mem(mem_avg, mem_peak, mem_delta)
    gpu_g, gpu_l = _grade_gpu(gpu_avg)
    coverage, _states = _coverage_text(fps, cpu, memory, gpu, native, swap, extras, meta.get("capabilities"))
    icons = {"good": "🟢", "warn": "🟡", "bad": "🔴", "na": "⚪"}
    verdicts = [
        ("FPS", fps_avg, "", fps_g, fps_l),
        ("CPU", cpu_avg, "%", cpu_g, cpu_l),
        ("Memory", mem_avg, "MB", mem_g, mem_l),
        ("GPU", gpu_avg, "%", gpu_g, gpu_l),
    ]
    verdict_html = "".join(
        "<div class='verdict'>"
        f"<span>{html.escape(name)}</span>"
        f"<b>{html.escape(_fmt(value, unit))}</b>"
        f"<em class='grade {grade}'>{icons[grade]} {html.escape(label)}</em>"
        "</div>"
        for name, value, unit, grade, label in verdicts
    )

    paragraphs: list[str] = [f"本次测试持续{_duration_cn(duration_ms)}。"]
    if fps_avg is None:
        paragraphs.append("本次未采集到有效 FPS 数据。")
    elif fps_g == "good":
        paragraphs.append(f"期间平均 FPS 为 {_fmt(fps_avg)}，整体运行稳定。")
    elif fps_g == "warn":
        paragraphs.append(f"期间平均 FPS 为 {_fmt(fps_avg)}，整体尚可，但存在波动。")
    else:
        paragraphs.append(f"期间平均 FPS 为 {_fmt(fps_avg)}，帧率偏低，建议关注卡顿。")
    if fps_avg is not None:
        if fps_drops:
            paragraphs.append(f"检测到 {fps_drops} 次 FPS 低于 30 的异常情况。")
        else:
            paragraphs.append("未检测到连续 FPS 低于 30 的异常。")
    if mem_avg is None:
        paragraphs.append("未采集到有效内存数据。")
    elif mem_peak is not None and mem_peak >= 1024:
        paragraphs.append(
            f"Memory 峰值达到 {mem_peak / 1024:.2f}GB，建议关注内存占用。"
        )
    elif mem_delta is not None and mem_delta >= 80:
        paragraphs.append(
            f"Memory 在测试后期持续增长 {mem_delta:.0f}MB，最高达到 {_fmt(mem_peak)}MB，建议进一步关注内存增长趋势。"
        )
    else:
        paragraphs.append(f"Memory 平均 {_fmt(mem_avg)}MB，整体平稳。")
    if gpu_avg is None:
        paragraphs.append("GPU 当前设备不支持或未采集，报告中不填写 0。")

    log.info(
        "[Report] basics runId=%s app=%s package=%s version=%s device=%s os=%s durationMs=%s coverage=%s",
        meta.get("runId"),
        app_name,
        bundle,
        app_version,
        device_name,
        f"{os_label} {os_ver}",
        duration_ms,
        coverage,
    )
    log.info(
        "[Report] conclusion runId=%s fpsAvg=%s fpsGrade=%s fpsDrops=%s cpuAvg=%s memAvg=%s memDelta=%s memPeak=%s gpuAvg=%s gpuGrade=%s",
        meta.get("runId"),
        fps_avg,
        fps_l,
        fps_drops,
        cpu_avg,
        mem_avg,
        mem_delta,
        mem_peak,
        gpu_avg,
        gpu_l,
    )
    conclusion = "<br>".join(html.escape(item) for item in paragraphs)
    return f"""
<div class="detail-grid">
<section class="chart-card">
  <div class="chart-title"><div><p class="eyebrow">SESSION</p><h3>测试基本信息</h3></div></div>
  <dl class="info-list">
    <div><dt>应用</dt><dd>{html.escape(app_name)}</dd></div>
    <div><dt>Package</dt><dd class="pkg">{html.escape(bundle)}</dd></div>
    <div><dt>Version</dt><dd>{html.escape(app_version)}</dd></div>
    <div><dt>设备</dt><dd>{html.escape(device_name)}</dd></div>
    <div><dt>{html.escape(os_label)}</dt><dd>{html.escape(os_ver)}</dd></div>
    <div><dt>测试时间</dt><dd>{html.escape(_fmt_datetime(started))}</dd></div>
    <div><dt>测试时长</dt><dd>{html.escape(_duration_short(duration_ms))}</dd></div>
    <div class="span2"><dt>能力覆盖</dt><dd>{html.escape(coverage)}</dd></div>
  </dl>
</section>
<section class="chart-card">
  <div class="chart-title"><div><p class="eyebrow">CONCLUSION</p><h3>性能结论</h3></div></div>
  <div class="verdicts">{verdict_html}</div>
  <p class="conclusion">{conclusion}</p>
</section>
</div>"""


def render_report(meta: dict[str, Any], samples: list[dict[str, Any]]) -> str:
    log = get_logger("report")
    device = meta.get("device") or {}
    summary = meta.get("summary") or {}
    extras = meta.get("extras") or {}
    if not summary and samples:
        from .summary import summarize

        summary = summarize(samples)
    times = [_elapsed_ms(item, index) for index, item in enumerate(samples)]
    fps = [_num(_field(item, "fps")) for item in samples]
    cpu = [_num(_field(item, "cpu", "appCpuPct")) for item in samples]
    memory = [_num(_field(item, "memory", "memoryMiB")) for item in samples]
    native = [_num(_field(item, "nativePss")) for item in samples]
    swap = [_num(_field(item, "swapPss")) for item in samples]
    gpu = [_num(_field(item, "gpu", "gpuPct")) for item in samples]
    has_native = any(v is not None for v in native) or bool(extras.get("nativePss"))
    has_swap = any(v is not None for v in swap) or bool(extras.get("swapPss"))
    scene_count = sum(1 for item in samples if _text(_field(item, "scene")))
    script_count = sum(1 for item in samples if _text(_field(item, "scriptFn", "script", "function")))
    log.info(
        "[Report] render runId=%s samples=%s native=%s swap=%s scenes=%s scripts=%s",
        meta.get("runId"),
        len(samples),
        has_native,
        has_swap,
        scene_count,
        script_count,
    )
    overview = _overview_html(
        meta,
        samples,
        summary,
        extras if isinstance(extras, dict) else {},
        fps,
        cpu,
        memory,
        gpu,
        native,
        swap,
        times,
    )

    fps_summary = summary.get("fps") or {}
    cpu_summary = summary.get("cpu") or {}
    memory_summary = summary.get("memory") or {}
    cpu_le25 = _ratio(cpu, 25, le=True)
    cpu_le50 = _ratio(cpu, 50, le=True)
    log.info(
        "[Report] cpuShare runId=%s samples=%s le25=%s le50=%s",
        meta.get("runId"),
        sum(1 for v in cpu if v is not None),
        cpu_le25,
        cpu_le50,
    )
    last_fps = next((v for v in reversed(fps) if v is not None), None)
    last_cpu = next((v for v in reversed(cpu) if v is not None), None)
    last_mem = next((v for v in reversed(memory) if v is not None), None)
    last_native = next((v for v in reversed(native) if v is not None), None)
    last_swap = next((v for v in reversed(swap) if v is not None), None)

    fps_svg = (
        _chart([{"values": fps, "color": FPS_COLOR, "label": "FPS"}], times, 0, _fps_max(fps), "FPS")
        if len(times) >= 2
        else "<p class='empty'>样本不足</p>"
    )
    cpu_svg = (
        _chart([{"values": cpu, "color": CPU_COLOR, "label": "CPU"}], times, 0, _cpu_max(cpu), "%")
        if len(times) >= 2
        else "<p class='empty'>样本不足</p>"
    )
    mem_series = [{"values": memory, "color": MEM_COLOR, "label": "TOTAL PSS"}]
    mem_pool = [v for v in memory if v is not None]
    if has_native:
        mem_series.append({"values": native, "color": NATIVE_COLOR, "label": "Native PSS"})
        mem_pool.extend(v for v in native if v is not None)
    if has_swap:
        mem_series.append({"values": swap, "color": SWAP_COLOR, "label": "Swap PSS"})
        mem_pool.extend(v for v in swap if v is not None)
    mem_legend = [("TOTAL PSS", MEM_COLOR)]
    if has_native:
        mem_legend.append(("Native PSS", NATIVE_COLOR))
    if has_swap:
        mem_legend.append(("Swap PSS", SWAP_COLOR))
    mem_svg = (
        _chart(mem_series, times, 0, _mem_max(mem_pool), "MB", height=220)
        if len(times) >= 2
        else "<p class='empty'>样本不足</p>"
    )

    native_th = "<th>Native PSS</th>" if has_native else ""
    swap_th = "<th>Swap PSS</th>" if has_swap else ""
    rows: list[str] = []
    for index, item in enumerate(samples):
        scene = _text(_field(item, "scene")) or "-"
        script_fn = _text(_field(item, "scriptFn", "script", "function")) or "-"
        native_td = f"<td>{_fmt(native[index], ' MB')}</td>" if has_native else ""
        swap_td = f"<td>{_fmt(swap[index], ' MB')}</td>" if has_swap else ""
        rows.append(
            f'<tr data-index="{index}">'
            f"<td>{_clock(_elapsed_ms(item, index))}</td>"
            f"<td>{html.escape(scene)}</td>"
            f"<td class='mono'>{html.escape(script_fn)}</td>"
            f"<td>{_fmt(fps[index])}</td>"
            f"<td>{_fmt(cpu[index], '%')}</td>"
            f"<td>{_fmt(memory[index], ' MB')}</td>"
            f"{native_td}{swap_td}"
            f"<td>{_fmt(gpu[index], '%')}</td>"
            "</tr>"
        )
    colspan = 7 + int(has_native) + int(has_swap)
    device_name = str(device.get("name") or device.get("id") or "未知设备")
    device_model = str(device.get("model") or device_name)
    bundle = str(meta.get("packageId") or meta.get("bundle") or "")
    title = report_display_name(meta)
    app_name = str(meta.get("appName") or bundle or "未知应用")
    app_version = str(meta.get("appVersion") or meta.get("versionName") or "") or "版本未知"
    version_code = str(meta.get("versionCode") or "")
    if app_version != "版本未知" and version_code and version_code not in app_version:
        app_version = f"{app_version} ({version_code})"
    log.info(
        "[Report] title runId=%s name=%s app=%s version=%s model=%s",
        meta.get("runId"),
        title,
        meta.get("appName"),
        meta.get("versionName") or meta.get("appVersion"),
        device_model,
    )
    duration = _clock(times[-1]) if times else "0:00"
    mem_bits = [_fmt(last_mem, " MB")]
    if has_native and last_native is not None:
        mem_bits.append(f"N {_fmt(last_native)}")
    if has_swap and last_swap is not None:
        mem_bits.append(f"S {_fmt(last_swap)}")

    native_cell = (
        '<div id="inspect-native-cell"><small>Native PSS</small><b id="inspect-native">--</b></div>'
        if has_native
        else '<div id="inspect-native-cell" hidden><small>Native PSS</small><b id="inspect-native">--</b></div>'
    )
    swap_cell = (
        '<div id="inspect-swap-cell"><small>Swap PSS</small><b id="inspect-swap">--</b></div>'
        if has_swap
        else '<div id="inspect-swap-cell" hidden><small>Swap PSS</small><b id="inspect-swap">--</b></div>'
    )
    inspect_payload = {
        "times": [round(float(item), 1) for item in times],
        "fps": _compact_series(fps),
        "cpu": _compact_series(cpu),
        "memory": _compact_series(memory),
        "native": _compact_series(native),
        "swap": _compact_series(swap),
        "gpu": _compact_series(gpu),
        "hasNative": has_native,
        "hasSwap": has_swap,
    }
    log.info(
        "[ReportInspect] embed runId=%s samples=%s native=%s swap=%s",
        meta.get("runId"),
        len(times),
        has_native,
        has_swap,
    )

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>
:root{{--ink:#17252b;--muted:#718087;--line:#dce6e3;--paper:#f4f7f4;--panel:#fff;--mint:#d8f0e5;--green:{MEM_COLOR};--coral:{FPS_COLOR};--amber:{SWAP_COLOR};--blue:{CPU_COLOR};--shadow:0 18px 45px rgba(29,59,54,.07);--sans:'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;--mono:'Cascadia Mono',Consolas,'Microsoft YaHei',monospace}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font-family:var(--sans);font-size:14px}}
.wrap{{max-width:1180px;margin:0 auto;padding:32px 28px 60px}}
.eyebrow{{font:500 10px var(--mono);letter-spacing:1.5px;color:#8a999b;margin:0 0 7px}}
h1{{font-size:28px;letter-spacing:-1px;margin:0 0 8px}}h2,h3{{margin:0}}
.meta{{color:var(--muted);display:flex;gap:16px;flex-wrap:wrap;margin-bottom:18px}}
.detail-grid{{display:grid;grid-template-columns:1fr 1.15fr;gap:17px;margin-bottom:17px}}
.info-list{{display:grid;grid-template-columns:1fr 1fr;gap:4px 18px;margin:14px 0 0}}
.info-list div{{border-bottom:1px solid #eef3f1;padding:8px 0}}
.info-list .span2{{grid-column:1/-1}}
.info-list dt{{font:500 10px var(--mono);letter-spacing:.6px;color:var(--muted);margin:0 0 4px}}
.info-list dd{{margin:0;font-size:15px;overflow-wrap:break-word}}
.info-list dd.pkg{{word-break:break-all}}
.verdicts{{display:grid;gap:0;margin-top:12px}}
.verdict{{display:grid;grid-template-columns:88px 1fr auto;align-items:center;gap:10px;padding:9px 0;border-bottom:1px solid #eef3f1}}
.verdict span{{color:var(--muted);font:500 11px var(--mono)}}
.verdict b{{font-size:18px;letter-spacing:-.4px}}
.grade{{font-style:normal;font-size:13px}}
.grade.good{{color:#318c69}}.grade.warn{{color:#c9892a}}.grade.bad{{color:#c45c4a}}.grade.na{{color:#8a999b}}
.conclusion{{margin:16px 0 0;padding:14px 16px;background:#f7fbf9;border-left:3px solid var(--green);color:#334;line-height:1.75}}
.metric-grid{{display:grid;grid-template-columns:1.35fr 1fr 1fr 1fr;gap:15px;margin-bottom:17px}}
.metric-card{{background:#fff;border:1px solid var(--line);padding:20px 21px;box-shadow:var(--shadow)}}
.metric-card strong{{display:block;font-size:34px;letter-spacing:-2px;margin:16px 0 4px}}
.metric-card small{{color:var(--muted);font-size:10px}}
.fps-card{{background:#fff7f2;border-color:#f5d6cc}}.fps-card strong{{color:var(--coral);font-size:42px}}
.metric-label{{font:500 10px var(--mono);letter-spacing:1px;display:flex;justify-content:space-between}}
.quality{{font-size:8px;padding:4px 6px;border-radius:20px;color:#318c69;background:#ddf3e6}}
.metric-foot{{display:flex;gap:16px;border-top:1px solid #ebdfd8;margin-top:14px;padding-top:11px;color:var(--muted);font-size:10px}}
.metric-foot b{{color:var(--ink);margin-left:4px}}
.chart-grid{{display:grid;grid-template-columns:1.2fr 1fr;gap:17px;margin-bottom:17px}}
.chart-card{{background:#fff;border:1px solid rgba(196,213,208,.7);box-shadow:var(--shadow);padding:20px 22px}}
.memory-chart{{grid-column:1/-1}}
.chart-title{{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}}
.chart-value{{font:500 11px var(--mono);color:var(--coral)}}
.chart-axis-hint{{margin:6px 0 0;color:var(--muted);font:10px var(--mono)}}
.chart-legend{{display:flex;flex-wrap:wrap;gap:10px 14px;margin-top:8px;font:500 10px var(--mono)}}
.chart-legend .leg{{display:flex;align-items:center;gap:6px}}
.chart-legend i{{width:10px;height:3px;border-radius:2px;display:inline-block}}
.chart-legend .leg.avg{{color:#8a999b}}
.chart-legend .leg.avg i{{height:0;width:14px;border-top:2px dashed currentColor;background:transparent;border-radius:0}}
svg{{width:100%;margin-top:10px;cursor:crosshair}}
.empty{{color:var(--muted);padding:24px 0}}
.timeline-inspect{{background:#fff;border:1px solid var(--line);box-shadow:var(--shadow);padding:16px 22px 18px;margin-bottom:17px}}
.timeline-inspect[hidden]{{display:none!important}}
.timeline-inspect-head{{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:14px}}
.timeline-inspect-head h3{{margin:0}}
.inspect-live-btn{{border:1px solid var(--line);background:#fff;color:var(--ink);padding:8px 12px;border-radius:3px;cursor:pointer;font-weight:600;font-size:12px}}
.inspect-live-btn:hover{{border-color:var(--green);background:#f2fbf5}}
.inspect-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:12px}}
.inspect-grid small{{display:block;color:var(--muted);font:500 10px var(--mono);letter-spacing:.6px;margin-bottom:4px}}
.inspect-grid b{{font-size:18px;letter-spacing:-.4px}}
.metric-grid.is-inspecting .quality.available{{color:#ad7d36;background:#fff1d2}}
tr[data-index]{{cursor:pointer}}
tr.is-inspect td{{background:#fff4ef}}
.table-card{{background:#fff;border:1px solid var(--line);box-shadow:var(--shadow);padding:20px 22px}}
.table-wrap{{max-height:520px;overflow:auto;margin-top:14px}}
table{{width:100%;border-collapse:collapse}}
th,td{{border-bottom:1px solid #e6eeeb;padding:8px 10px;text-align:left;font-size:12px}}
th{{position:sticky;top:0;background:#f7fbf9;font:500 10px var(--mono);letter-spacing:.4px;color:var(--muted)}}
td.mono{{font-family:var(--mono);font-size:11px}}
.hint{{color:var(--muted);font-size:12px;line-height:1.6;margin:8px 0 0}}
@media(max-width:900px){{.metric-grid,.chart-grid,.detail-grid{{grid-template-columns:1fr}}.memory-chart{{grid-column:auto}}}}
</style></head><body>
<div class="wrap">
<p class="eyebrow">PERFORMANCE LAB / SESSION REPORT</p>
<h1>{html.escape(title)}</h1>
<p class="meta"><span>{html.escape(app_name)}</span><span>{html.escape(app_version)}</span>
<span>{html.escape(device_model)}</span><span>{html.escape(bundle)}</span>
<span>时长 {duration}</span><span>样本 {len(samples)}</span></p>
{overview}
<div class="metric-grid">
<article class="metric-card fps-card"><div class="metric-label"><span>FPS</span><em class="quality available">REPORT</em></div>
<strong id="fps-value">{_fmt(last_fps)}</strong><small>末点帧率 · 点击曲线查看该时刻</small>
<div class="metric-foot"><span>平均 <b>{_fmt(fps_summary.get('avg'))}</b></span><span>最低 <b>{_fmt(fps_summary.get('min'))}</b></span></div></article>
<article class="metric-card"><div class="metric-label"><span>APP CPU</span><em class="quality available">AVAILABLE</em></div>
<strong id="cpu-value">{_fmt(last_cpu, '%')}</strong><small>目标进程 CPU 占用</small>
<div class="metric-foot"><span>≤25% 概率 <b>{_fmt(cpu_le25, '%')}</b></span><span>≤50% 概率 <b>{_fmt(cpu_le50, '%')}</b></span></div></article>
<article class="metric-card"><div class="metric-label"><span>MEMORY</span><em class="quality available">PSS</em></div>
<strong id="memory-value" style="color:var(--green)">{_fmt(last_mem, ' MB')}</strong><small>目标进程 TOTAL PSS</small>
<div class="metric-foot"><span>峰值 <b>{_fmt(memory_summary.get('peak'), ' MB')}</b></span><span>平均 <b>{_fmt(memory_summary.get('avg'), ' MB')}</b></span></div></article>
<article class="metric-card"><div class="metric-label"><span>GPU</span><em class="quality">DEGRADED</em></div>
<strong id="gpu-value">{_fmt(next((v for v in reversed(gpu) if v is not None), None), '%')}</strong><small>不支持时显示 -</small>
<div class="metric-foot"><span>Peak CPU <b>{_fmt(cpu_summary.get('peak'), '%')}</b></span></div></article>
</div>
<aside id="timeline-inspect" class="timeline-inspect" hidden>
  <div class="timeline-inspect-head"><div><p class="eyebrow">TIME SYNC</p><h3>时间点 <span id="inspect-clock">--</span></h3></div>
  <button type="button" id="inspect-clear" class="inspect-live-btn">清除选点</button></div>
  <div class="inspect-grid">
    <div><small>FPS</small><b id="inspect-fps">--</b></div>
    <div><small>CPU</small><b id="inspect-cpu">--</b></div>
    <div><small>Memory</small><b id="inspect-memory">--</b></div>
    {native_cell}
    {swap_cell}
    <div><small>GPU</small><b id="inspect-gpu">不支持</b></div>
  </div>
</aside>
<div class="chart-grid">
<article class="chart-card"><div class="chart-title"><div><p class="eyebrow">FRAME PACING</p><h3>FPS 趋势</h3>
{_legend([(FPS_COLOR, 'FPS')])}
<p class="chart-axis-hint">纵轴：帧率 (FPS) · 横轴：时间 · 点击对齐该时刻</p></div>
<span class="chart-value" id="chart-fps-label">{_fmt(last_fps)} FPS</span></div>{fps_svg}</article>
<article class="chart-card"><div class="chart-title"><div><p class="eyebrow">RESOURCE LOAD</p><h3>CPU</h3>
{_legend([(CPU_COLOR, 'App CPU')])}
<p class="chart-axis-hint">纵轴：占用 (%) · 横轴：时间 · 点击对齐该时刻</p></div>
<span class="chart-value" id="chart-cpu-label">{_fmt(last_cpu, '%')} CPU</span></div>{cpu_svg}</article>
<article class="chart-card memory-chart"><div class="chart-title"><div><p class="eyebrow">PROCESS MEMORY</p><h3>Memory 趋势</h3>
{_legend([(color, name) for name, color in mem_legend])}
<p class="chart-axis-hint">纵轴：内存 (MB) · 横轴：时间 · 点击对齐该时刻</p></div>
<span class="chart-value" id="chart-memory-label" style="color:var(--green)">{' · '.join(mem_bits)}</span></div>{mem_svg}</article>
</div>
<section class="table-card">
<div class="chart-title"><div><p class="eyebrow">SAMPLES</p><h3>采样明细</h3>
<p class="hint">「场景」和「脚本函数」用于和自动化用例对齐。监测中调用 <code>POST /api/v1/runs/{{runId}}/marker</code>，body 为 {{"scene":"进入大厅","scriptFn":"test_enter_lobby"}}，之后的每个采样点都会带上这两列。</p></div></div>
<div class="table-wrap"><table><thead><tr><th>时间</th><th>场景</th><th>脚本函数</th><th>FPS</th><th>CPU</th><th>Memory</th>{native_th}{swap_th}<th>GPU</th></tr></thead>
<tbody>{''.join(rows) or f'<tr><td colspan="{colspan}">无样本</td></tr>'}</tbody></table></div>
</section>
</div>
""" + _inspect_script(inspect_payload) + "</body></html>"
