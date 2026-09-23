"""Start the local Agent and open the browser."""

from __future__ import annotations

import atexit
import json
import os
import runpy
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser

from perfpilot.diagnose import diagnose, layout_ok
from perfpilot.logutil import get_logger, setup_logging
from perfpilot.runtime import ensure_adb_server, prepare_environment, stop_adb_server


def _run_collect(kind: str, rest: list[str]) -> None:
    os.environ["PERFPILOT_COLLECT"] = "1"
    sys.argv = [sys.argv[0], *rest]
    if kind == "android":
        import adb_fps

        raise SystemExit(adb_fps.main())
    if kind == "ios":
        import ios_perf

        raise SystemExit(ios_perf.main())
    if kind == "pymobiledevice3":
        sys.argv = ["pymobiledevice3", *rest]
        runpy.run_module("pymobiledevice3", run_name="__main__")
        return
    raise SystemExit(f"未知采集器：{kind}")


def _open_browser(url: str) -> None:
    log = get_logger("main")
    time.sleep(0.3)
    log.info("[UI] openBrowser start url=%s frozen=%s", url, getattr(sys, "frozen", False))
    if os.name == "nt":
        try:
            os.startfile(url)  # type: ignore[attr-defined]
            log.info("[UI] openBrowser success method=startfile url=%s", url)
            return
        except OSError as error:
            log.warning("[UI] openBrowser startfile failed url=%s err=%s", url, error)
        try:
            subprocess.Popen(
                ["cmd", "/c", "start", "", url],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            log.info("[UI] openBrowser success method=cmd-start url=%s", url)
            return
        except OSError as error:
            log.warning("[UI] openBrowser cmd-start failed url=%s err=%s", url, error)
    opened = webbrowser.open(url, new=2)
    log.info("[UI] openBrowser webbrowser.open returned=%s url=%s", opened, url)


def _existing_agent(url: str) -> bool:
    """Return whether ``url`` belongs to an already-running PerfPilot Agent."""
    try:
        with urllib.request.urlopen(f"{url}/api/v1/health", timeout=3) as response:
            if response.status != 200:
                return False
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return False
    return (
        isinstance(payload, dict)
        and payload.get("status") == "connected"
        and payload.get("listen") == "127.0.0.1"
    )


def main() -> None:
    prepare_environment()
    if len(sys.argv) == 2 and sys.argv[1] == "--doctor":
        setup_logging()
        print(json.dumps(diagnose(), ensure_ascii=False, indent=2))
        return
    if len(sys.argv) >= 3 and sys.argv[1] == "--collect":
        _run_collect(sys.argv[2], sys.argv[3:])
        return
    setup_logging()
    log = get_logger("main")
    ok, layout = layout_ok()
    log.info(
        "[Agent] start frozen=%s pid=%s bits=%s argv=%s layout=%s pathChars=%s",
        getattr(sys, "frozen", False),
        os.getpid(),
        64 if sys.maxsize > 2**32 else 32,
        sys.argv,
        layout,
        len(os.environ.get("PATH", "")),
    )
    if not ok:
        print(layout)
        sys.stdout.flush()
        raise SystemExit(2)
    info = diagnose()
    log.info(
        "[Agent] diagnose adbExists=%s appleUsbmux=%s wintun=%s adb=%s",
        info.get("adbExists"),
        info.get("appleUsbmux"),
        info.get("wintun"),
        info.get("adb"),
    )
    for hint in info.get("hints") or []:
        print(f"注意：{hint}")
    sys.stdout.flush()
    port = int(os.environ.get("PERF_WEB_PORT", "8765"))
    url = f"http://127.0.0.1:{port}"

    # Closing the browser does not stop the local Agent. Re-launching the EXE
    # should therefore focus the existing local page instead of failing while
    # trying to bind the same port a second time.
    if _existing_agent(url):
        log.info("[Agent] existing instance found url=%s; reopening browser", url)
        _open_browser(url)
        return

    ensure_adb_server()
    from perfpilot.server import serve
    from perfpilot.session import manager

    atexit.register(manager.shutdown)

    adb_stopped = False

    def stop_adb_once() -> None:
        nonlocal adb_stopped
        if adb_stopped:
            return
        adb_stopped = True
        stop_adb_server()

    atexit.register(stop_adb_once)

    def on_ready() -> None:
        log.info("[Agent] listen ready url=%s", url)
        print(f"请用系统浏览器打开 {url}  （不要用 https，也不要用 localhost）")
        sys.stdout.flush()
        _open_browser(url)

    def on_already_running() -> bool:
        if not _existing_agent(url):
            return False
        log.info("[Agent] instance won bind race url=%s; reopening browser", url)
        _open_browser(url)
        return True

    serve(
        "127.0.0.1",
        port,
        on_ready=on_ready,
        on_already_running=on_already_running,
        on_shutdown=stop_adb_once,
    )


if __name__ == "__main__":
    main()
