#!/bin/sh
set -eu
command -v youtube_parse >/dev/null 2>&1 || . ./app/youtube_probe.sh
T=$(mktemp -d /tmp/rmon-youtube-fixture.XXXXXX)
chmod 700 "$T"
trap 'rm -rf "$T"' EXIT INT TERM
cat >"$T/youtube_api_body" <<'JSON'
{"provider_name":"YouTube","type":"video","title":"Test"}
JSON
cat >"$T/youtube_watch_body" <<'HTML'
<html><script>var ytInitialPlayerResponse = {"videoDetails":{"videoId":"jNQXAC9IVRw","title":"A brace } in a string and an escaped quote \""},"playabilityStatus":{"status":"OK"},"streamingData":{"formats":[{"bitrate":1,"mimeType":"video/mp4","url":"https://googlevideo.com.evil.invalid/video"},{"bitrate":100,"mimeType":"video/mp4","url":"https://r1---sn-test.googlevideo.com/videoplayback?test=1"}]}}; otherJavascript();</script></html>
HTML
youtube_parse page >"$T/page_metrics"
grep -q "^$(printf 'youtube_watch_valid\t1')$" "$T/page_metrics"
grep -q "^$(printf 'youtube_api_valid\t1')$" "$T/page_metrics"
grep -q "^$(printf 'youtube_media_candidate\t1')$" "$T/page_metrics"
[ "$(cat "$T/youtube_media_url")" = 'https://r1---sn-test.googlevideo.com/videoplayback?test=1' ]
echo 'PASS: actual player JSON, escaped strings and CDN URL validation'
cat >"$T/youtube_before.json" <<'JSON'
{"connections":[{"id":"watching","download":1000000},{"id":"idle","download":50000000},{"id":"router","download":0},{"id":"manifest","download":0}]}
JSON
cat >"$T/youtube_after.json" <<'JSON'
{"connections":[{"id":"watching","download":1065536,"metadata":{"sourceIP":"192.168.2.50","host":"rr2---sn-test.googlevideo.com"}},{"id":"idle","download":50000000,"metadata":{"sourceIP":"192.168.2.51","host":"r1---sn-test.googlevideo.com"}},{"id":"router","download":9000000,"metadata":{"sourceIP":"192.168.0.102","host":"r1---sn-test.googlevideo.com"}},{"id":"manifest","download":9000000,"metadata":{"sourceIP":"192.168.2.50","host":"manifest.googlevideo.com"}}]}
JSON
printf '192.168.2.1\n192.168.0.102\n127.0.0.1\n' >"$T/youtube_own_ips"
youtube_parse observe >"$T/active_metrics"
grep -q "^$(printf 'youtube_active_bytes\t65536')$" "$T/active_metrics"
grep -q "^$(printf 'youtube_active_clients\t1')$" "$T/active_metrics"
echo 'PASS: only new video bytes on client connections count as playback'
printf 'not JSON' >"$T/youtube_before.json"
youtube_parse observe >"$T/invalid_metrics"
grep -q "^$(printf 'youtube_active_bytes\t0')$" "$T/invalid_metrics"
grep -q "^$(printf 'youtube_observer\tunavailable')$" "$T/invalid_metrics"
echo 'PASS: unavailable connection API cannot claim playback'
