"""Stream Android logcat filtered to the monitored package."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from typing import Callable, Optional

from .collectors import decode_collector_line
from .logutil import get_logger
from .runtime import adb_cwd, adb_env, adb_executable, subprocess_kwargs

log = get_logger("logcat")

THREADTIME_PID = re.compile(
    r"^\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:\.\d+)?\s+(\d+)\s+\d+\s+[VDIWEF]\s+"
)

_MODES = ("pid+recent", "pid", "grep+recent", "grep")


def _adb(serial: str, args: list[str], timeout: int = 8) -> subprocess.CompletedProcess[str]:
    command = [adb_executable()]
    if serial:
        command.extend(["-s", serial])
    command.extend(args)
    return subprocess.run(
        command,
        cwd=adb_cwd() or None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=adb_env(),
        **subprocess_kwargs(),
    )


def _adb_shell(serial: str, *remote: str, timeout: int = 5) -> str:
    try:
        result = _adb(serial, ["shell", *remote], timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        log.warning("[Logcat] shell failed serial=%s cmd=%s err=%s", serial, remote[:3], error)
        return ""
    return result.stdout or ""


def _pids_from_ps(text: str, package: str) -> list[int]:
    found: list[int] = []
    prefix = f"{package}:"
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.lower().startswith("user"):
            continue
        parts = stripped.split()
        if len(parts) < 2:
            continue
        name = parts[-1]
        if name != package and not name.startswith(prefix):
            continue
        for token in parts[1:4]:
            try:
                pid = int(token)
            except ValueError:
                continue
            if pid > 0:
                found.append(pid)
                break
    return found


def resolve_package_pids(serial: str, package: str) -> list[int]:
    candidates: list[int] = []
    pidof_out = _adb_shell(serial, "pidof", package)
    for token in pidof_out.split():
        try:
            candidates.append(int(token))
        except ValueError:
            continue
    if not candidates:
        ps_text = _adb_shell(serial, "ps", "-A") or _adb_shell(serial, "ps")
        candidates.extend(_pids_from_ps(ps_text, package))
    matched: list[int] = []
    seen: set[int] = set()
    for pid in candidates:
        if pid in seen:
            continue
        cmdline = _adb_shell(serial, "cat", f"/proc/{pid}/cmdline").replace("\0", " ").strip()
        first = cmdline.split()[0] if cmdline else ""
        if first == package or first.startswith(f"{package}:"):
            seen.add(pid)
            matched.append(pid)
            continue
        if not first:
            seen.add(pid)
            matched.append(pid)
    result = matched or list(dict.fromkeys(candidates))
    return result


def _line_pid(line: str) -> Optional[int]:
    match = THREADTIME_PID.match(line)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def keep_line(line: str, pids: set[int], package: str) -> bool:
    if package and package in line:
        return True
    pid = _line_pid(line)
    return pid is not None and pid in pids


def _logcat_args(pids: list[int], mode: str) -> list[str]:
    args = ["logcat", "-v", "threadtime"]
    if "recent" in mode:
        args.extend(["-T", "1"])
    if mode.startswith("pid"):
        for pid in pids:
            args.append(f"--pid={pid}")
    return args


def _option_rejected(line: str) -> bool:
    lower = line.lower()
    return (
        "unknown option" in lower
        or "unrecognized" in lower
        or "not a valid" in lower
        or "unrecognized flag" in lower
    )


def _terminate_process(proc: Optional[subprocess.Popen[bytes]]) -> None:
    if proc is None or proc.poll() is not None:
        return
    pid = proc.pid
    try:
        proc.terminate()
    except OSError:
        pass
    try:
        proc.wait(timeout=1.2)
        return
    except Exception:
        pass
    if os.name == "nt" and pid:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=8,
            )
        except (OSError, subprocess.SubprocessError):
            pass
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


def _spawn_logcat(serial: str, args: list[str]) -> subprocess.Popen[bytes]:
    command = [adb_executable()]
    if serial:
        command.extend(["-s", serial])
    command.extend(args)
    options: dict[str, object] = {}
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP
        options["creationflags"] = flags | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    log.info("[Logcat] spawn serial=%s cmd=%s", serial, " ".join(command))
    return subprocess.Popen(
        command,
        cwd=adb_cwd() or None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
        env=adb_env(),
        **options,
    )


def run_package_logcat(
    serial: str,
    package: str,
    should_stop: Callable[[], bool],
    on_line: Callable[[str], None],
    on_process: Optional[Callable[[Optional[subprocess.Popen[bytes]]], None]] = None,
    run_id: str = "",
) -> None:
    if not serial or not package:
        log.warning("[Logcat] skip missing serial=%s package=%s runId=%s", serial, package, run_id)
        return
    mode = _MODES[0]
    waiting_logged = False
    gone_logged = False
    total = 0
    log.info("[Logcat] loop start runId=%s serial=%s package=%s mode=%s", run_id, serial, package, mode)
    while not should_stop():
        use_pid = mode.startswith("pid")
        pids = resolve_package_pids(serial, package)
        if use_pid and not pids:
            if not waiting_logged:
                waiting_logged = True
                log.info("[Logcat] wait pid runId=%s package=%s serial=%s", run_id, package, serial)
                try:
                    on_line(f"等待应用进程 package={package}")
                except Exception:
                    log.exception("[Logcat] wait notice failed runId=%s", run_id)
            time.sleep(1.0)
            continue
        waiting_logged = False
        args = _logcat_args(pids, mode)
        proc: Optional[subprocess.Popen[bytes]] = None
        line_count = 0
        rejected = False
        last_pid_check = time.monotonic()
        try:
            proc = _spawn_logcat(serial, args)
            if on_process:
                try:
                    on_process(proc)
                except Exception:
                    log.exception("[Logcat] on_process failed runId=%s pid=%s", run_id, proc.pid if proc else None)
            assert proc.stdout is not None
            for raw in proc.stdout:
                if should_stop():
                    break
                text = decode_collector_line(raw)
                if not text:
                    continue
                if _option_rejected(text):
                    rejected = True
                    log.warning(
                        "[Logcat] option rejected runId=%s mode=%s line=%s",
                        run_id,
                        mode,
                        text[:200],
                    )
                    break
                if not use_pid and not keep_line(text, set(pids), package):
                    now = time.monotonic()
                    if now - last_pid_check >= 2.0:
                        last_pid_check = now
                        pids = resolve_package_pids(serial, package) or pids
                    continue
                gone_logged = False
                line_count += 1
                total += 1
                try:
                    on_line(text)
                except Exception:
                    log.exception("[Logcat] on_line failed runId=%s n=%s", run_id, total)
                if total <= 3 or total % 200 == 0:
                    log.info(
                        "[Logcat] recv runId=%s n=%s mode=%s package=%s pids=%s preview=%s",
                        run_id,
                        total,
                        mode,
                        package,
                        pids,
                        text[:80],
                    )
                now = time.monotonic()
                if now - last_pid_check < 2.0:
                    continue
                last_pid_check = now
                latest = resolve_package_pids(serial, package)
                if use_pid and not latest:
                    if not gone_logged:
                        gone_logged = True
                        log.info("[Logcat] process gone runId=%s package=%s oldPids=%s", run_id, package, pids)
                        on_line(f"应用进程已退出 package={package}，等待重新启动")
                    break
                if use_pid and latest and set(latest) != set(pids):
                    log.info(
                        "[Logcat] pid change runId=%s package=%s old=%s new=%s",
                        run_id,
                        package,
                        pids,
                        latest,
                    )
                    break
                if latest:
                    pids = latest
        except (OSError, subprocess.SubprocessError):
            log.exception("[Logcat] spawn/read failed runId=%s mode=%s package=%s", run_id, mode, package)
            on_line(f"logcat 启动失败 package={package} mode={mode}")
        finally:
            _terminate_process(proc)
            if on_process:
                on_process(None)
        if should_stop():
            break
        if rejected or (line_count == 0 and proc is not None and proc.returncode not in (0, None)):
            idx = _MODES.index(mode) if mode in _MODES else 0
            if idx + 1 < len(_MODES):
                mode = _MODES[idx + 1]
                log.info("[Logcat] fallback runId=%s package=%s mode=%s", run_id, package, mode)
                continue
        time.sleep(0.4)
    log.info("[Logcat] loop end runId=%s package=%s total=%s mode=%s", run_id, package, total, mode)
