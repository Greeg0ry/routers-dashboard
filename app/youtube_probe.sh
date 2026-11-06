# Included in the self-contained router probe by collector.py.
# No cookies, account credentials, player JS execution or signed URLs leave the router.
youtube_parse() {
	ucode -e '
import * as fs from "fs";
const dir = ARGV[1];
function emit(key, value) { printf("youtube_%s\t%s\n", key, value); }
function read_json(path) {
	try { return json(fs.readfile(path)); } catch (e) { return null; }
}
if (ARGV[0] == "page") {
	let api = read_json(dir + "/youtube_api_body");
	emit("api_valid", type(api) == "object" && api.provider_name == "YouTube" &&
		api.type == "video" && type(api.title) == "string" && length(api.title) > 0 ? 1 : 0);
	let page = fs.readfile(dir + "/youtube_watch_body") || "";
	let player = null;
	let marker = match(page, /ytInitialPlayerResponse\s*=\s*\{/);
	if (marker) {
		let start = index(page, marker[0]) + length(marker[0]) - 1;
		let depth = 0, quoted = false, escaped = false;
		for (let i = start; i < length(page); i++) {
			let ch = substr(page, i, 1);
			if (quoted) {
				if (escaped) escaped = false;
				else if (ch == "\\") escaped = true;
				else if (ch == "\"") quoted = false;
			} else if (ch == "\"") quoted = true;
			else if (ch == "{") depth++;
			else if (ch == "}" && --depth == 0) {
				try { player = json(substr(page, start, i - start + 1)); } catch (e) {}
				break;
			}
		}
	}
	let valid = type(player) == "object" && player.videoDetails?.videoId == "jNQXAC9IVRw";
	emit("watch_valid", valid ? 1 : 0);
	let status = player?.playabilityStatus?.status || "UNCONFIRMED";
	// Only a fixed set of states is reported, never arbitrary page content.
	if (index(["OK", "LOGIN_REQUIRED", "UNPLAYABLE", "ERROR", "CONTENT_CHECK_REQUIRED", "AGE_CHECK_REQUIRED"], status) < 0)
		status = "UNCONFIRMED";
	emit("player_status", status);
	let selected = null;
	if (valid && status == "OK") {
		let formats = (player.streamingData?.formats || []);
		for (let format in (player.streamingData?.adaptiveFormats || [])) push(formats, format);
		for (let format in formats) {
			if (type(format.url) != "string" || !match(format.url, /^https:\/\/[a-zA-Z0-9.-]+\.googlevideo\.com\//) ||
				type(format.mimeType) != "string" || index(format.mimeType, "video/") != 0)
				continue;
			if (!selected || (format.bitrate || 0) < (selected.bitrate || 0)) selected = format;
		}
	}
	if (selected) fs.writefile(dir + "/youtube_media_url", selected.url);
	emit("media_candidate", selected ? 1 : 0);
	emit("parser", "ok");
} else if (ARGV[0] == "observe") {
	let before = read_json(dir + "/youtube_before.json");
	let after = read_json(dir + "/youtube_after.json");
	let bytes = 0, clients = {};
	let valid = type(before?.connections) == "array" && type(after?.connections) == "array";
	if (valid) {
		let baseline = {};
		for (let conn in before.connections) if (conn.id) baseline[conn.id] = int(conn.download) || 0;
		let own_ips = split(trim(fs.readfile(dir + "/youtube_own_ips") || ""), "\n");
		for (let conn in after.connections) {
			let source = conn.metadata?.sourceIP;
			let host = conn.metadata?.host || conn.metadata?.destinationIP;
			if (!conn.id || type(source) != "string" || !length(source) || index(own_ips, source) >= 0 ||
				match(source, /^(127\.|::1$)/) || type(host) != "string" ||
				!match(host, /^rr?[0-9]+---[a-zA-Z0-9-]+\.googlevideo\.com$/)) continue;
			let delta = (int(conn.download) || 0) - (baseline[conn.id] || 0);
			if (delta > 0) { bytes += delta; clients[source] = true; }
		}
	}
	emit("active_bytes", bytes);
	emit("active_clients", length(keys(clients)));
	emit("observer", valid ? "available" : "unavailable");
}
' "$1" "$T"
}

youtube_snapshot() {
	[ -n "$youtube_lan" ] || return 0
	curl -q -s -m 2 --max-filesize 262144 -o "$T/youtube_$1.json" \
		"http://$youtube_lan:9090/connections" 2>/dev/null
}

youtube_fetch_watch() {
	curl -q -s -L $youtube_compress --proto '=https' --proto-redir '=https' --max-redirs 3 \
		-m 8 --max-filesize 2097152 -A "$youtube_ua" -o "$T/youtube_watch_body" \
		-w '%{http_code} %{time_total} %{size_download}\n' \
		'https://www.youtube.com/watch?v=jNQXAC9IVRw' >"$T/youtube_watch" 2>/dev/null
}

youtube_fetch_api() {
	curl -q -s -L $youtube_compress --proto '=https' --proto-redir '=https' --max-redirs 3 \
		-m 6 --max-filesize 65536 -A "$youtube_ua" -o "$T/youtube_api_body" \
		-w '%{http_code} %{time_total} %{size_download}\n' \
		'https://www.youtube.com/oembed?url=https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3DjNQXAC9IVRw&format=json' \
		>"$T/youtube_api" 2>/dev/null
}

youtube_probe() {
	kv youtube_probe_version 2
	youtube_lan=$(uci -q get network.lan.ipaddr 2>/dev/null | cut -d/ -f1)
	youtube_compress=""
	curl -V 2>/dev/null | grep -Eq 'zlib|brotli|zstd' && youtube_compress="--compressed"
	youtube_ua='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
	ip addr show 2>/dev/null | awk '$1 == "inet" || $1 == "inet6" {split($2,a,"/"); print a[1]}' >"$T/youtube_own_ips"
	youtube_snapshot before
	youtube_attempt=0
	while [ "$youtube_attempt" -lt 2 ]; do
		youtube_fetch_api & youtube_api_pid=$!
		youtube_fetch_watch
		wait "$youtube_api_pid"
		youtube_attempt=$((youtube_attempt + 1))
		# Retry only a complete network failure, not an explicit response or bot check.
		grep -q '^000 ' "$T/youtube_watch" && grep -q '^000 ' "$T/youtube_api" || break
	done
	kv youtube_watch "$(cat "$T/youtube_watch")"
	kv youtube_api "$(cat "$T/youtube_api")"
	if command -v ucode >/dev/null 2>&1; then
		youtube_parse page || kv youtube_parser unavailable
		if [ -s "$T/youtube_media_url" ]; then
			curl -q -s -L --proto '=https' --proto-redir '=https' --max-redirs 3 -m 8 \
				--range 0-65535 --max-filesize 131072 -A "$youtube_ua" \
				-o "$T/youtube_media_body" -w '%{http_code} %{time_total} %{size_download} %{content_type}\n' \
				"$(cat "$T/youtube_media_url")" >"$T/youtube_media" 2>/dev/null
			kv youtube_media_exit "$?"
			kv youtube_media "$(cat "$T/youtube_media")"
		fi
		# Compare counters, never cumulative totals: a buffered or idle session is no proof.
		sleep 3
		youtube_snapshot after
		youtube_parse observe
	else
		kv youtube_parser unavailable
	fi
}
