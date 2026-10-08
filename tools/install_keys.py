"""Installs the server's SSH public key on every reachable router (append-only).

Run on the server:  set -a; . ./.env; set +a; venv/bin/python -m tools.install_keys
After that the collector logs in with the key; passwords stay as a fallback.
"""
import asyncio
import os
import subprocess

import asyncssh

from app import collector, config

CMD = (
    'mkdir -p /etc/dropbear && touch /etc/dropbear/authorized_keys && chmod 600 /etc/dropbear/authorized_keys && '
    '(grep -qF "{key}" /etc/dropbear/authorized_keys && echo present || '
    '(echo "{key}" >> /etc/dropbear/authorized_keys && echo added))'
)


async def install(dev, key, sem):
    if not dev["online"]:
        return dev["name"], "offline"
    async with sem:
        for password in config.SSH_PASSWORDS:
            try:
                async with asyncssh.connect(dev["ip"], username=config.SSH_USER, password=password, client_keys=None,
                                            known_hosts=None, agent_path=None, connect_timeout=15) as conn:
                    r = await conn.run(CMD.format(key=key), timeout=20, check=False)
                    return dev["name"], str(r.stdout).strip() or f"exit {r.exit_status}"
            except asyncssh.PermissionDenied:
                continue
            except (OSError, asyncssh.Error, asyncio.TimeoutError, TimeoutError) as e:
                return dev["name"], f"error: {type(e).__name__}"
    return dev["name"], "wrong password"


async def main():
    if not os.path.exists(config.SSH_KEY):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "routers-dashboard", "-f", config.SSH_KEY], check=True)
    key = open(config.SSH_KEY + ".pub").read().strip()
    sem = asyncio.Semaphore(8)
    for name, result in await asyncio.gather(*(install(d, key, sem) for d in await collector.tailscale_peers())):
        print(f"{name:24} {result}")


asyncio.run(main())
