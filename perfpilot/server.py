"""Local HTTP Agent: 127.0.0.1 only, Phase 1 Run API."""

from __future__ import annotations

import json
import os
import re
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Empty
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import __version__
from .devices import applications, capabilities
from .logutil import get_logger
from .paths import WEB_ROOT, data_root
from .report import render_report, report_filename
from .session import DeviceBusyError, NotForegroundError, manager
from .android_fg import foreground_info
from .store import RunStore
from .watch import registry


class UiLifecycle:
    """Track browser clients without controlling the local Agent lifetime."""

    reconnect_grace_seconds = 5
    heartbeat_timeout_seconds = 30

    def __init__(self) -> None:
        self._clients: dict[str, float] = {}
        self._seen_client = False
        self._closed = False
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def connect(self, client_id: str) -> None:
        self._touch(client_id)

    def heartbeat(self, client_id: str) -> None:
        self._touch(client_id)

    def disconnect(self, client_id: str) -> None:
        with self._lock:
            self._clients.pop(client_id, None)
            self._schedule_locked(
                self.reconnect_grace_seconds if not self._clients else self.heartbeat_timeout_seconds
            )

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._timer:
                self._timer.cancel()
                self._timer = None

    def _touch(self, client_id: str) -> None:
        with self._lock:
            if self._closed:
                return
            self._seen_client = True
            self._clients[client_id] = time.monotonic()
            self._schedule_locked(self.heartbeat_timeout_seconds)

    def _schedule_locked(self, delay: float) -> None:
        if self._closed:
            return
        if self._timer:
            self._timer.cancel()
        self._timer = threading.Timer(delay, self._expire_clients)
        self._timer.daemon = True
        self._timer.start()

    def _expire_clients(self) -> None:
        with self._lock:
            if self._closed:
                return
            now = time.monotonic()
            self._clients = {
                client_id: seen_at
                for client_id, seen_at in self._clients.items()
                if now - seen_at < self.heartbeat_timeout_seconds
            }
            if self._clients:
                self._schedule_locked(self.heartbeat_timeout_seconds)


class Handler(BaseHTTPRequestHandler):
    def send_json(self, payload: Any, status: int = 200) -> None:
        log = get_logger("http")
        try:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8", errors="replace")
        except (TypeError, ValueError, UnicodeError) as error:
            log.warning("[HTTP] sendJson encode failed err=%s", error)
            body = json.dumps({"error": "响应编码失败"}, ensure_ascii=False).encode("utf-8")
            status = 500
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(body)
            log.info("[HTTP] sendJson ok status=%s bytes=%s", status, len(body))
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError) as error:
            log.warning("[HTTP] sendJson write failed err=%s bytes=%s", error, len(body))

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
        log = get_logger("http")
        if path == "/" or path == "/index.html":
            log.info("[HTTP] serveIndex client=%s webRoot=%s", self.client_address[0], WEB_ROOT)
            return self.serve_file(WEB_ROOT / "index.html", "text/html; charset=utf-8")
        if path in ("/styles.css", "/app.js"):
            file_path = WEB_ROOT / path.removeprefix("/")
            content_type = "text/javascript; charset=utf-8" if file_path.suffix == ".js" else "text/css; charset=utf-8"
            return self.serve_file(file_path, content_type)
        if path == "/api/v1/health" or path == "/api/v1/agent":
            from .diagnose import diagnose

            payload = {
                "status": "connected",
                "version": __version__,
                "dataDir": str(data_root()),
                "listen": "127.0.0.1",
            }
            payload.update(diagnose())
            log.info(
                "[Agent] health frozen=%s adbExists=%s appleUsbmux=%s wintun=%s layoutOk=%s",
                payload.get("frozen"),
                payload.get("adbExists"),
                payload.get("appleUsbmux"),
                payload.get("wintun"),
                payload.get("layoutOk"),
            )
            return self.send_json(payload)
        if path == "/api/v1/devices":
            query = parse_qs(urlparse(self.path).query)
            connected = registry.refresh(timeout=8) if query.get("fresh") else registry.snapshot()
            return self.send_json({"devices": connected, "agent": {"status": "connected", "version": __version__}})
        if path == "/api/v1/devices/stream":
            return self.stream_devices()
        match = re.fullmatch(r"/api/v1/devices/([^/]+)/foreground", path)
        if match:
            device = next((item for item in registry.snapshot() if item["id"] == match.group(1)), None)
            try:
                payload = foreground_info(device or {"id": match.group(1), "platform": "android"})
            except Exception as error:
                log.exception("[Foreground] query failed device=%s err=%s", match.group(1), error)
                payload = {"supported": True, "package": None, "label": None, "error": "读取前台应用失败"}
            return self.send_json(payload)
        if path == "/api/v1/reports" or path == "/api/v1/runs":
            return self.send_json({"reports": manager.catalog(), "runs": manager.catalog()})
        if path == "/api/v1/runs/active":
            return self.send_json({"runs": manager.active()})
        match = re.fullmatch(r"/api/v1/devices/([^/]+)/applications", path)
        if match:
            query = parse_qs(urlparse(self.path).query)
            device = next((item for item in registry.snapshot() if item["id"] == match.group(1)), None)
            hinted = (query.get("platform") or [""])[0]
            platform = hinted or (device["platform"] if device else "") or "android"
            log = get_logger("http")
            tag = "[IosApps]" if platform == "ios" else "[AndroidApps]"
            log.info(
                "%s http start device=%s platform=%s snapshot=%s path=%s",
                tag,
                match.group(1),
                platform,
                bool(device),
                self.path,
            )
            try:
                payload = applications(match.group(1), platform)
            except Exception as error:
                log.exception("%s http failed device=%s platform=%s err=%s", tag, match.group(1), platform, error)
                payload = {"applications": [], "error": f"读取应用列表失败：{error}"}
            log.info(
                "%s http done device=%s count=%s error=%s",
                tag,
                match.group(1),
                len(payload.get("applications") or []),
                payload.get("error"),
            )
            try:
                return self.send_json(payload)
            except Exception:
                log.exception("%s http send failed device=%s", tag, match.group(1))
                return None
        match = re.fullmatch(r"/api/v1/(?:runs|sessions)/([^/]+)/stream", path)
        if match:
            return self.stream(match.group(1))
        match = re.fullmatch(r"/api/v1/(?:runs|sessions)/([^/]+)$", path)
        if match:
            session = manager.get(match.group(1))
            if not session:
                meta = RunStore(match.group(1)).read_meta()
                if not meta:
                    return self.send_json({"error": "Run not found"}, 404)
                return self.send_json(
                    {
                        "runId": meta.get("runId"),
                        "sessionId": meta.get("runId"),
                        "status": meta.get("status"),
                        "reportReady": meta.get("reportReady", False),
                        "error": meta.get("error"),
                        "summary": meta.get("summary"),
                    }
                )
            return self.send_json(manager.public_run(session, include_samples=True))
        match = re.fullmatch(r"/api/v1/(?:runs|sessions)/([^/]+)/report", path)
        if match:
            run_id = match.group(1)
            session = manager.get(run_id)
            store = session["store"] if session else RunStore(run_id)
            meta = (session if session else None) or store.read_meta()
            status = (meta or {}).get("status") if isinstance(meta, dict) else None
            if session:
                status = session.get("status") or status
            if status == "discarded":
                return self.send_json({"error": "Run discarded"}, 404)
            if status not in ("running", "ready", "finalizing"):
                samples = store.read_samples()
                payload = store.read_meta() or {}
                if session:
                    payload.setdefault("appName", session.get("appName"))
                    payload.setdefault("appVersion", session.get("appVersion"))
                    payload.setdefault("versionName", session.get("versionName"))
                    payload.setdefault("versionCode", session.get("versionCode"))
                    payload.setdefault("capabilities", session.get("capabilities") or [])
                    payload.setdefault("extras", session.get("extras") or {})
                    payload.setdefault("summary", session.get("summary"))
                if samples or payload:
                    log = get_logger("http")
                    want_report = payload.get("wantReport")
                    if session and session.get("wantReport") is False:
                        want_report = False
                    if want_report is False:
                        log.info("[StopMonitor] report skip wantReport=false runId=%s", run_id)
                    else:
                        try:
                            html = render_report(payload, samples)
                            store.write_report(html)
                            log.info("[Report] refresh runId=%s samples=%s status=%s", run_id, len(samples), status)
                        except Exception:
                            log.exception("[Report] refresh failed runId=%s", run_id)
            report = store.report_path
            if not report.is_file():
                return self.send_json(
                    {
                        "error": "Report is still being generated" if session else "Run not found",
                        "status": session["status"] if session else "unknown",
                    },
                    409 if session else 404,
                )
            named = store.read_meta() or {}
            if session:
                named.setdefault("appName", session.get("appName"))
                named.setdefault("appVersion", session.get("appVersion"))
                named.setdefault("versionName", session.get("versionName"))
                named.setdefault("versionCode", session.get("versionCode"))
            return self.serve_file(report, "text/html; charset=utf-8", download_name=report_filename(named))
        return self.send_json({"error": "Not found"}, 404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            data = self.read_json()
            if path in ("/api/v1/ui/connect", "/api/v1/ui/heartbeat", "/api/v1/ui/disconnect"):
                client_id = str(data.get("clientId") or "").strip()
                if not client_id:
                    return self.send_json({"error": "clientId is required"}, 400)
                lifecycle = self.server.ui_lifecycle
                if path == "/api/v1/ui/connect":
                    lifecycle.connect(client_id)
                elif path == "/api/v1/ui/heartbeat":
                    lifecycle.heartbeat(client_id)
                else:
                    lifecycle.disconnect(client_id)
                return self.send_json({"status": "ok"})
            if path == "/api/v1/capabilities":
                device_id = data.get("deviceId")
                device = next((item for item in registry.snapshot() if item["id"] == device_id), {})
                log = get_logger("http")
                log.info(
                    "[StartMonitor] capabilities start device=%s bundle=%s snapshot=%s",
                    device_id,
                    data.get("bundle"),
                    bool(device),
                )
                payload = capabilities(device, data.get("bundle", ""))
                log.info("[StartMonitor] capabilities done device=%s ready=%s", device_id, payload.get("ready"))
                return self.send_json(payload)
            match = re.fullmatch(r"/api/v1/devices/([^/]+)/capabilities", path)
            if match:
                device = next((item for item in registry.snapshot() if item["id"] == match.group(1)), {})
                return self.send_json(capabilities(device, data.get("bundle", "")))
            if path in ("/api/v1/runs", "/api/v1/sessions"):
                device = data.get("device") or {}
                if data.get("deviceId") and not device:
                    device = next((item for item in registry.snapshot() if item["id"] == data["deviceId"]), {})
                bundle = data.get("bundle") or data.get("packageId")
                if not device or not bundle:
                    return self.send_json({"error": "device 和 bundle/packageId 必填"}, 400)
                session = manager.create(
                    device,
                    bundle,
                    source=data.get("source", "manual"),
                    app=data.get("app") if isinstance(data.get("app"), dict) else {},
                    capabilities=data.get("capabilities") if isinstance(data.get("capabilities"), list) else [],
                )
                return self.send_json(
                    {
                        "runId": session["runId"],
                        "sessionId": session["runId"],
                        "status": "ready",
                        "appName": session.get("appName"),
                        "appVersion": session.get("appVersion"),
                        "versionName": session.get("versionName"),
                        "versionCode": session.get("versionCode"),
                    },
                    201,
                )
            match = re.fullmatch(r"/api/v1/(?:runs|sessions)/([^/]+)/start", path)
            if match:
                session = manager.start(match.group(1))
                return self.send_json({"runId": session["runId"], "sessionId": session["runId"], "status": "running"})
            match = re.fullmatch(r"/api/v1/(?:runs|sessions)/([^/]+)/stop", path)
            if match:
                body = data if isinstance(data, dict) else {}
                save = False if body.get("save") is False else True
                report = False if body.get("report") is False else True
                manager.stop(match.group(1), save=save, report=report)
                log = get_logger("http")
                log.info(
                    "[StopMonitor] http stop runId=%s save=%s report=%s",
                    match.group(1),
                    save,
                    report,
                )
                return self.send_json({"status": "finalizing", "save": save, "report": report})
            match = re.fullmatch(r"/api/v1/(?:runs|sessions)/([^/]+)/extras", path)
            if match:
                extras = manager.set_extras(match.group(1), data or {})
                log = get_logger("http")
                log.info(
                    "[AndroidSample] extras http runId=%s nativePss=%s swapPss=%s logcat=%s",
                    match.group(1),
                    extras.get("nativePss"),
                    extras.get("swapPss"),
                    extras.get("logcat"),
                )
                return self.send_json({"runId": match.group(1), "extras": extras})
            match = re.fullmatch(r"/api/v1/(?:runs|sessions)/([^/]+)/marker", path)
            if match:
                marker = manager.set_marker(match.group(1), data or {})
                log = get_logger("http")
                log.info(
                    "[SceneMarker] http runId=%s scene=%s scriptFn=%s",
                    match.group(1),
                    marker.get("scene"),
                    marker.get("scriptFn"),
                )
                return self.send_json({"runId": match.group(1), "marker": marker})
        except DeviceBusyError as error:
            occupied = manager.get(error.run_id) or {}
            return self.send_json(
                {
                    "error": str(error),
                    "runId": error.run_id,
                    "status": occupied.get("status"),
                    "bundle": occupied.get("bundle") or occupied.get("packageId"),
                    "packageId": occupied.get("packageId") or occupied.get("bundle"),
                },
                409,
            )
        except NotForegroundError as error:
            return self.send_json(
                {
                    "error": str(error),
                    "code": "not_foreground",
                    "package": error.bundle,
                    "foreground": error.foreground,
                },
                409,
            )
        except (KeyError, ValueError, RuntimeError, OSError) as error:
            return self.send_json({"error": str(error)}, 400)
        return self.send_json({"error": "Not found"}, 404)

    def stream_devices(self) -> None:
        queue = registry.subscribe()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            while True:
                try:
                    event = queue.get(timeout=15)
                    self.wfile.write(f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode())
                    self.wfile.flush()
                except Empty:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        finally:
            registry.unsubscribe(queue)

    def stream(self, run_id: str) -> None:
        session = manager.get(run_id)
        if not session:
            return self.send_json({"error": "Run not found"}, 404)
        token = manager.attach_viewer(run_id)
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
        finally:
            manager.detach_viewer(run_id, token)

    def serve_file(self, path: Path, content_type: str, download_name: str | None = None) -> None:
        log = get_logger("http")
        if not path.is_file():
            log.warning("[HTTP] serveFile miss path=%s exists=False", path)
            return self.send_json({"error": "Not found", "path": str(path)}, 404)
        body = path.read_bytes()
        log.info("[HTTP] serveFile ok path=%s bytes=%s type=%s download=%s", path.name, len(body), content_type, download_name)
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        if download_name:
            from urllib.parse import quote

            ascii_name = re.sub(r"[^\w.\-]+", "-", download_name).strip("-") or "report.html"
            self.send_header(
                "Content-Disposition",
                f"inline; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(download_name)}",
            )
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


class AgentHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.ui_lifecycle = UiLifecycle()

    def server_bind(self) -> None:
        if os.name == "nt":
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            except OSError:
                pass
        super().server_bind()

    def server_close(self) -> None:
        self.ui_lifecycle.close()
        super().server_close()


def serve(
    host: str = "127.0.0.1",
    port: int = 8765,
    on_ready: Any = None,
    on_already_running: Any = None,
    on_shutdown: Any = None,
) -> None:
    log = get_logger("http")
    index = WEB_ROOT / "index.html"
    log.info(
        "[Agent] listen start pid=%s host=%s port=%s webRoot=%s indexExists=%s",
        os.getpid(),
        host,
        port,
        WEB_ROOT,
        index.is_file(),
    )
    registry.start()
    try:
        server = AgentHTTPServer((host, port), Handler)
    except OSError as error:
        # Another launch may have passed the pre-flight health check before
        # the first process finished binding. Let the caller reopen a verified
        # existing Agent rather than treating this normal double-click race as
        # a fatal port conflict.
        if on_already_running:
            try:
                if on_already_running():
                    return
            except Exception:
                log.exception("[Agent] existing-instance callback failed host=%s port=%s", host, port)
        log.error("[Agent] listen failed pid=%s host=%s port=%s err=%s", os.getpid(), host, port, error)
        print(f"端口 {port} 已被占用，多半是旧的 PerfPilot Agent 没关掉。请先结束占用该端口的 python 进程，再重新启动。")
        sys.stdout.flush()
        raise SystemExit(1) from error
    print(f"PerfPilot Agent: http://{host}:{port}")
    sys.stdout.flush()
    if on_ready:
        threading.Thread(target=on_ready, name="open-ui", daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nPerfPilot Agent 正在停止…")
    finally:
        try:
            # serve_forever() has already returned here. Close the listening
            # socket before collector cleanup so a stuck child cannot keep
            # the Agent port occupied.
            server.server_close()
        finally:
            try:
                manager.shutdown()
            finally:
                if on_shutdown:
                    try:
                        on_shutdown()
                    except Exception:
                        log.exception("[Agent] shutdown callback failed")


def main() -> None:
    port = int(os.environ.get("PERF_WEB_PORT", "8765"))
    serve("127.0.0.1", port)


if __name__ == "__main__":
    main()
