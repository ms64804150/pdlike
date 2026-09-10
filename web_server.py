#!/usr/bin/env python3
"""Local Web Agent for the Android/iOS performance collectors."""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Empty, Queue
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
IOS_TOOL = ROOT / ".venv-ios" / "Scripts" / "pymobiledevice3.exe"
TIDEVICE = ROOT / ".venv-ios" / "Scripts" / "tidevice.exe"
REPORTS = ROOT / "web_reports"
REPORTS.mkdir(exist_ok=True)

sessions: dict[str, dict[str, Any]] = {}
sessions_lock = threading.Lock()


def save_report_record(session: dict[str, Any]) -> None:
    record = {
        "sessionId": session["id"],
        "status": session.get("status"),
        "reportReady": session.get("reportReady", False),
        "error": session.get("error"),
        "device": session.get("device"),
        "bundle": session.get("bundle"),
        "createdAt": session.get("createdAt", time.time()),
        "report": Path(session["report"]).name,
    }
    (REPORTS / f"{session['id']}.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def report_catalog() -> list[dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for record_path in REPORTS.glob("*.json"):
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(record, dict) and record.get("sessionId"):
            records[str(record["sessionId"])] = record
    for report_path in REPORTS.glob("*.html"):
        session_id = report_path.stem
        records.setdefault(
            session_id,
            {
                "sessionId": session_id,
                "status": "completed",
                "reportReady": True,
                "error": None,
                "device": {},
                "bundle": "历史报告",
                "createdAt": report_path.stat().st_mtime,
                "report": report_path.name,
            },
        )
    for session in sessions.values():
        if session.get("id"):
            records[session["id"]] = {
                "sessionId": session["id"],
                "status": session.get("status"),
                "reportReady": session.get("reportReady", False),
                "error": session.get("error"),
                "device": session.get("device"),
                "bundle": session.get("bundle"),
                "createdAt": session.get("createdAt", time.time()),
                "report": Path(session["report"]).name,
            }
    return sorted(records.values(), key=lambda item: item.get("createdAt", 0), reverse=True)


def run_json(command: list[str], timeout: int = 30) -> Any:
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=environment,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Agent command failed")
    return json.loads(result.stdout)


def devices() -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if IOS_TOOL.is_file():
        try:
            for item in run_json([str(IOS_TOOL), "usbmux", "list"]):
                found.append(
                    {
                        "id": item.get("UniqueDeviceID") or item.get("Identifier"),
                        "name": item.get("DeviceName") or "iPhone",
                        "model": item.get("ProductType"),
                        "platform": "ios",
                        "version": item.get("ProductVersion"),
                        "connection": item.get("ConnectionType", "USB"),
                        "status": "connected",
                    }
                )
        except (OSError, RuntimeError, ValueError):
            pass
    if not found and TIDEVICE.is_file():
        try:
            result = subprocess.run(
                [str(TIDEVICE), "list"], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=20,
            )
            for line in result.stdout.splitlines()[1:]:
                columns = line.split()
                if len(columns) >= 5 and columns[0] != "-":
                    found.append({
                        "id": columns[0], "name": columns[2],
                        "model": columns[3], "platform": "ios",
                        "version": columns[4], "connection": columns[5] if len(columns) > 5 else "USB",
                        "status": "connected",
                    })
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        result = subprocess.run(
            ["adb", "devices", "-l"], capture_output=True, text=True, timeout=10
        )
        for line in result.stdout.splitlines()[1:]:
            columns = line.split()
            if len(columns) >= 2 and columns[1] == "device":
                found.append(
                    {
                        "id": columns[0],
                        "name": next((x.split(":", 1)[1] for x in columns[2:] if x.startswith("model:")), columns[0]),
                        "model": "Android device",
                        "platform": "android",
                        "version": "Android",
                        "connection": "USB" if ":" not in columns[0] else "Wi-Fi",
                        "status": "connected",
                    }
                )
    except (OSError, subprocess.SubprocessError):
        pass
    return [item for item in found if item.get("id")]


def applications(device_id: str, platform: str) -> list[dict[str, str]]:
    if platform == "ios":
        if not TIDEVICE.is_file():
            return []
        try:
            result = subprocess.run(
                [str(TIDEVICE), "-u", device_id, "applist", "--type", "user"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=45,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )
            apps = []
            for line in result.stdout.splitlines():
                match = re.match(r"^(\S+)\s+(.+?)\s+([\d.]+)$", line.strip())
                if match:
                    apps.append({"bundle": match.group(1), "name": match.group(2), "version": match.group(3)})
            return apps
        except (OSError, subprocess.SubprocessError):
            return []
    try:
        result = subprocess.run(["adb", "-s", device_id, "shell", "pm", "list", "packages"], capture_output=True, text=True, timeout=30)
        return [{"bundle": line.removeprefix("package:").strip(), "name": line.removeprefix("package:").strip(), "version": ""} for line in result.stdout.splitlines() if line.startswith("package:")]
    except (OSError, subprocess.SubprocessError):
        return []


def capability(device: dict[str, Any], bundle: str) -> dict[str, Any]:
    platform = device.get("platform")
    indicators = [
        {"id": "fps", "label": "FPS", "state": "available", "source": "View / DVT"},
        {"id": "cpu", "label": "App CPU", "state": "available", "source": "Process monitor"},
        {"id": "memory", "label": "App Memory", "state": "available", "source": "RSS / PSS"},
        {"id": "battery", "label": "Battery", "state": "available", "source": "Device status"},
        {"id": "gpu", "label": "GPU", "state": "degraded", "source": "Device dependent"},
        {"id": "network", "label": "Network", "state": "degraded", "source": "Device dependent"},
    ]
    if platform == "android":
        indicators[0]["source"] = "SurfaceFlinger / gfxinfo"
    return {"deviceId": device.get("id"), "bundle": bundle, "indicators": indicators, "ready": True}


def parse_sample(line: str) -> dict[str, Any] | None:
    ios_match = re.search(r"FPS=\s*([\d.-]+).*?AppCPU=\s*([\d.-]+)%.*?(?:Memory|AppPSS)=\s*([\d.-]+)\s*MiB.*?GPU=\s*([\d.-]+)", line)
    if ios_match:
        values = [None if value == "-" else float(value) for value in ios_match.groups()]
        return {"time": time.time(), "fps": values[0], "cpu": values[1], "memory": values[2], "gpu": values[3], "networkDown": None, "networkUp": None}
    android_match = re.search(r"FPS=\s*([\d.-]+).*?AppCPU=\s*([\d.-]+)%.*?AppPSS=\s*([\d.-]+)\s*MB", line)
    if android_match:
        values = [None if value == "-" else float(value) for value in android_match.groups()]
        return {"time": time.time(), "fps": values[0], "cpu": values[1], "memory": values[2], "gpu": None, "networkDown": None, "networkUp": None}
    return None


def run_session(session_id: str, command: list[str]) -> None:
    session = sessions[session_id]
    try:
        process_options: dict[str, Any] = {}
        if os.name == "nt":
            process_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        process = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1, env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}, **process_options)
        session["process"] = process
        session["logs"] = []
        assert process.stdout is not None
        for line in process.stdout:
            stripped_line = line.strip()
            if stripped_line:
                session["logs"].append(stripped_line)
                session["logs"] = session["logs"][-40:]
            sample = parse_sample(line)
            if sample:
                session["samples"].append(sample)
                session["latest"] = sample
                session["events"].put({"type": "sample", "data": sample})
            elif "报告已生成" in line:
                session["reportReady"] = Path(session["report"]).is_file()
                session["events"].put({"type": "report", "data": {"ready": session["reportReady"]}})
            elif "已停止" in line:
                session["events"].put({"type": "log", "data": {"message": line.strip()}})
            elif "采样失败" in line or "报告生成失败" in line:
                session["error"] = line.strip()
        process.wait()
        session["status"] = "failed" if process.returncode else "completed"
        session["reportReady"] = Path(session["report"]).is_file()
        if process.returncode and not session.get("error"):
            session["error"] = (
                f"采集进程异常退出，退出码 {process.returncode}"
                + (f"；{session['logs'][-1]}" if session.get("logs") else "")
            )
        if not session["reportReady"] and not session.get("error"):
            session["error"] = "采集进程未生成报告"
        save_report_record(session)
        session["events"].put({
            "type": "status",
            "data": {
                "status": session["status"],
                "reportReady": session["reportReady"],
                "error": session.get("error"),
            },
        })
    except Exception as error:
        session["status"] = "failed"
        session["error"] = str(error)
        session["reportReady"] = False
        save_report_record(session)
        session["events"].put({"type": "error", "data": {"message": str(error)}})


class Handler(BaseHTTPRequestHandler):
    def send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/" or path == "/index.html":
            return self.serve_file(WEB_ROOT / "index.html", "text/html; charset=utf-8")
        if path in ("/styles.css", "/app.js"):
            file_path = WEB_ROOT / path.removeprefix("/")
            return self.serve_file(file_path, "text/plain; charset=utf-8" if file_path.suffix == ".js" else "text/css; charset=utf-8")
        if path == "/api/v1/devices":
            return self.send_json({"devices": devices(), "agent": {"status": "connected", "version": "0.1.0"}})
        if path == "/api/v1/reports":
            return self.send_json({"reports": report_catalog()})
        match = re.fullmatch(r"/api/v1/devices/([^/]+)/applications", path)
        if match:
            device = next((item for item in devices() if item["id"] == match.group(1)), None)
            return self.send_json({"applications": applications(match.group(1), device["platform"] if device else "ios")})
        match = re.fullmatch(r"/api/v1/sessions/([^/]+)/stream", path)
        if match:
            return self.stream(match.group(1))
        match = re.fullmatch(r"/api/v1/sessions/([^/]+)$", path)
        if match:
            session = sessions.get(match.group(1))
            if not session:
                return self.send_json({"error": "Session not found"}, 404)
            return self.send_json({"sessionId": session["id"], "status": session["status"], "reportReady": session.get("reportReady", False), "error": session.get("error")})
        match = re.fullmatch(r"/api/v1/sessions/([^/]+)/report", path)
        if match:
            session = sessions.get(match.group(1))
            report = Path(session["report"]) if session else REPORTS / f"{match.group(1)}.html"
            if not report.is_file():
                return self.send_json({"error": "Report is still being generated" if session else "Session not found", "status": session["status"] if session else "unknown"}, 409 if session else 404)
            return self.serve_file(report, "text/html; charset=utf-8")
        return self.send_json({"error": "Not found"}, 404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            data = self.read_json()
            if path == "/api/v1/capabilities":
                device_id = data.get("deviceId")
                device = next((item for item in devices() if item["id"] == device_id), {})
                return self.send_json(capability(device, data.get("bundle", "")))
            match = re.fullmatch(r"/api/v1/devices/([^/]+)/capabilities", path)
            if match:
                device = next((item for item in devices() if item["id"] == match.group(1)), {})
                return self.send_json(capability(device, data.get("bundle", "")))
            if path == "/api/v1/sessions":
                session_id = uuid.uuid4().hex[:10]
                report = REPORTS / f"{session_id}.html"
                stop_file = REPORTS / f"{session_id}.stop"
                session = {"id": session_id, "status": "ready", "device": data.get("device"), "bundle": data.get("bundle"), "report": str(report), "stopFile": str(stop_file), "reportReady": False, "samples": [], "latest": {}, "events": Queue(), "error": None, "createdAt": time.time()}
                sessions[session_id] = session
                return self.send_json({"sessionId": session_id, "status": "ready"}, 201)
            match = re.fullmatch(r"/api/v1/sessions/([^/]+)/start", path)
            if match:
                session = sessions[match.group(1)]
                session["status"] = "running"
                device = session["device"]
                if device.get("platform") == "android":
                    session["status"] = "ready"
                    command = [os.fspath(Path(os.sys.executable)), os.fspath(ROOT / "adb_fps.py"), "--package", session["bundle"], "--serial", device["id"], "--interval", "1", "--report", session["report"], "--stop-file", session["stopFile"]]
                else:
                    backend = "tidevice" if tuple(int(part) for part in device.get("version", "0").split(".")[:2]) < (17, 0) else "pymobiledevice3"
                    command = [os.fspath(Path(os.sys.executable)), os.fspath(ROOT / "ios_perf.py"), "--bundle", session["bundle"], "--udid", device["id"], "--backend", backend, "--report", session["report"], "--stop-file", session["stopFile"]]
                threading.Thread(target=run_session, args=(session["id"], command), daemon=True).start()
                return self.send_json({"sessionId": session["id"], "status": "running"})
            match = re.fullmatch(r"/api/v1/sessions/([^/]+)/stop", path)
            if match:
                session = sessions.get(match.group(1))
                if session and session.get("process") and session["process"].poll() is None:
                    Path(session["stopFile"]).touch()
                return self.send_json({"status": "finalizing"})
        except (KeyError, ValueError, RuntimeError, OSError) as error:
            return self.send_json({"error": str(error)}, 400)
        return self.send_json({"error": "Not found"}, 404)

    def stream(self, session_id: str) -> None:
        session = sessions.get(session_id)
        if not session:
            return self.send_json({"error": "Session not found"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            while True:
                try:
                    event = session["events"].get(timeout=1)
                    self.wfile.write(f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode())
                    self.wfile.flush()
                    if event["type"] in ("status", "error"):
                        break
                except Empty:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def serve_file(self, path: Path, content_type: str) -> None:
        if not path.is_file():
            return self.send_json({"error": "Not found"}, 404)
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


if __name__ == "__main__":
    port = int(os.environ.get("PERF_WEB_PORT", "8765"))
    print(f"Perf Web Agent: http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
