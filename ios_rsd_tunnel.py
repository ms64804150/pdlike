#!/usr/bin/env python3
"""Create an iOS 17 developer tunnel from a known USB RSD endpoint."""

import argparse
import asyncio
import ctypes
import json
from pathlib import Path

from pymobiledevice3.remote.common import TunnelProtocol
from pymobiledevice3.remote.module_imports import start_tunnel
from pymobiledevice3.remote.remote_service_discovery import (
    RemoteServiceDiscoveryService,
)
from pymobiledevice3.remote.tunnel_service import (
    create_core_device_tunnel_service_using_rsd,
)


def is_administrator() -> bool:
    return bool(ctypes.windll.shell32.IsUserAnAdmin())


async def run(host: str, port: int) -> None:
    connection_file = Path(__file__).with_name(".ios-rsd.json")
    rsd = RemoteServiceDiscoveryService((host, port))
    await rsd.connect()
    service = await create_core_device_tunnel_service_using_rsd(rsd)
    try:
        async with start_tunnel(service, protocol=TunnelProtocol.TCP) as tunnel:
            connection_file.write_text(
                json.dumps({"host": tunnel.address, "port": tunnel.port}),
                encoding="utf-8",
            )
            print("Tunnel created. Keep this window open.", flush=True)
            print(f"--rsd-host {tunnel.address} --rsd-port {tunnel.port}", flush=True)
            await asyncio.Event().wait()
    finally:
        connection_file.unlink(missing_ok=True)
        await service.close()
        await rsd.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Start an iOS developer tunnel without Bonjour discovery"
    )
    parser.add_argument("host", help="Pre-tunnel RSD IPv6 address, including scope ID")
    parser.add_argument("--port", type=int, default=58783)
    args = parser.parse_args()
    if not is_administrator():
        parser.error("this command must run in an Administrator PowerShell")
    asyncio.run(run(args.host, args.port))


if __name__ == "__main__":
    main()