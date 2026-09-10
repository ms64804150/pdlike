"""Windows compatibility for IPv6 link-local scopes emitted by pymobiledevice3."""

import re
import socket
import os


_original_getaddrinfo = socket.getaddrinfo
_SCOPE_NAME = re.compile(r"%(?:ethernet|wlan|wifi)_(\d+)$", re.IGNORECASE)


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
    return _original_getaddrinfo(host, *args, **kwargs)


socket.getaddrinfo = getaddrinfo


try:
    from OpenSSL import crypto

    _original_set_version = crypto.X509Req.set_version

    def set_x509_request_version(request, version):
        return _original_set_version(request, 0 if version == 2 else version)

    crypto.X509Req.set_version = set_x509_request_version
except (ImportError, AttributeError):
    pass
