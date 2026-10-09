# routers-dashboard: Sing-Box X on a router whose flash is too small for forkop's own switch.
# forkop wants room for the old and the new sing-box together and does not count on filesystem
# compression, so it refuses. Here the old package goes first: it is fetched into RAM for the
# way back, forkop is stopped, the package is removed, and forkop then installs X itself.
# Piped to a file and run detached; the last line is RESULT=ok|already|untouched|rolled_back|broken.
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
W=/tmp/rmon-sbx
INIT=/etc/init.d/forkop
BIN=/usr/bin/sing-box
NEED_KB=14000 # X unpacks to about 8.7 MB; forkop itself asks for the package plus 2 MB

say() { echo "[$(date +%H:%M:%S)] $*"; }
free_kb() { df -k /overlay | awk 'NR==2{print $4}'; }
running() { forkop get_status 2>/dev/null | grep -q '"running": *1'; }
finish() {
	rm -rf "$W"
	echo "RESULT=$1"
	exit "$2"
}

if command -v apk >/dev/null 2>&1; then
	PM=apk
	inst() { apk list -I 2>/dev/null | grep -oE "^$1-[0-9][^ ]*" | head -n 1; }
else
	PM=opkg
	inst() { opkg list-installed 2>/dev/null | awk -v p="$1" '$1 == p { print $1 "-" $3 }'; }
fi
if [ -n "$(inst sing-box-x)" ]; then
	say "уже установлен $(inst sing-box-x)"
	finish already 0
fi
NAME=""
for n in sing-box-tiny sing-box; do
	[ -n "$(inst $n)" ] && NAME=$n && break
done
if [ -z "$NAME" ]; then
	say "sing-box стоит не пакетом sing-box или sing-box-tiny (например, Extended) — этот способ не подходит"
	finish untouched 3
fi
OLD=$(inst $NAME)
say "установлен $OLD ($PM), свободно на флеше $(free_kb) КиБ"

# 1. the way back, before anything is touched
rm -rf "$W" && mkdir -p "$W" && cd "$W" || finish untouched 3
if [ "$PM" = apk ]; then
	apk update >/dev/null 2>&1
	apk fetch "$NAME" >fetch.log 2>&1
else
	opkg update >/dev/null 2>&1
	opkg download "$NAME" >fetch.log 2>&1
fi
RB=$(ls "$W"/$NAME[-_][0-9]*.apk "$W"/$NAME[-_][0-9]*.ipk 2>/dev/null | head -n 1)
if [ -z "$RB" ] || [ "$(wc -c <"$RB")" -lt 1000000 ]; then
	say "не удалось скачать $NAME для отката: $(tail -n 2 fetch.log | tr '\n' ' ')"
	say "на роутере ничего не изменено"
	finish untouched 3
fi
say "пакет для отката скачан в память: $(basename "$RB"), $(($(wc -c <"$RB") / 1024)) КиБ"

running && WAS=1 || WAS=0
start_forkop() {
	if [ "$WAS" != 1 ]; then
		say "forkop до этого не работал — оставлен остановленным"
		return 0
	fi
	"$INIT" start >/dev/null 2>&1
	i=0
	while [ "$i" -lt 36 ]; do
		running && [ -n "$(pidof sing-box)" ] && return 0
		sleep 5
		i=$((i + 1))
	done
	return 1
}
rollback() {
	say "ОТКАТ: $1"
	if [ "$PM" = apk ]; then
		apk del sing-box-x </dev/null >/dev/null 2>&1
		# dependencies that left together with the old package come back from the repository
		apk add --allow-untrusted "$RB" </dev/null >rb.log 2>&1 ||
			apk add --no-network --allow-untrusted "$RB" </dev/null >rb.log 2>&1
	else
		opkg remove sing-box-x </dev/null >/dev/null 2>&1
		opkg install --force-overwrite --force-downgrade "$RB" </dev/null >rb.log 2>&1
	fi
	if [ -x "$BIN" ] && [ -n "$(inst $NAME)" ] && start_forkop; then
		say "возвращён $(inst $NAME), forkop $([ "$WAS" = 1 ] && echo работает || echo 'остановлен, как и был')"
		finish rolled_back 2
	fi
	say "вернуть прежний sing-box не удалось или forkop с ним не запускается: $(tail -n 3 rb.log | tr '\n' ' ')"
	say "пакет для отката оставлен на роутере в $W (до перезагрузки)"
	echo "RESULT=broken"
	exit 4
}

# 2. stop forkop the way its own package switch does, then free the flash
say "останавливаю forkop"
env FORKOP_INTERNAL_SERVICE_STOP=1 "$INIT" stop >/dev/null 2>&1
forkop restore_dnsmasq >/dev/null 2>&1
i=0
while [ -n "$(pidof sing-box)" ] && [ "$i" -lt 20 ]; do
	sleep 1
	i=$((i + 1))
done
if [ -n "$(pidof sing-box)" ]; then
	say "sing-box не остановился, пакет не удалялся"
	start_forkop
	finish untouched 3
fi
if [ "$PM" = apk ]; then apk del "$NAME" </dev/null >rm.log 2>&1; else opkg remove "$NAME" </dev/null >rm.log 2>&1; fi
[ -z "$(inst $NAME)" ] || rollback "не удалось удалить $NAME: $(tail -n 2 rm.log | tr '\n' ' ')"
sync
# the filesystem gives the blocks back with a delay
i=0
while [ "$(free_kb)" -lt "$NEED_KB" ] && [ "$i" -lt 10 ]; do
	sleep 3
	i=$((i + 1))
done
FREE=$(free_kb)
say "$OLD удалён, свободно на флеше $FREE КиБ"
[ "$FREE" -ge "$NEED_KB" ] || rollback "после удаления свободно только $FREE КиБ, для X нужно $NEED_KB"

# 3. forkop installs X itself: its mirror, checksum, version check, variant marker
say "forkop ставит Sing-Box X"
forkop component_action sing_box install_x '' >x.json 2>&1
say "ответ forkop: $(tr -d '\n' <x.json | tail -c 300)"
grep -q '"success": *true' x.json && [ -n "$(inst sing-box-x)" ] && [ -x "$BIN" ] || rollback "forkop не установил X"

# 4. back in service
start_forkop || rollback "forkop не запускается с X"
say "готово: $(inst sing-box-x), свободно на флеше $(free_kb) КиБ, forkop $([ "$WAS" = 1 ] && echo работает || echo 'остановлен, как и был')"
finish ok 0
