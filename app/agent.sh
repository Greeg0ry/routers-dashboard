#!/bin/sh
# rmon-agent: reports this router's state to the monitoring dashboard.
# Runs the read-only probe and POSTs its output; started by cron every 2 minutes.
. /etc/rmon-agent.conf || exit 1
PROBE=/usr/lib/rmon/probe.sh
# spread routers across the minute so they do not all report at once
[ "$1" = now ] || sleep $(($$ % 30))

SHA=$(sha256sum "$PROBE" | cut -c1-16)
HDR=/tmp/.rmon-hdr.$$
RMON_NO_CLASH=1 sh "$PROBE" 2>/dev/null </dev/null | curl -s -m 25 -o /dev/null -D "$HDR" -X POST \
	-H "X-Device: $DEVICE" -H "X-Token: $TOKEN" -H "X-Probe-Sha: $SHA" -H 'Content-Type: text/plain' \
	--data-binary @- "$URL/api/ingest"
rc=$?

# The server names the probe version it expects; fetch it when ours differs.
WANT=$(tr -d '\r' <"$HDR" 2>/dev/null | awk 'tolower($1)=="x-probe-sha:"{print $2}')
rm -f "$HDR"
if [ -n "$WANT" ] && [ "$WANT" != "$SHA" ]; then
	NEW=/tmp/.rmon-probe.$$
	if curl -fsS -m 25 -o "$NEW" -H "X-Device: $DEVICE" -H "X-Token: $TOKEN" "$URL/api/agent/probe" &&
		[ "$(sha256sum "$NEW" | cut -c1-16)" = "$WANT" ] && sh -n "$NEW"; then
		cat "$NEW" >"$PROBE"
	fi
	rm -f "$NEW"
fi
exit $rc
