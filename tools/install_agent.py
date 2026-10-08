"""Installs (or updates) the monitoring agent on every router reachable over SSH.

Run on the server:  set -a; . ./.env; set +a; venv/bin/python -m tools.install_agent [name ...]
Routers the server cannot reach: run the command from the dashboard's router card on the router itself.
"""
import asyncio
import os
import sys

import asyncssh

from app import collector, config


async def install(dev, sem):
    if not dev["online"]:
        return dev["name"], "offline"
    key = [config.SSH_KEY] if os.path.exists(config.SSH_KEY) else None
    async with sem:
        for password in config.SSH_PASSWORDS or [None]:
            try:
                async with asyncssh.connect(dev["ip"], username=config.SSH_USER, password=password, client_keys=key,
                                            known_hosts=None, agent_path=None, connect_timeout=15) as conn:
                    r = await conn.run(collector.install_command(dev["id"]), timeout=90, check=False)
                    out = (str(r.stdout).strip() or str(r.stderr).strip()).splitlines()
                    return dev["name"], out[-1] if out else f"exit {r.exit_status}"
            except asyncssh.PermissionDenied:
                continue
            except (OSError, asyncssh.Error, asyncio.TimeoutError, TimeoutError) as e:
                return dev["name"], f"error: {type(e).__name__}"
    return dev["name"], "wrong password"


async def main():
    only = set(sys.argv[1:])
    peers = [p for p in await collector.tailscale_peers() if not only or p["name"] in only]
    sem = asyncio.Semaphore(6)
    for name, result in await asyncio.gather(*(install(p, sem) for p in peers)):
        print(f"{name:24} {result}")


asyncio.run(main())
