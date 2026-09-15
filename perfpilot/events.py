"""Unified Run event schema consumed by the local UI and reports."""

from __future__ import annotations

import time
import uuid
from typing import Any, Optional


SCHEMA_VERSION = 1


def new_run_id() -> str:
    return uuid.uuid4().hex[:16]


def now_ms() -> int:
    return int(time.time() * 1000)


def sample_event(
    run_id: str,
    started_at_ms: int,
    fps: Optional[float],
    app_cpu_pct: Optional[float],
    memory_mib: Optional[float],
    gpu_pct: Optional[float] = None,
    network_down_mib: Optional[float] = None,
    network_up_mib: Optional[float] = None,
    extra_metrics: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    timestamp_ms = now_ms()
    metrics = {
        "fps": fps,
        "appCpuPct": app_cpu_pct,
        "memoryMiB": memory_mib,
        "gpuPct": gpu_pct,
        "networkDownMiB": network_down_mib,
        "networkUpMiB": network_up_mib,
    }
    extras = extra_metrics or {}
    metrics.update(extras)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "type": "sample",
        "runId": run_id,
        "timestampMs": timestamp_ms,
        "elapsedMs": max(0, timestamp_ms - started_at_ms),
        "metrics": metrics,
        # Flattened fields keep the existing Web console simple.
        "fps": fps,
        "cpu": app_cpu_pct,
        "memory": memory_mib,
        "gpu": gpu_pct,
        "networkDown": network_down_mib,
        "networkUp": network_up_mib,
        **extras,
        "time": timestamp_ms / 1000,
    }


def status_event(run_id: str, status: str, **extra: Any) -> dict[str, Any]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "type": "status",
        "runId": run_id,
        "timestampMs": now_ms(),
        "status": status,
        **extra,
    }
