"""Windows compatibility patches for pymobiledevice3 (IPv6 scope, pyOpenSSL)."""

from __future__ import annotations

import re
import socket
import sys

_SCOPE_NAME = re.compile(r"%(?:ethernet|wlan|wifi)_(\d+)$", re.IGNORECASE)
_APPLIED = False


def apply() -> None:
    global _APPLIED
    if _APPLIED:
        return
    _patch_getaddrinfo()
    _patch_x509_request_version()
    _patch_usbmux_pair_record_exists()
    _APPLIED = True


def _patch_getaddrinfo() -> None:
    original = socket.getaddrinfo

    def getaddrinfo(host, *args, **kwargs):
        if isinstance(host, str):
            match = _SCOPE_NAME.search(host)
            if match:
                interface_name = host[match.start() + 1 :]
                try:
                    interface_index = socket.if_nametoindex(interface_name)
                except OSError:
                    interface_index = int(match.group(1))
                host = host[: match.start()] + f"%{interface_index}"
        return original(host, *args, **kwargs)

    socket.getaddrinfo = getaddrinfo  # type: ignore[assignment]


def _patch_x509_request_version() -> None:
    try:
        from OpenSSL import crypto

        original = crypto.X509Req.set_version

        def set_x509_request_version(request, version):
            return original(request, 0 if version == 2 else version)

        crypto.X509Req.set_version = set_x509_request_version
    except Exception:
        pass


def _patch_usbmux_pair_record_exists() -> None:
    if sys.platform != "win32":
        return
    try:
        from pymobiledevice3.exceptions import MuxException
        from pymobiledevice3.lockdown import PlistUsbmuxLockdownClient

        original = PlistUsbmuxLockdownClient.save_pair_record

        async def save_pair_record(client):
            try:
                await original(client)
            except MuxException as error:
                if not re.search(r"['\"]Number['\"]:\s*183\b", str(error)):
                    raise

        PlistUsbmuxLockdownClient.save_pair_record = save_pair_record
    except Exception:
        pass
