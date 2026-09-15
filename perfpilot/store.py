"""Persist each Run as run.json + samples.jsonl + report.html."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from .paths import runs_root


class RunStore:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.directory = runs_root() / run_id
        self.directory.mkdir(parents=True, exist_ok=True)
        self.meta_path = self.directory / "run.json"
        self.samples_path = self.directory / "samples.jsonl"
        self.report_path = self.directory / "report.html"
        self.stop_path = self.directory / "stop.flag"
        self.pid_path = self.directory / "collector.pid"
        self.logcat_pid_path = self.directory / "logcat.pid"
        self.extras_path = self.directory / "extras.json"
        self.markers_path = self.directory / "markers.jsonl"

    def write_extras(self, payload: dict[str, Any]) -> None:
        self.extras_path.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )

    def append_marker(self, payload: dict[str, Any]) -> None:
        with self.markers_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def write_meta(self, payload: dict[str, Any]) -> None:
        self.meta_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def append_sample(self, event: dict[str, Any]) -> None:
        with self.samples_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def read_meta(self) -> dict[str, Any]:
        if not self.meta_path.is_file():
            return {}
        try:
            return json.loads(self.meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def read_samples(self) -> list[dict[str, Any]]:
        if not self.samples_path.is_file():
            return []
        samples: list[dict[str, Any]] = []
        for line in self.samples_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                samples.append(item)
        return samples

    def write_report(self, html: str) -> None:
        self.report_path.write_text(html, encoding="utf-8")

    def purge(self) -> None:
        shutil.rmtree(self.directory, ignore_errors=True)


def list_runs() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for directory in sorted(runs_root().iterdir(), reverse=True):
        meta_path = directory / "run.json"
        if not meta_path.is_file():
            continue
        try:
            record = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(record, dict):
            records.append(record)
    return sorted(records, key=lambda item: item.get("startedAtMs", 0), reverse=True)
