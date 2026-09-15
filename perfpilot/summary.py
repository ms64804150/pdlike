"""Compute a compact metric summary from structured samples."""

from __future__ import annotations

from statistics import mean, median
from typing import Any, Callable, Optional


def _values(samples: list[dict[str, Any]], key: str) -> list[float]:
    values: list[float] = []
    for item in samples:
        metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else item
        value = metrics.get(key) if isinstance(metrics, dict) else None
        if isinstance(value, bool) or value is None:
            continue
        try:
            values.append(float(value))
        except (TypeError, ValueError):
            continue
    return values


def _percentile(values: list[float], ratio: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * ratio))))
    return ordered[index]


def _ratio(values: list[float], predicate: Callable[[float], bool]) -> Optional[float]:
    if not values:
        return None
    return sum(1 for value in values if predicate(value)) / len(values) * 100.0


def summarize(samples: list[dict[str, Any]]) -> dict[str, Any]:
    fps = _values(samples, "fps")
    cpu = _values(samples, "appCpuPct") or _values(samples, "cpu")
    memory = _values(samples, "memoryMiB") or _values(samples, "memory")
    gpu = _values(samples, "gpuPct") or _values(samples, "gpu")
    missing = 0
    for item in samples:
        metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else item
        if not isinstance(metrics, dict) or metrics.get("fps") is None:
            missing += 1
    return {
        "sampleCount": len(samples),
        "missingRatePct": (missing / len(samples) * 100.0) if samples else None,
        "fps": {
            "avg": mean(fps) if fps else None,
            "min": min(fps) if fps else None,
            "p10": _percentile(fps, 0.10),
            "p50": median(fps) if fps else None,
            "p90": _percentile(fps, 0.90),
            "below25Pct": _ratio(fps, lambda value: value < 25),
        },
        "cpu": {
            "avg": mean(cpu) if cpu else None,
            "p50": median(cpu) if cpu else None,
            "p90": _percentile(cpu, 0.90),
            "p95": _percentile(cpu, 0.95),
            "peak": max(cpu) if cpu else None,
        },
        "memory": {
            "avg": mean(memory) if memory else None,
            "peak": max(memory) if memory else None,
            "delta": (memory[-1] - memory[0]) if len(memory) >= 2 else None,
        },
        "gpu": {
            "avg": mean(gpu) if gpu else None,
            "p90": _percentile(gpu, 0.90),
            "peak": max(gpu) if gpu else None,
        },
    }
