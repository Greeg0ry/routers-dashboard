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
FORBIDDEN = re.compile("|".join([
    r"\breboot\b", r"\bpoweroff\b", r"\bhalt\b", r"\bsysupgrade\b", r"\bfirstboot\b", r"\bjffs2reset\b",
    r"factory[-_ ]?reset", r"\bmkfs", r"\bdd\s+(if|of)=", r"\bmtd\b", r"\bpasswd\b", r"/etc/shadow",
    r"authorized_keys", r"\brm\s+(-\w+\s+)*-\w*[rR]\w*\s+(-\w+\s+)*(?!/tmp/)",
    r"tailscale\s+(down|logout|set|up)", r"(tailscale|dropbear|network|firewall|uhttpd|cron)\s+(stop|disable|restart|reload)",
    r"\buci\b.*\b(dropbear|tailscale|network|wireless|firewall)\b", r"\b(ifdown|ifup|wifi)\b",
    r"\b(apk\s+del|opkg\s+remove)\b", r"rmon", r"(zapret2?|forkop|sing-box)\s+disable",
    r"(curl|wget)[^|;&]*\|\s*(ba)?sh",
]), re.I)

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


def _curl(args):
    for i, arg in enumerate(args):
        if arg.startswith("--"):
            if arg.split("=")[0] not in _CURL_LONG:
                return False
        elif arg.startswith("-") and len(arg) > 1:
            if set(arg[1:]) & set("OTKcDXdFJ"):
                return False
            if "o" in arg[1:] and args[i + 1:i + 2] != ["/dev/null"]:
                return False
    return True


SUB = {
    "uci": lambda a: [x for x in a if not x.startswith("-")][:1] in (["show"], ["get"], ["export"], ["changes"]),
    "nft": lambda a: [x for x in a if not x.startswith("-")][:1] == ["list"] and "-f" not in a,
    "ip": lambda a: not _IP_WRITE & set(a),
    "forkop": lambda a: bool(a) and a[0].startswith(("get_", "check_", "show_")),
    "ubus": lambda a: a[:1] == ["list"],
    "apk": lambda a: a[:1] in (["info"], ["list"], ["version"], ["policy"]),
    "opkg": lambda a: a[:1] in (["list"], ["list-installed"], ["info"], ["status"]),
    "service": lambda a: a[1:2] == ["status"],
    "sing-box": lambda a: a[:1] == ["version"],
    "command": lambda a: a[:1] == ["-v"],
    "logread": lambda a: "-f" not in a,
    "top": lambda a: any(x.startswith("-") and "b" in x for x in a),
    "find": lambda a: not {"-delete", "-exec", "-ok", "-execdir", "-fprint"} & set(a),
    "awk": lambda a: not any(w in x for x in a for w in ("system", "getline", ">", "|")) and "-f" not in a,
    "curl": _curl,
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
    if FORBIDDEN.search(command):
        _log(device, command, None, "refused: forbidden")
        return "Отказано: команда из запрещённого списка (перезагрузка, прошивка, сеть, SSH, tailscale, удаление пакетов)."
    if " ".join(command.split()) not in PLAN:
        reason = not_read_only(command)
        if reason:
            _log(device, command, None, f"refused: {reason}")
            return (f"Отказано: {reason}. Вне утверждённого плана разрешены только команды чтения, "
                    "соединённые через ; | && — без циклов, $(…) и записи в файлы."
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
