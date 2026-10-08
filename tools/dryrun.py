"""One poll cycle against the real routers, printed as a table. Sends nothing.

Run on the server:  DATA_DIR=/tmp/monit-dry venv/bin/python -m tools.dryrun
"""
import asyncio

from app import alerts, collector, db

alerts.queue = lambda downs, ups: None


async def main():
    await collector.cycle()
    mark = {"ok": "+", "fail": "X", "na": "-", "unknown": "?"}
    print(f"{'router':24}" + "".join(f"{c[:8]:>9}" for c in collector.CHECKS))
    for d in db.q("SELECT * FROM devices ORDER BY name"):
        checks = {c["name"]: c for c in db.q("SELECT * FROM checks WHERE device_id = ?", (d["id"],))}
        print(f"{d['name']:24}" + "".join(f"{mark[checks[c]['status']]:>9}" for c in collector.CHECKS))
        for c in collector.CHECKS:
            if checks[c]["status"] in ("fail", "unknown") and checks[c]["detail"] != "нет данных":
                print(f"    {c}: {checks[c]['detail']}")


asyncio.run(main())
