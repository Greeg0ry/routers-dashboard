# routers-dashboard probe. Read-only: changes nothing on the router.
# Piped to `sh -s` over SSH, prints "key<TAB>value" lines.
main() {
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
T=/tmp/.rmon.$$
mkdir -p "$T" || exit 1
trap 'rm -rf "$T"' EXIT INT TERM

kv() { printf '%s\t%s\n' "$1" "$(printf '%s' "$2" | tr '\n\t' '  ')"; }
yn() { "$@" >/dev/null 2>&1 && echo 1 || echo 0; }

# --- site checks, in parallel -------------------------------------------------
SITES=""
fetch() { curl -s -o /dev/null -m 10 -w '%{http_code} %{time_total} %{remote_ip}' "$1" 2>/dev/null; }
site() {
	(
		r=$(fetch "$2")
		# one retry so a single lost handshake is not reported as an outage
		case "$r" in 000*) r=$(fetch "$2") ;; esac
		echo "$r" >"$T/$1"
	) </dev/null >/dev/null 2>&1 &
	SITES="$SITES $!"
}
if command -v curl >/dev/null 2>&1; then
	kv curl 1
	site ya https://ya.ru/
	site google https://www.google.com/generate_204
	site youtube https://www.youtube.com/generate_204
	site googlevideo https://manifest.googlevideo.com/generate_204
	site openai https://api.openai.com/v1/models
	site discord https://discord.com/api/v9/gateway
	site discord_gw https://gateway.discord.gg/
	site discord_cdn https://cdn.discordapp.com/
	site discord_media https://media.discordapp.net/
	(curl -s -m 10 https://chatgpt.com/cdn-cgi/trace 2>/dev/null | grep -E '^(loc|colo)=' | tr '\n' ' ' >"$T/gpttrace") </dev/null >/dev/null 2>&1 &
	SITES="$SITES $!"
else
	kv curl 0
fi

# --- forkop, in background with a deadline ------------------------------------
FP=""
if [ -x /usr/bin/forkop ]; then
	kv forkop_installed 1
	kv forkop_enabled "$(yn /etc/init.d/forkop enabled)"
	kv singbox_procs "$(pidof sing-box | wc -w)"
	(
		forkop get_status 2>/dev/null | tr -d '\n' >"$T/f_status"
		forkop check_nft_rules 2>/dev/null | tr -d '\n' >"$T/f_nft"
		forkop check_fakeip 2>/dev/null | tr -d '\n' >"$T/f_fakeip"
		forkop show_version 2>/dev/null | head -n 1 >"$T/f_version"
		LAN=$(uci -q get network.lan.ipaddr | cut -d/ -f1)
		[ -n "$LAN" ] && [ -z "$RMON_NO_CLASH" ] && curl -s -m 5 "http://$LAN:9090/proxies" 2>/dev/null | head -c 400000 | tr -d '\n' >"$T/f_clash"
	) </dev/null >/dev/null 2>&1 &
	FP=$!
else
	kv forkop_installed 0
fi

# --- system -------------------------------------------------------------------
[ -f /etc/openwrt_release ] && . /etc/openwrt_release
kv model "$(cat /tmp/sysinfo/model 2>/dev/null)"
kv release "$DISTRIB_RELEASE"
kv uptime "$(cut -d. -f1 /proc/uptime)"
kv load "$(cut -d' ' -f1-3 /proc/loadavg)"
kv mem "$(awk '/^MemTotal:/{t=$2} /^MemAvailable:/{a=$2} END{print t" "a}' /proc/meminfo)"
kv overlay "$(df -k /overlay 2>/dev/null | awk 'NR==2{print $2" "$4}')"

# --- zapret / zapret2 ---------------------------------------------------------
for z in zapret zapret2; do
	[ -x "/etc/init.d/$z" ] || continue
	[ "$z" = zapret ] && b=nfqws || b=nfqws2
	kv "${z}_installed" 1
	kv "${z}_enabled" "$(yn /etc/init.d/$z enabled)"
	kv "${z}_procs" "$(pidof $b | wc -w)"
	kv "${z}_nft" "$(yn nft list table inet $z)"
done

# --- collect ------------------------------------------------------------------
[ -n "$SITES" ] && wait $SITES
for f in ya google youtube googlevideo openai gpttrace discord discord_gw discord_cdn discord_media; do
	kv "site_$f" "$(cat "$T/$f" 2>/dev/null)"
done

if [ -n "$FP" ]; then
	i=0
	while kill -0 "$FP" 2>/dev/null && [ "$i" -lt 25 ]; do
		sleep 1
		i=$((i + 1))
	done
	kill "$FP" 2>/dev/null
	for f in status nft fakeip version clash; do
		kv "forkop_$f" "$(cat "$T/f_$f" 2>/dev/null)"
	done
fi
kv done 1
}
main
