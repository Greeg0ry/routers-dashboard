"""MCP server for one Claude run: the only tools the model has.

Started by the Claude CLI (`python -m app.fix_mcp`). The environment names the
job, the routers the run may touch and the mode: "investigate" refuses commands
that change anything, "execute" additionally allows the exact commands of the approved plan. Every command is logged to job_log.
"""
import json
import os
import re
import shlex

from mcp.server.fastmcp import FastMCP

from . import collector, db

JOB = int(os.environ.get("RMON_JOB", "0"))
MODE = os.environ.get("RMON_MODE", "investigate")
DEVICES = set(filter(None, os.environ.get("RMON_DEVICES", "").split(",")))
MAX_CALLS = int(os.environ.get("RMON_MAX_CALLS", "20"))
# the commands the owner approved; only these may change a router
PLAN = {" ".join(c.split()) for c in json.loads(os.environ.get("RMON_PLAN", "[]"))}
OUTPUT_LIMIT = 5000
calls = 0

# Refused even when the owner approved the plan: these can cut the router off from its owner or from us.
_END = r"(?=\s|;|&|\||$)"
_RM_TARGET = "|".join([
    r"[^\s;|&]*(?:\$|\.\.|~)[^\s;|&]*",  # a path that is only known at run time
    r"(?![/-])[^\s;|&]+",  # relative to wherever the shell happens to be
    r"/\*?",
    r"/(?:etc|usr|lib|lib64|bin|sbin|overlay|rom|root|www|opt|var|tmp|proc|sys|dev|boot|mnt)(?:/\*|/)?",
    r"/etc/(?:config|init\.d|rc\.d|ssl|apk|opkg|crontabs|dropbear|tailscale|hotplug\.d|uci-defaults)(?:/\*|/)?",
    r"/usr/(?:bin|sbin|lib|share|libexec)(?:/\*|/)?",
])
FORBIDDEN = [(reason, re.compile(pattern, re.I)) for reason, pattern in [
    ("перезагрузка и выключение", r"\b(reboot|poweroff|halt)\b"),
    ("прошивка и сброс настроек", r"\b(sysupgrade|firstboot|jffs2reset|mtd)\b|factory[-_ ]?reset|\bmkfs|\bdd\s+(if|of)="),
    ("пароли и ключи доступа", r"\bpasswd\b|/etc/shadow|authorized_keys"),
    ("рекурсивное удаление системного каталога (можно удалять конкретные каталоги вроде /opt/zapret)",
     r"\brm\s+(?:-\w+\s+)*-\w*[rR]\w*(?:\s+[^\s;|&]+)*?\s+(?:" + _RM_TARGET + ")" + _END),
    ("tailscale", r"tailscale\s+(down|logout|set|up)"),
    ("остановка сети, SSH, firewall, веб-интерфейса или cron",
     r"(tailscale|dropbear|network|firewall|uhttpd|cron)\s+(stop|disable|restart|reload)|\b(ifdown|ifup|wifi)\b"),
    ("настройки сети, Wi-Fi, firewall, SSH и tailscale", r"\buci\b.*\b(dropbear|tailscale|network|wireless|firewall)\b"),
    # packages may be removed (as an approved plan step), except the ones that keep the router reachable and booting
    ("удаление системного пакета",
     r"\b(apk\s+del|opkg\s+remove)\b[^;|&]*\b(tailscale\w*|dropbear|openssh\S*|busybox|base-files|netifd|procd|ubus\w*|uci|libc|musl"
     r"|kernel|kmod-\S+|firewall4?|nftables\S*|dnsmasq\S*|odhcp\S+|apk\S*|opkg|curl|libcurl\S*|ca-bundle|ca-certificates|uhttpd\S*"
     r"|luci(?:-(?:base|ssl|light|nginx|mod-\S+|lib-\S+|theme-\S+))?(?![\w-])|wpad\S*|hostapd\S*)"),
    ("агент мониторинга", r"rmon"),
    ("отключение автозапуска zapret и forkop", r"(zapret2?|forkop|sing-box)\s+disable"),
    ("запуск скрипта прямо из сети", r"(curl|wget)[^|;&]*\|\s*(ba)?sh"),
]]


def forbidden(command):
    """The reason a command is never run, or None."""
    return next((reason for reason, pattern in FORBIDDEN if pattern.search(command)), None)


# Everything outside the approved plan must be a read. This is an allowlist: a command
# line passes only if every part of it is a known read-only command.
READ = {"cat", "head", "tail", "grep", "egrep", "fgrep", "wc", "sort", "uniq", "cut", "tr", "jsonfilter", "ls", "stat",
        "du", "df", "free", "uptime", "uname", "date", "ps", "pidof", "pgrep", "dmesg", "ping", "nslookup", "traceroute",
        "netstat", "ss", "iwinfo", "echo", "printf", "sleep", "test", "[", "true", "which", "md5sum", "sha256sum",
        "readlink", "nproc", "id", "hostname"}
_CURL_LONG = {"--max-time", "--connect-timeout", "--resolve", "--interface", "--silent", "--head", "--insecure",
              "--location", "--write-out", "--header", "--proxy", "--socks5", "--socks5-hostname", "--http1.1",
              "--http2", "--tlsv1.2", "--tlsv1.3", "--user-agent", "--show-error", "--ipv4", "--ipv6"}
_IP_WRITE = {"add", "del", "delete", "set", "flush", "replace", "change", "append", "prepend"}


# Downloads are not reads, but packages and lists have to be fetched somewhere: /tmp is
# always fine (it is RAM and gone after a reboot); while a plan is being carried out the
# zapret directories are open too. Installing what was downloaded is still a plan step.
DOWNLOAD_DIRS = ("/tmp/", "/opt/zapret/", "/opt/zapret2/") if MODE == "execute" else ("/tmp/",)
_WGET_LONG = {"--no-check-certificate", "--timeout", "--tries", "--user-agent", "--header", "--quiet", "--spider",
              "--continue", "--no-verbose", "--output-document", "--directory-prefix", "--show-progress", "--no-proxy"}


def _download_to(path):
    return path in ("-", "/dev/null") or (path.startswith(DOWNLOAD_DIRS) and ".." not in path and "$" not in path)


def _curl(args):
    for i, arg in enumerate(args):
        if arg.startswith("--"):
            if arg.split("=")[0] not in _CURL_LONG:
                return False
        elif arg.startswith("-") and len(arg) > 1:
            if set(arg[1:]) & set("OTKcDXdFJ"):
                return False
            if "o" in arg[1:] and not (arg.endswith("o") and i + 1 < len(args) and _download_to(args[i + 1])):
                return False
    return True


def _wget(args):
    """wget / uclient-fetch with an explicit destination inside DOWNLOAD_DIRS."""
    target, i = None, 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("--"):
            name, _, value = arg.partition("=")
            if name not in _WGET_LONG:
                return False
            if name in ("--output-document", "--directory-prefix"):
                target = (value or "".join(args[i + 1:i + 2])) + ("/" if name == "--directory-prefix" else "")
        elif arg.startswith("-") and len(arg) > 1:
            flags = arg[1:]
            for n, flag in enumerate(flags):
                if flag in "OPUTt":  # these take a value: the rest of the cluster or the next argument
                    value = flags[n + 1:]
                    if not value:
                        i += 1
                        value = "".join(args[i:i + 1])
                    if flag in "OP":
                        target = value + ("/" if flag == "P" and not value.endswith("/") else "")
                    break
                if flag not in "qcSv46":
                    return False
        i += 1
    return target is not None and _download_to(target)


SUB = {
    "uci": lambda a: [x for x in a if not x.startswith("-")][:1] in (["show"], ["get"], ["export"], ["changes"]),
    "nft": lambda a: [x for x in a if not x.startswith("-")][:1] == ["list"] and "-f" not in a,
    "ip": lambda a: not _IP_WRITE & set(a),
    "forkop": lambda a: bool(a) and a[0].startswith(("get_", "check_", "show_")),
    "ubus": lambda a: a[:1] == ["list"],
    "apk": lambda a: a[:1] in (["info"], ["list"], ["version"], ["policy"], ["search"]),
    "sed": lambda a: len(a) >= 2 and a[0] == "-n" and re.fullmatch(r"[0-9,;p$ ]+", a[1]) is not None,
    "opkg": lambda a: a[:1] in (["list"], ["list-installed"], ["info"], ["status"]),
    "service": lambda a: a[1:2] == ["status"],
    "sing-box": lambda a: a[:1] == ["version"],
    "command": lambda a: a[:1] == ["-v"],
    "logread": lambda a: "-f" not in a,
    "top": lambda a: any(x.startswith("-") and "b" in x for x in a),
    "find": lambda a: not {"-delete", "-exec", "-ok", "-execdir", "-fprint"} & set(a),
    "awk": lambda a: not any(w in x for x in a for w in ("system", "getline", ">", "|")) and "-f" not in a,
    "curl": _curl,
    "wget": _wget,
    "uclient-fetch": _wget,
}
_REDIRECT = re.compile(r">>?\s*/dev/null(?![\w./])|>&[12]")


def _reads(words):
    cmd, args = words[0], words[1:]
    if cmd.startswith("/etc/init.d/"):
        return args in (["status"], ["enabled"], ["running"], ["info"])
    name = cmd.rsplit("/", 1)[-1]
    if name in SUB:
        return SUB[name](args)
    return name in READ


def not_read_only(command):
    """None when the command line only reads; otherwise a short reason."""
    segments, cur, quote, i = [], [], None, 0
    while i < len(command):
        ch = command[i]
        substitution = ch == "`" or command.startswith("$(", i)
        if quote:
            if ch == quote:
                quote = None
            elif quote == '"' and substitution:
                return "подстановка команд запрещена"
            cur.append(ch)
        elif ch in "'\"":
            quote = ch
            cur.append(ch)
        elif ch == "\\":
            cur.append(command[i:i + 2])
            i += 1
        elif substitution:
            return "подстановка команд запрещена"
        elif ch == ">":
            found = _REDIRECT.match(command, i)
            if not found:
                return "запись в файл запрещена"
            if cur and cur[-1] in ("1", "2") and (len(cur) == 1 or cur[-2].isspace()):
                cur.pop()
            i = found.end() - 1
        elif ch == "<":
            return "перенаправление ввода запрещено"
        elif ch == "&" and command[i + 1:i + 2] != "&":
            return "фоновый запуск запрещён"
        elif ch in ";|&\n":
            segments.append("".join(cur))
            cur = []
            if ch == "&":
                i += 1
        else:
            cur.append(ch)
        i += 1
    if quote:
        return "незакрытая кавычка"
    for segment in segments + ["".join(cur)]:
        try:
            words = shlex.split(segment)
        except ValueError:
            return "не удалось разобрать команду"
        if words and not _reads(words):
            return f"«{segment.strip()[:60]}» не входит в список команд чтения"
    return None


SECRETS = [
    (re.compile(r'("(?:password|uuid|private_key|pre_shared_key|psk|secret|token|short_id|public_key)"\s*:\s*")[^"]+'), r"\1…"),
    (re.compile(r"\b(vless|vmess|trojan|ss|hysteria2?|tuic|socks5?)://\S+"), r"\1://…"),
    (re.compile(r"(option\s+(?:password|key|private_key|token|secret)\s+)\S+"), r"\1'…'"),
]

mcp = FastMCP("rmon")


def _device(name):
    row = db.one("SELECT * FROM devices WHERE name = ? AND present = 1", (name.strip(),))
    if not row or row["id"] not in DEVICES:
        return None
    return dict(row)


def _budget():
    global calls
    calls += 1
    return calls <= MAX_CALLS


def _clean(text):
    for pattern, replacement in SECRETS:
        text = pattern.sub(replacement, text)
    if len(text) > OUTPUT_LIMIT:
        half = OUTPUT_LIMIT // 2
        text = f"{text[:half]}\n… вырезано {len(text) - OUTPUT_LIMIT} символов …\n{text[-half:]}"
    return text


def _log(device, command, exit_code, output):
    db.x("INSERT INTO job_log (ts, job_id, device, mode, command, exit_code, output) VALUES (?,?,?,?,?,?,?)",
         (db.now(), JOB, device, MODE, command, exit_code, output[:2000]))


@mcp.tool()
async def router_run(device: str, command: str, timeout: int = 60) -> str:
    """Run a shell command as root on a router (OpenWrt, busybox ash) and return its exit code and output.

    Combine related commands into one call with ';'. Long output is cut in the middle, so filter it
    on the router (grep, head, tail). Secrets in the output are masked.

    Args:
        device: router name exactly as given in the task
        command: shell command line
        timeout: seconds to wait, at most 180
    """
    dev = _device(device)
    if not dev:
        return f"Роутер «{device}» не входит в эту задачу."
    if not _budget():
        return "Лимит вызовов исчерпан. Заверши работу и выдай итог по тому, что уже известно."
    reason = forbidden(command)
    if reason:
        _log(device, command, None, f"refused: forbidden: {reason}")
        return (f"Отказано, это запрещено всегда: {reason}. Остальные части команды допустимы — "
                "если без запрещённой части задача решается, владельцу нужен новый план без неё.")
    if " ".join(command.split()) not in PLAN:
        reason = not_read_only(command)
        if reason:
            _log(device, command, None, f"refused: {reason}")
            return (f"Отказано: {reason}. Вне утверждённого плана разрешены только команды чтения, "
                    "соединённые через ; | && — без циклов, $(…) и записи в файлы, "
                    f"и скачивание через wget -O / curl -o в {', '.join(DOWNLOAD_DIRS)}."
                    + ("" if MODE == "execute" else " Изменения включи в план исправления."))
    result, error = await collector.ssh_run(dev, command, max(5, min(int(timeout), 180)))
    if error:
        _log(device, command, None, f"error: {error}")
        return f"Команда не выполнена: {'неверный пароль SSH' if error == 'auth' else error}"
    output = _clean((str(result.stdout or "") + str(result.stderr or "")).strip())
    _log(device, command, result.exit_status, output)
    return f"exit {result.exit_status}\n{output}"


@mcp.tool()
async def router_check(device: str) -> str:
    """Run the dashboard's own probe on a router and return the current status of every check
    (internet, zapret, forkop, google, youtube, chatgpt, discord). Takes about 30 seconds.
    This is the verdict the owner sees, so use it to confirm that a fix worked.

    Args:
        device: router name exactly as given in the task
    """
    dev = _device(device)
    if not dev:
        return f"Роутер «{device}» не входит в эту задачу."
    if not _budget():
        return "Лимит вызовов исчерпан. Заверши работу и выдай итог по тому, что уже известно."
    kv, error = await collector.ssh_probe(dev)
    if error:
        return f"Проба не выполнена: {error}"
    results, _ = collector.evaluate(kv)
    text = "\n".join(f"{name}: {status} — {detail}" for name, (status, detail) in results.items() if name != "reach")
    _log(device, "(probe)", 0, text)
    return text


if __name__ == "__main__":
    mcp.run()
