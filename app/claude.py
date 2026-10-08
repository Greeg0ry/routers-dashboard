"""Claude Code CLI on this server: subscription login and headless runs."""
import asyncio
import json
import logging
import os
import re
import time

from . import config

log = logging.getLogger("monitor.claude")

# Runs get a clean environment: the service's secrets must not reach the CLI.
ENV = {
    "HOME": os.environ.get("HOME", ""),
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "LANG": "C.UTF-8",
    "DISABLE_AUTOUPDATER": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
}
_URL = re.compile(r"https://claude\.(?:com|ai)/[^\s\x07\x1b]+")
_ANSI = re.compile(r"\x1b\][^\x07]*\x07|\x1b\[[0-9;?]*[A-Za-z]")
_status = {"ts": 0, "data": {}}


class ClaudeError(Exception):
    pass


async def auth_status(fresh=False) -> dict:
    """{"loggedIn": bool, ...} as printed by `claude auth status`; cached for a minute."""
    if not fresh and time.time() - _status["ts"] < 60:
        return _status["data"]
    try:
        proc = await asyncio.create_subprocess_exec(
            config.CLAUDE_BIN, "auth", "status", "--json", env=ENV,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), 30)
        data = json.loads(out.decode() or "{}")
    except (OSError, ValueError, asyncio.TimeoutError, TimeoutError) as e:
        data = {"loggedIn": False, "error": type(e).__name__}
    _status.update(ts=time.time(), data=data)
    return data


async def logout():
    proc = await asyncio.create_subprocess_exec(
        config.CLAUDE_BIN, "auth", "logout", env=ENV, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    await asyncio.wait_for(proc.wait(), 30)
    _status["ts"] = 0


class Login:
    """`claude auth login` behind a pseudo-terminal: it prints a link and waits for the code from the browser."""

    def __init__(self):
        self.proc = None
        self.master = None
        self.buf = b""

    def _read(self):
        try:
            self.buf += os.read(self.master, 65536)
        except OSError:
            asyncio.get_running_loop().remove_reader(self.master)

    async def start(self) -> str:
        import pty  # POSIX only; the rest of the module also loads on a Windows dev machine
        self.master, slave = pty.openpty()
        self.proc = await asyncio.create_subprocess_exec(
            config.CLAUDE_BIN, "auth", "login", "--claudeai", env={**ENV, "BROWSER": "/bin/false", "TERM": "dumb"},
            stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
        os.close(slave)
        asyncio.get_running_loop().add_reader(self.master, self._read)
        for _ in range(60):
            await asyncio.sleep(0.5)
            found = _URL.search(self.buf.decode("utf-8", "replace"))
            if found and b"Paste code" in self.buf:
                return found[0]
            if self.proc.returncode is not None:
                break
        text = self.output()
        self.close()
        raise ClaudeError(text[-300:] or "claude auth login не выдал ссылку")

    def output(self) -> str:
        return _ANSI.sub("", self.buf.decode("utf-8", "replace")).strip()

    async def submit(self, code: str) -> bool:
        self.buf = b""
        os.write(self.master, code.strip().encode() + b"\r")
        try:
            await asyncio.wait_for(self.proc.wait(), 90)
        except (asyncio.TimeoutError, TimeoutError):
            pass
        self.close()
        return bool((await auth_status(fresh=True)).get("loggedIn"))

    def close(self):
        if self.master is not None:
            try:
                asyncio.get_running_loop().remove_reader(self.master)
                os.close(self.master)
            except (OSError, ValueError):
                pass
            self.master = None
        if self.proc and self.proc.returncode is None:
            self.proc.kill()


def _usage(data) -> dict:
    u = data.get("usage") or {}
    return {
        "in": (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0),
        "cached": u.get("cache_read_input_tokens") or 0,
        "out": u.get("output_tokens") or 0,
        "turns": data.get("num_turns") or 0,
        "runs": 1,
    }


def _json(text):
    found = re.search(r"\{.*\}", text or "", re.S)
    try:
        return json.loads(found[0]) if found else None
    except ValueError:
        return None


async def run(prompt, *, model, system, schema=None, mcp=None, tools=(), effort=None, timeout=900):
    """One headless run. Returns (answer, usage); answer is a dict when a schema is given, else text.

    The run has no built-in tools at all: it can only call the MCP tools named in
    `tools`, and the short system prompt replaces Claude Code's own.
    """
    args = [config.CLAUDE_BIN, "-p", "--output-format", "json", "--model", model, "--system-prompt", system,
            "--no-session-persistence", "--disable-slash-commands", "--strict-mcp-config",
            "--permission-prompts", "none", "--setting-sources", "", "--tools", ""]
    if effort:
        args += ["--effort", effort]
    if schema:
        args += ["--json-schema", json.dumps(schema)]
    if mcp:
        args += ["--mcp-config", str(mcp), "--allowedTools", ",".join(tools)]
    proc = await asyncio.create_subprocess_exec(
        *args, env=ENV, cwd=str(config.DATA_DIR), stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
    try:
        out, err = await asyncio.wait_for(proc.communicate(prompt.encode()), timeout)
    except (asyncio.TimeoutError, TimeoutError):
        proc.kill()
        raise ClaudeError(f"модель {model} не уложилась в {timeout // 60} мин")
    except asyncio.CancelledError:
        proc.kill()
        raise
    try:
        data = json.loads(out.decode("utf-8", "replace"))
        if isinstance(data, list):  # some versions print the whole event list
            data = next(e for e in reversed(data) if e.get("type") == "result")
    except (ValueError, StopIteration):
        raise ClaudeError((err.decode("utf-8", "replace") or out.decode("utf-8", "replace"))[-300:].strip()
                          or f"claude завершился с кодом {proc.returncode}")
    text = str(data.get("result") or "")
    if data.get("is_error"):
        if re.search(r"/login|not logged in|authentication|OAuth", text, re.I):
            _status["ts"] = 0
            raise ClaudeError("Claude не авторизован. Отправьте боту /login")
        raise ClaudeError(text[:300] or str(data.get("subtype")))
    if not schema:
        return text, _usage(data)
    answer = data.get("structured_output")
    if not isinstance(answer, dict):
        answer = _json(text)
    if not isinstance(answer, dict):
        raise ClaudeError(f"модель {model} ответила не по формату: {text[:200]}")
    return answer, _usage(data)
