"""Run / session manager: one active collection per device."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import uuid
from contextlib import nullcontext
from pathlib import Path
from queue import Queue
from typing import Any, Optional

from . import events
from .collectors import collector_command, decode_collector_line, parse_collector_line, start_collector
from .logcat import run_package_logcat
from .logutil import get_logger
from .report import render_report, report_display_name
from .store import RunStore, list_runs
from .summary import summarize

UI_LOG_LIMIT = 500


def _read_pid_file(path: Path) -> Optional[int]:
    try:
        text = path.read_text(encoding="utf-8").strip()
        pid = int(text)
        return pid if pid > 0 else None
    except (OSError, ValueError):
        return None


def _write_pid_file(path: Path, pid: int) -> None:
    try:
        path.write_text(str(pid), encoding="utf-8")
    except OSError:
        pass


def _clear_pid_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _read_pid(store: RunStore) -> Optional[int]:
    return _read_pid_file(store.pid_path)


def _write_pid(store: RunStore, pid: int) -> None:
    _write_pid_file(store.pid_path, pid)


def default_extras(device: Optional[dict[str, Any]] = None) -> dict[str, bool]:
    android = str((device or {}).get("platform") or "").lower() == "android"
    return {"nativePss": android, "swapPss": android, "logcat": False}


def _clear_pid(store: RunStore) -> None:
    try:
        store.pid_path.unlink(missing_ok=True)
    except OSError:
        pass


def _collector_failure_message(logs: list[str], returncode: int) -> str:
    skip = ("报告已生成", "已停止：", "启动采集:")
    for text in reversed(logs):
        cleaned = text.replace("\ufffd", "").strip()
        if not cleaned or any(marker in cleaned for marker in skip):
            continue
        if "sitecustomize" in cleaned.lower() or "pythonverbose" in cleaned.lower():
            continue
        if cleaned.startswith("│") or "exec_module" in cleaned:
            continue
        if "采样失败" in cleaned or "监测异常" in cleaned or "无法" in cleaned:
            return cleaned
        if cleaned.isascii() and ("error" in cleaned.lower() or "fail" in cleaned.lower()):
            return f"采集失败：{cleaned}"
    return f"采集进程异常退出，退出码 {returncode}"


def _kill_pid(pid: Optional[int]) -> None:
    if not pid:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=8,
            )
        else:
            os.kill(pid, signal.SIGTERM)
    except (OSError, subprocess.SubprocessError):
        pass


class DeviceBusyError(RuntimeError):
    def __init__(self, run_id: str) -> None:
        super().__init__(f"设备已被 Run {run_id} 占用")
        self.run_id = run_id


class NotForegroundError(RuntimeError):
    def __init__(self, bundle: str, foreground: str) -> None:
        super().__init__(f"应用不在前台（当前前台：{foreground}）")
        self.bundle = bundle
        self.foreground = foreground


class SessionManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.runs: dict[str, dict[str, Any]] = {}
        self.device_locks: dict[str, str] = {}
        self.reclaim_stale_collectors()

    def reclaim_stale_collectors(self) -> None:
        """Stop leftover collectors after Agent crash / restart."""
        for record in list_runs():
            if record.get("status") not in ("ready", "running", "finalizing"):
                continue
            run_id = str(record.get("runId") or "")
            if not run_id:
                continue
            store = RunStore(run_id)
            try:
                store.stop_path.touch()
            except OSError:
                pass
            _kill_pid(_read_pid(store))
            _kill_pid(_read_pid_file(store.logcat_pid_path))
            _clear_pid(store)
            _clear_pid_file(store.logcat_pid_path)
            record["status"] = "failed"
            record["error"] = "Agent 已重启，监测已中止"
            record["endedAtMs"] = events.now_ms()
            record["reportReady"] = bool(record.get("reportReady"))
            try:
                store.write_meta(record)
            except OSError:
                pass

    def shutdown(self) -> None:
        for run_id, session in list(self.runs.items()):
            store: Optional[RunStore] = session.get("store")
            try:
                self.stop(run_id)
            except Exception:
                pass
            process = session.get("process")
            pid = process.pid if process else (_read_pid(store) if store else None)
            _kill_pid(pid)
            self._stop_logcat(session)
            if store:
                _clear_pid(store)
                _clear_pid_file(store.logcat_pid_path)

    def _release_device(self, run_id: str) -> None:
        with self._lock:
            for device_id, locked_run in list(self.device_locks.items()):
                if locked_run == run_id:
                    self.device_locks.pop(device_id, None)

    def _is_collecting(self, session: Optional[dict[str, Any]]) -> bool:
        if not session:
            return False
        status = session.get("status")
        process = session.get("process")
        if status == "running":
            return process is None or process.poll() is None
        if status == "ready":
            started = int(session.get("startedAtMs") or 0)
            return events.now_ms() - started < 8000
        return False

    def has_active_collection(self) -> bool:
        with self._lock:
            sessions = tuple(self.runs.values())
        return any(self._is_collecting(session) for session in sessions)

    def create(
        self,
        device: dict[str, Any],
        bundle: str,
        source: str = "manual",
        app: Optional[dict[str, Any]] = None,
        capabilities: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        device_id = str(device.get("id") or "")
        app_info = app if isinstance(app, dict) else {}
        from .devices import format_app_version, logs_capability, package_identity

        identity = package_identity(device if isinstance(device, dict) else {}, bundle)
        app_version = str(identity.get("version") or app_info.get("version") or "").strip()
        app_version_code = str(identity.get("versionCode") or app_info.get("versionCode") or "").strip()
        app_version_label = format_app_version(app_version, app_version_code)
        get_logger("session").info(
            "[AppVersion] create platform=%s bundle=%s versionName=%s versionCode=%s source=%s",
            (device or {}).get("platform"),
            bundle,
            app_version,
            app_version_code,
            "dumpsys" if identity.get("version") or identity.get("versionCode") else "client",
        )
        with self._lock:
            occupied = self.device_locks.get(device_id)
            if occupied:
                other = self.runs.get(occupied)
                if self._is_collecting(other):
                    raise DeviceBusyError(occupied)
                self.device_locks.pop(device_id, None)
            run_id = events.new_run_id()
            store = RunStore(run_id)
            started = events.now_ms()
            extras = default_extras(device)
            app_name = str(app_info.get("name") or "").strip() or bundle
            cap_list: list[dict[str, Any]] = []
            if isinstance(capabilities, list):
                for item in capabilities[:20]:
                    if not isinstance(item, dict):
                        continue
                    cap_list.append(
                        {
                            "id": str(item.get("id") or "")[:40],
                            "label": str(item.get("label") or "")[:80],
                            "state": str(item.get("state") or "")[:20],
                            "source": str(item.get("source") or "")[:120],
                        }
                    )
            if not any(item.get("id") == "logs" for item in cap_list):
                cap_list.append(logs_capability((device or {}).get("platform"), bundle))
            record = {
                "schemaVersion": 1,
                "runId": run_id,
                "source": source,
                "packageId": bundle,
                "bundle": bundle,
                "appName": app_name,
                "appVersion": app_version_label or app_version,
                "versionName": app_version,
                "versionCode": app_version_code,
                "capabilities": cap_list,
                "device": device,
                "status": "ready",
                "startedAtMs": started,
                "endedAtMs": None,
                "error": None,
                "reportReady": False,
                "extras": extras,
                "marker": {"scene": "", "scriptFn": ""},
            }
            store.write_meta(record)
            store.write_extras(extras)
            get_logger("session").info(
                "[StartMonitor] create runId=%s package=%s appName=%s appVersion=%s versionCode=%s capCount=%s",
                run_id,
                bundle,
                app_name,
                app_version_label or app_version,
                app_version_code,
                len(cap_list),
            )
            get_logger("session").info(
                "[AndroidSample] extras default runId=%s platform=%s nativePss=%s swapPss=%s logcat=%s",
                run_id,
                device.get("platform") if isinstance(device, dict) else None,
                extras["nativePss"],
                extras["swapPss"],
                extras.get("logcat"),
            )
            session = {
                **record,
                "store": store,
                "samples": [],
                "latest": {},
                "events": Queue(),
                "process": None,
                "logcat_process": None,
                "logcat_stop": threading.Event(),
                "logcat_active": False,
                "log_lock": threading.Lock(),
                "logs": [],
                "collector_logs": [],
                "viewers": set(),
                "orphan_timer": None,
                "keepData": True,
                "wantReport": True,
            }
            self.runs[run_id] = session
            if device_id:
                self.device_locks[device_id] = run_id
            return session

    def set_extras(self, run_id: str, extras: dict[str, Any]) -> dict[str, bool]:
        session = self.runs.get(run_id)
        if not session:
            raise KeyError(run_id)
        current = session.get("extras") or default_extras(session.get("device"))
        payload = {
            "nativePss": bool(extras["nativePss"]) if "nativePss" in extras else bool(current.get("nativePss")),
            "swapPss": bool(extras["swapPss"]) if "swapPss" in extras else bool(current.get("swapPss")),
            "logcat": bool(extras["logcat"]) if "logcat" in extras else bool(current.get("logcat")),
        }
        session["extras"] = payload
        store: RunStore = session["store"]
        store.write_extras(payload)
        meta = store.read_meta()
        if meta:
            meta["extras"] = payload
            store.write_meta(meta)
        log = get_logger("session")
        log.info(
            "[AndroidSample] extras update runId=%s nativePss=%s swapPss=%s logcat=%s package=%s",
            run_id,
            payload["nativePss"],
            payload["swapPss"],
            payload["logcat"],
            session.get("bundle"),
        )
        if payload["logcat"]:
            self._start_logcat(run_id)
        elif session.get("logcat_active") or session.get("logcat_process"):
            log.info("[Logcat] disable runId=%s package=%s", run_id, session.get("bundle"))
            self._append_ui_log(session, "已停止 logcat 采集")
            self._stop_logcat(session)
        return payload

    def set_marker(self, run_id: str, payload: dict[str, Any]) -> dict[str, str]:
        session = self.runs.get(run_id)
        if not session:
            raise KeyError(run_id)
        scene = str(payload.get("scene") or payload.get("name") or "").strip()
        script_fn = str(
            payload.get("scriptFn") or payload.get("script") or payload.get("function") or ""
        ).strip()
        marker = {"scene": scene, "scriptFn": script_fn}
        session["marker"] = marker
        store: RunStore = session["store"]
        started = int(session.get("startedAtMs") or 0)
        record = {
            **marker,
            "timestampMs": events.now_ms(),
            "elapsedMs": max(0, events.now_ms() - started) if started else 0,
        }
        store.append_marker(record)
        log = get_logger("session")
        log.info(
            "[SceneMarker] set runId=%s scene=%s scriptFn=%s elapsedMs=%s package=%s",
            run_id,
            scene,
            script_fn,
            record["elapsedMs"],
            session.get("bundle"),
        )
        return marker

    def public_run(self, session: dict[str, Any], include_samples: bool = False) -> dict[str, Any]:
        payload = {
            "runId": session["runId"],
            "sessionId": session["runId"],
            "status": session.get("status"),
            "reportReady": session.get("reportReady", False),
            "error": session.get("error"),
            "device": session.get("device"),
            "bundle": session.get("bundle"),
            "packageId": session.get("packageId"),
            "appName": session.get("appName"),
            "appVersion": session.get("appVersion"),
            "versionName": session.get("versionName"),
            "versionCode": session.get("versionCode"),
            "startedAtMs": session.get("startedAtMs"),
            "endedAtMs": session.get("endedAtMs"),
            "sampleCount": len(session.get("samples") or []),
            "keepData": bool(session.get("keepData", True)),
            "wantReport": bool(session.get("wantReport", True)),
            "discarded": session.get("status") == "discarded",
            "extras": session.get("extras") or default_extras(session.get("device")),
            "capabilities": session.get("capabilities") or [],
        }
        if include_samples:
            payload["samples"] = (session.get("samples") or [])[-600:]
            payload["logs"] = [
                str(line)
                for line in (session.get("logs") or [])
                if "PERFPILOT_SAMPLE" not in str(line)
            ][-UI_LOG_LIMIT:]
        return payload

    def active(self) -> list[dict[str, Any]]:
        log = get_logger("session")
        found: list[dict[str, Any]] = []
        skipped = 0
        for session in self.runs.values():
            status = session.get("status")
            if status in ("completed", "failed", "discarded"):
                skipped += 1
                continue
            if status == "finalizing":
                process = session.get("process")
                if process is not None and process.poll() is not None:
                    skipped += 1
                    continue
            if status not in ("ready", "running", "finalizing"):
                skipped += 1
                continue
            found.append(self.public_run(session, include_samples=True))
        log.info("[MonitorTab] active listed=%s skipped=%s total=%s", len(found), skipped, len(self.runs))
        return found

    def attach_viewer(self, run_id: str) -> Optional[str]:
        session = self.runs.get(run_id)
        if not session:
            return None
        token = uuid.uuid4().hex
        viewers: set[str] = session.setdefault("viewers", set())
        viewers.add(token)
        timer = session.get("orphan_timer")
        if timer:
            timer.cancel()
            session["orphan_timer"] = None
        return token

    def detach_viewer(self, run_id: str, token: Optional[str]) -> None:
        session = self.runs.get(run_id)
        if not session:
            return
        viewers: set[str] = session.setdefault("viewers", set())
        if token:
            viewers.discard(token)
        if viewers:
            return
        log = get_logger("session")
        log.info(
            "[StopMonitor] viewers empty keep collecting runId=%s status=%s",
            run_id,
            session.get("status"),
        )

    def _stop_orphan(self, run_id: str) -> None:
        session = self.runs.get(run_id)
        if not session:
            return
        if session.get("viewers"):
            return
        if session.get("status") != "running":
            return
        self.stop(run_id)

    def get(self, run_id: str) -> Optional[dict[str, Any]]:
        return self.runs.get(run_id)

    def catalog(self) -> list[dict[str, Any]]:
        records = {
            item["runId"]: item
            for item in list_runs()
            if item.get("runId") and item.get("status") != "discarded"
        }
        for session in self.runs.values():
            if session.get("status") == "discarded":
                continue
            records[session["runId"]] = {
                "runId": session["runId"],
                "sessionId": session["runId"],
                "status": session.get("status"),
                "reportReady": session.get("reportReady", False),
                "error": session.get("error"),
                "device": session.get("device"),
                "bundle": session.get("bundle"),
                "packageId": session.get("packageId"),
                "appName": session.get("appName"),
                "appVersion": session.get("appVersion"),
                "versionName": session.get("versionName"),
                "versionCode": session.get("versionCode"),
                "createdAt": (session.get("startedAtMs") or 0) / 1000,
                "startedAtMs": session.get("startedAtMs"),
                "report": "report.html",
            }
        result = sorted(records.values(), key=lambda item: item.get("startedAtMs") or item.get("createdAt") or 0, reverse=True)
        for item in result:
            item.setdefault("sessionId", item.get("runId"))
            item.setdefault("createdAt", (item.get("startedAtMs") or 0) / 1000)
            item["title"] = report_display_name(item)
        return result

    def start(self, run_id: str) -> dict[str, Any]:
        from .android_fg import foreground_package

        session = self.runs[run_id]
        device = session.get("device") or {}
        bundle = str(session.get("bundle") or "")
        if device.get("platform") != "ios" and bundle:
            current = foreground_package(str(device.get("id") or ""))
            if current and current != bundle:
                self._release_device(run_id)
                session["status"] = "failed"
                session["error"] = f"应用不在前台（当前前台：{current}）"
                raise NotForegroundError(bundle, current)
        store: RunStore = session["store"]
        session["status"] = "running"
        session["startedAtMs"] = events.now_ms()
        command = collector_command(
            session["device"],
            session["bundle"],
            store.report_path,
            store.stop_path,
            extras_file=store.extras_path,
        )
        thread = threading.Thread(target=self._run_collector, args=(run_id, command), daemon=True)
        thread.start()
        self._start_logcat(run_id)
        return session

    def stop(self, run_id: str, save: bool = True, report: bool = True) -> None:
        session = self.runs.get(run_id)
        if not session:
            return
        log = get_logger("session")
        status = session.get("status")
        if status in ("completed", "failed", "discarded"):
            log.info("[StopMonitor] stop skip already ended runId=%s status=%s", run_id, status)
            self._release_device(run_id)
            return
        keep_data = bool(save)
        want_report = bool(report) and keep_data
        if status == "finalizing":
            log.info(
                "[StopMonitor] stop skip already finalizing runId=%s keep=%s report=%s samples=%s",
                run_id,
                session.get("keepData", True),
                session.get("wantReport", True),
                len(session.get("samples") or []),
            )
            return
        session["keepData"] = keep_data
        session["wantReport"] = want_report
        session["status"] = "finalizing"
        log.info(
            "[StopMonitor] stop runId=%s keep=%s report=%s samples=%s package=%s",
            run_id,
            keep_data,
            want_report,
            len(session.get("samples") or []),
            session.get("bundle"),
        )
        Path(session["store"].stop_path).touch()
        process = session.get("process")
        if process and process.poll() is None:
            threading.Timer(0.8, lambda: process.poll() is None and process.terminate()).start()
        self._stop_logcat(session)
        self._release_device(run_id)

    def _publish(self, session: dict[str, Any], event: dict[str, Any]) -> None:
        session["events"].put(event)

    def _stop_logcat(self, session: dict[str, Any]) -> None:
        event = session.get("logcat_stop")
        if event is not None:
            try:
                event.set()
            except Exception:
                pass
        process = session.get("logcat_process")
        pid = process.pid if process is not None else None
        if pid:
            _kill_pid(pid)
        session["logcat_process"] = None
        store = session.get("store")
        if store:
            _clear_pid_file(store.logcat_pid_path)

    def _start_logcat(self, run_id: str) -> None:
        session = self.runs.get(run_id)
        if not session:
            return
        log = get_logger("session")
        extras = session.get("extras") or {}
        device = session.get("device") or {}
        bundle = str(session.get("bundle") or session.get("packageId") or "")
        android = str(device.get("platform") or "").lower() == "android"
        if not android or not bundle:
            log.info("[Logcat] skip runId=%s platform=%s package=%s", run_id, device.get("platform"), bundle)
            return
        if not extras.get("logcat"):
            log.info("[Logcat] skip disabled runId=%s package=%s", run_id, bundle)
            return
        if session.get("status") not in ("ready", "running"):
            log.info("[Logcat] skip status runId=%s status=%s", run_id, session.get("status"))
            return
        if session.get("logcat_active"):
            log.info("[Logcat] skip already active runId=%s package=%s", run_id, bundle)
            return
        session["logcat_stop"] = threading.Event()
        session["_logcatConnectedLogged"] = False
        session["logcat_active"] = True
        threading.Thread(
            target=self._run_logcat,
            args=(run_id,),
            daemon=True,
            name=f"logcat-{run_id}",
        ).start()
        log.info(
            "[Logcat] start scheduled runId=%s package=%s serial=%s",
            run_id,
            bundle,
            device.get("id"),
        )

    def _append_ui_log(self, session: dict[str, Any], line: str) -> None:
        text = str(line or "").replace("\r", " ").strip()
        if not text:
            return
        clipped = text[:1000]
        with session.get("log_lock") or nullcontext():
            logs = session.setdefault("logs", [])
            logs.append(clipped)
            session["logs"] = logs[-UI_LOG_LIMIT:]
        self._publish(session, {"type": "log", "data": {"line": clipped}})

    def _run_logcat(self, run_id: str) -> None:
        session = self.runs.get(run_id)
        if not session:
            return
        log = get_logger("session")
        device = session.get("device") or {}
        serial = str(device.get("id") or "")
        package = str(session.get("bundle") or session.get("packageId") or "")
        store: RunStore = session["store"]
        stop_event = session.get("logcat_stop")
        if stop_event is None:
            stop_event = threading.Event()
            session["logcat_stop"] = stop_event
        log.info("[Logcat] thread start runId=%s serial=%s package=%s", run_id, serial, package)
        log_handle = None
        flush_every = 0
        try:
            log_path = store.directory / "logcat.log"
            log_handle = log_path.open("a" if log_path.is_file() else "w", encoding="utf-8", errors="replace")
        except OSError:
            log.exception("[Logcat] open file failed runId=%s", run_id)

        def should_stop() -> bool:
            if stop_event is not None and getattr(stop_event, "is_set", lambda: False)():
                return True
            if not bool((session.get("extras") or {}).get("logcat")):
                return True
            try:
                if Path(store.stop_path).is_file():
                    return True
            except OSError:
                pass
            return session.get("status") not in ("ready", "running")

        def on_line(line: str) -> None:
            nonlocal flush_every
            if log_handle:
                try:
                    log_handle.write(line + "\n")
                    flush_every += 1
                    if flush_every >= 10:
                        log_handle.flush()
                        flush_every = 0
                except OSError:
                    pass
            self._append_ui_log(session, line)

        def on_process(proc: Optional[subprocess.Popen[bytes]]) -> None:
            session["logcat_process"] = proc
            if proc is not None:
                _write_pid_file(store.logcat_pid_path, proc.pid)
                log.info("[Logcat] process ready runId=%s pid=%s package=%s", run_id, proc.pid, package)
                if not session.get("_logcatConnectedLogged"):
                    session["_logcatConnectedLogged"] = True
                    self._append_ui_log(session, f"logcat 已连接 package={package}")
            else:
                _clear_pid_file(store.logcat_pid_path)

        try:
            run_package_logcat(
                serial,
                package,
                should_stop,
                on_line,
                on_process=on_process,
                run_id=run_id,
            )
        except Exception:
            log.exception("[Logcat] thread failed runId=%s package=%s", run_id, package)
            self._append_ui_log(session, f"logcat 采集失败 package={package}")
        finally:
            if log_handle:
                try:
                    log_handle.flush()
                    log_handle.close()
                except OSError:
                    pass
            session["logcat_process"] = None
            session["logcat_active"] = False
            _clear_pid_file(store.logcat_pid_path)
            log.info("[Logcat] thread end runId=%s package=%s", run_id, package)

    def _run_collector(self, run_id: str, command: list[str]) -> None:
        session = self.runs[run_id]
        store: RunStore = session["store"]
        try:
            process = start_collector(command)
            session["process"] = process
            android = str((session.get("device") or {}).get("platform") or "").lower() == "android"
            _write_pid(store, process.pid)
            log = get_logger("session")
            log.info("[MetricTab] collector start runId=%s android=%s cmd=%s", run_id, android, " ".join(command))
            if not android:
                self._append_ui_log(session, "启动采集: " + " ".join(command))
            assert process.stdout is not None
            log_file = (store.directory / "collector.log").open("w", encoding="utf-8", errors="replace")
            try:
                for raw in process.stdout:
                    text = decode_collector_line(raw)
                    if text:
                        collector_logs = session.setdefault("collector_logs", [])
                        collector_logs.append(text)
                        session["collector_logs"] = collector_logs[-80:]
                        if not android:
                            lock = session.get("log_lock")
                            if lock is not None:
                                with lock:
                                    session.setdefault("logs", []).append(text)
                                    session["logs"] = session["logs"][-UI_LOG_LIMIT:]
                            else:
                                session.setdefault("logs", []).append(text)
                                session["logs"] = session["logs"][-UI_LOG_LIMIT:]
                        try:
                            log_file.write(text + "\n")
                            log_file.flush()
                        except OSError:
                            pass
                        if text.startswith("[IosPerf]") or "采样失败" in text:
                            log.info("[Collector] runId=%s out=%s", run_id, text[:500])
                    if "PERFPILOT_EVENT" in text and "background" in text:
                        self._publish(session, {"type": "warning", "data": {"code": "background", "message": text}})
                        continue
                    if "PERFPILOT_EVENT" in text and "foreground" in text:
                        self._publish(session, {"type": "warning", "data": {"code": "foreground", "message": text}})
                        continue
                    parsed = parse_collector_line(text)
                    if parsed:
                        extra_metrics: dict[str, Any] = {
                            key: parsed[key]
                            for key in ("nativePss", "swapPss")
                            if key in parsed
                        }
                        marker = session.get("marker") or {}
                        scene = parsed.get("scene") or marker.get("scene")
                        script_fn = (
                            parsed.get("scriptFn")
                            or parsed.get("script")
                            or parsed.get("function")
                            or marker.get("scriptFn")
                        )
                        if scene:
                            extra_metrics["scene"] = str(scene)
                        if script_fn:
                            extra_metrics["scriptFn"] = str(script_fn)
                        if extra_metrics:
                            count = int(session.get("_extrasSampleCount") or 0) + 1
                            session["_extrasSampleCount"] = count
                            if count <= 3 or count % 30 == 0:
                                log.info(
                                    "[AndroidSample] sample extras runId=%s n=%s nativePss=%s swapPss=%s scene=%s scriptFn=%s",
                                    run_id,
                                    count,
                                    extra_metrics.get("nativePss"),
                                    extra_metrics.get("swapPss"),
                                    extra_metrics.get("scene"),
                                    extra_metrics.get("scriptFn"),
                                )
                        event = events.sample_event(
                            run_id,
                            session["startedAtMs"],
                            parsed.get("fps"),
                            parsed.get("cpu"),
                            parsed.get("memory"),
                            parsed.get("gpu"),
                            extra_metrics=extra_metrics,
                        )
                        session["samples"].append(event)
                        session["latest"] = event
                        store.append_sample(event)
                        self._publish(session, {"type": "sample", "data": event})
                    elif "应用不在前台" in text or text.startswith("监测异常"):
                        self._publish(session, {"type": "warning", "data": {"code": "background", "message": text}})
                    elif "应用已回到前台" in text or text.startswith("监测恢复"):
                        self._publish(session, {"type": "warning", "data": {"code": "foreground", "message": text}})
                    elif "采样失败" in text or "报告生成失败" in text:
                        session["error"] = text
                        self._publish(session, {"type": "error", "data": {"message": text}})
                    else:
                        line = text.replace("\r", " ").strip()
                        if line and "PERFPILOT_SAMPLE" not in line and "PERFPILOT_EVENT" not in line:
                            count = int(session.get("_uiLogCount") or 0) + 1
                            session["_uiLogCount"] = count
                            if count <= 5 or count % 40 == 0:
                                log.info("[MetricTab] log runId=%s n=%s preview=%s", run_id, count, line[:120])
                            if not android:
                                self._publish(session, {"type": "log", "data": {"line": line[:400]}})
            finally:
                log_file.close()
            process.wait()
            samples = session["samples"] or store.read_samples()
            summary = summarize(samples)
            ended = events.now_ms()
            stopped = Path(store.stop_path).is_file()
            failed = bool(process.returncode) and not stopped
            keep_data = bool(session.get("keepData", True))
            want_report = bool(session.get("wantReport", True)) and keep_data
            log.info(
                "[StopMonitor] collector exit runId=%s keep=%s report=%s samples=%s code=%s stopped=%s failed=%s",
                run_id,
                keep_data,
                want_report,
                len(samples),
                process.returncode,
                stopped,
                failed,
            )
            if not keep_data:
                session["status"] = "discarded"
                session["reportReady"] = False
                session["endedAtMs"] = ended
                session["error"] = None
                try:
                    store.purge()
                    log.info("[StopMonitor] purge ok runId=%s samples=%s", run_id, len(samples))
                except OSError:
                    log.exception("[StopMonitor] purge failed runId=%s", run_id)
                self._publish(
                    session,
                    {
                        "type": "status",
                        "data": {
                            "status": "discarded",
                            "reportReady": False,
                            "discarded": True,
                            "sampleCount": len(samples),
                        },
                    },
                )
                return
            session["status"] = "failed" if failed else "completed"
            session["endedAtMs"] = ended
            if failed and not session.get("error"):
                session["error"] = _collector_failure_message(
                    session.get("collector_logs") or session.get("logs") or [],
                    process.returncode,
                )
                log.warning(
                    "[Collector] run failed runId=%s code=%s message=%s",
                    run_id,
                    process.returncode,
                    session["error"],
                )
            meta = {
                "schemaVersion": 1,
                "runId": run_id,
                "source": session.get("source", "manual"),
                "packageId": session.get("packageId"),
                "bundle": session.get("bundle"),
                "appName": session.get("appName"),
                "appVersion": session.get("appVersion"),
                "versionName": session.get("versionName"),
                "versionCode": session.get("versionCode"),
                "capabilities": session.get("capabilities") or [],
                "device": session.get("device"),
                "status": session["status"],
                "startedAtMs": session.get("startedAtMs"),
                "endedAtMs": ended,
                "error": session.get("error"),
                "summary": summary,
                "reportReady": False,
                "wantReport": want_report,
                "extras": session.get("extras") or {},
                "marker": session.get("marker") or {},
            }
            if want_report:
                html = render_report(meta, samples)
                store.write_report(html)
                meta["reportReady"] = store.report_path.is_file()
            else:
                meta["reportReady"] = False
                log.info("[StopMonitor] skip report runId=%s samples=%s", run_id, len(samples))
            session["reportReady"] = meta["reportReady"]
            store.write_meta(meta)
            if want_report and not session["reportReady"] and not session.get("error"):
                session["error"] = "采集进程未生成报告"
            self._publish(
                session,
                {
                    "type": "status",
                    "data": {
                        "status": session["status"],
                        "reportReady": session["reportReady"],
                        "error": session.get("error"),
                        "summary": summary,
                        "wantReport": want_report,
                    },
                },
            )
        except Exception as error:
            log = get_logger("session")
            if not session.get("keepData", True):
                session["status"] = "discarded"
                session["reportReady"] = False
                session["endedAtMs"] = events.now_ms()
                try:
                    store.purge()
                    log.info("[StopMonitor] purge after error runId=%s err=%s", run_id, error)
                except OSError:
                    log.exception("[StopMonitor] purge failed runId=%s", run_id)
                self._publish(
                    session,
                    {
                        "type": "status",
                        "data": {
                            "status": "discarded",
                            "reportReady": False,
                            "discarded": True,
                            "error": str(error),
                        },
                    },
                )
                return
            session["status"] = "failed"
            session["error"] = str(error)
            session["reportReady"] = False
            store.write_meta(
                {
                    "schemaVersion": 1,
                    "runId": run_id,
                    "packageId": session.get("packageId"),
                    "device": session.get("device"),
                    "status": "failed",
                    "error": str(error),
                    "startedAtMs": session.get("startedAtMs"),
                    "endedAtMs": events.now_ms(),
                    "reportReady": False,
                }
            )
            self._publish(session, {"type": "error", "data": {"message": str(error)}})
        finally:
            _clear_pid(store)
            self._stop_logcat(session)
            self._release_device(run_id)


manager = SessionManager()
