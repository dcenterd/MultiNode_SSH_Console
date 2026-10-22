"""SSH logic for MultiNode SSH Console (no GUI imports, so it is easy to test).

Written by Zareh Kasparian with Claude Code support.

Public API:
    parse_targets(text, default_port) -> (targets, errors)
    run_command(target, username, password, command, ...) -> Result
    run_all(targets, ..., on_result=callback, cancel=Event) -> list[Result]
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Optional

import paramiko

OK = "OK"
FAILED = "FAILED"  # command ran but exited non-zero
ERROR = "ERROR"  # could not connect / authenticate / run
UNREACHABLE = "UNREACHABLE"  # failed the quick TCP port pre-check
TIMEOUT = "TIMEOUT"
CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class Target:
    host: str
    port: int

    def __str__(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{host}:{self.port}"


@dataclass
class Result:
    target: Target
    status: str = ERROR
    exit_status: Optional[int] = None
    output: str = ""
    error: str = ""  # stderr from the command, or the connection error text
    seconds: float = 0.0


_SPLIT = re.compile(r"[,\s;]+")
_HOSTNAME = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?$")


def _parse_one(token: str, default_port: int) -> Target:
    host, port = token, default_port
    if token.startswith("["):  # [ipv6]:port or [ipv6]
        end = token.find("]")
        if end == -1:
            raise ValueError("missing ']'")
        host = token[1:end]
        rest = token[end + 1 :]
        if rest:
            if not rest.startswith(":"):
                raise ValueError("unexpected text after ']'")
            port = int(rest[1:])
    elif token.count(":") == 1:  # host:port / ipv4:port
        host, p = token.split(":")
        port = int(p)
    # else: bare hostname/ipv4, or a bare ipv6 (several colons) on default port
    if not host:
        raise ValueError("empty host")
    if ":" not in host and not _HOSTNAME.match(host):
        raise ValueError("invalid host name")
    if not 1 <= port <= 65535:
        raise ValueError("port out of range")
    return Target(host, port)


def parse_targets(text: str, default_port: int = 22):
    """Turn free text into unique targets.

    Accepts one or many addresses separated by newlines, spaces, commas or
    semicolons; ``host:port`` and ``[ipv6]:port`` override the default port;
    anything after ``#`` on a line is a comment. Returns (targets, errors)
    where errors is a list of (token, reason) for entries that were skipped.
    """
    targets: list[Target] = []
    errors: list[tuple[str, str]] = []
    seen: set[Target] = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip().lstrip("﻿")
        for token in _SPLIT.split(line):
            if not token:
                continue
            try:
                target = _parse_one(token, default_port)
            except ValueError as e:
                errors.append((token, str(e)))
                continue
            if target not in seen:
                seen.add(target)
                targets.append(target)
    return targets, errors


class HostKeyRejected(paramiko.SSHException):
    """The server's host key was not accepted (new key declined, or changed)."""


def fingerprint(key: paramiko.PKey) -> str:
    """OpenSSH-style SHA256 fingerprint, e.g. SHA256:abc..."""
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def public_key_line(key: paramiko.PKey) -> str:
    """The key in authorized_keys / known_hosts form: '<type> <base64>'."""
    return f"{key.get_name()} {key.get_base64()}"


def default_known_hosts_path() -> str:
    if os.name == "nt":  # Windows: %APPDATA%\\remote-ssh-console\\known_hosts
        base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "remote-ssh-console", "known_hosts")


def list_known_hosts(path: str):
    """[(host_entry, key_type, fingerprint, public_key_line)] from our known_hosts file."""
    keys = paramiko.HostKeys()
    try:
        keys.load(path)
    except OSError:
        return []
    rows = []
    for name in keys.keys():
        for key_type, key in keys[name].items():
            rows.append((name, key_type, fingerprint(key), public_key_line(key)))
    return sorted(rows)


def remove_known_host(path: str, name: str) -> None:
    keys = paramiko.HostKeys()
    keys.load(path)
    if name in keys:
        del keys[name]
    keys.save(path)


ACCEPT = "accept"
ACCEPT_ALL = "accept_all"  # accept this and every other *new* host this run
ACCEPT_MOVE = "accept_move"  # accept, and forget the old address(es) of this same key
REJECT = "reject"


@dataclass(frozen=True)
class HostKeyQuestion:
    target: Target
    key_type: str
    fingerprint: str
    public_key: str
    changed: bool = False  # True: we already knew a different key for this host
    old_fingerprint: str = ""
    # Other addresses that already have exactly this key accepted (device got a new IP?)
    same_key_hosts: tuple = ()


class HostKeyVerifier:
    """Trust-on-first-use host key store (OpenSSH known_hosts format).

    `prompt(question) -> ACCEPT | ACCEPT_ALL | REJECT` is called, one at a time,
    for unknown hosts and for hosts whose key changed. It may block (a GUI
    dialog). Known keys that match are accepted silently.
    """

    def __init__(
        self, path: str, prompt: Callable[[HostKeyQuestion], str], auto_trust_known_keys: bool = False
    ):
        self.path = path
        self.prompt = prompt
        self.accept_all_new = False
        # Silently accept an unknown address when its key is already trusted
        # for another address (e.g. the device was given a new IP).
        self.auto_trust_known_keys = auto_trust_known_keys
        self._lock = threading.Lock()  # guards the key file and serialises prompts
        self._keys = paramiko.HostKeys()
        try:
            self._keys.load(path)
        except OSError:
            pass

    @staticmethod
    def _name(target: Target) -> str:
        return target.host if target.port == 22 else f"[{target.host}]:{target.port}"

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._keys.save(self.path)

    def verify(self, target: Target, key: paramiko.PKey) -> None:
        """Return if trusted; raise HostKeyRejected otherwise."""
        name = self._name(target)
        with self._lock:
            known = self._keys.lookup(name)
            old = None
            if known is not None:
                same = known.get(key.get_name())
                if same is not None and same.asbytes() == key.asbytes():
                    return
                old = same if same is not None else next(iter(known.values()))
            same_hosts = self._hosts_with_key(key) if old is None else []
            if known is None and (self.accept_all_new or (same_hosts and self.auto_trust_known_keys)):
                self._remember(name, key)
                return
            question = HostKeyQuestion(
                target, key.get_name(), fingerprint(key), public_key_line(key),
                changed=old is not None,
                old_fingerprint=fingerprint(old) if old is not None else "",
                same_key_hosts=tuple(same_hosts),
            )
            answer = self.prompt(question)
            if answer == ACCEPT_ALL and old is None:
                self.accept_all_new = True
            if answer in (ACCEPT, ACCEPT_ALL, ACCEPT_MOVE):
                self._remember(name, key, replace=old is not None)
                if answer == ACCEPT_MOVE and old is None:
                    self._forget_key_elsewhere(key, keep=name)
                return
        if old is not None:
            raise HostKeyRejected(
                "HOST KEY CHANGED - connection refused. The device presented a different "
                "key than the one you accepted before (possible man-in-the-middle, or the "
                "device was reinstalled)."
            )
        raise HostKeyRejected("Host key was not accepted")

    def _hosts_with_key(self, key: paramiko.PKey) -> list[str]:
        raw = key.asbytes()
        return sorted(
            name
            for name in self._keys.keys()
            if any(k.asbytes() == raw for k in self._keys[name].values())
        )

    def _all_entries(self):
        return [
            (name, key_type, key)
            for name in self._keys.keys()
            for key_type, key in self._keys[name].items()
        ]

    def _set_entries(self, entries) -> None:
        # paramiko's per-host `del` only edits a temporary copy, so rebuild.
        fresh = paramiko.HostKeys()
        for name, key_type, key in entries:
            fresh.add(name, key_type, key)
        self._keys = fresh

    def _forget_key_elsewhere(self, key: paramiko.PKey, keep: str) -> None:
        raw = key.asbytes()
        self._set_entries(
            [e for e in self._all_entries() if e[0] == keep or e[2].asbytes() != raw]
        )
        self._save()

    def _remember(self, name: str, key: paramiko.PKey, replace: bool = False) -> None:
        if replace:
            self._set_entries([e for e in self._all_entries() if e[0] != name])
        self._keys.add(name, key.get_name(), key)
        self._save()


def _connect_or_reason(target: Target, timeout: float):
    """TCP connect. Returns (socket, None) on success, else (None, reason)."""
    try:
        return socket.create_connection((target.host, target.port), timeout=timeout), None
    except socket.timeout:
        return None, f"No response on port {target.port} (host down, or a firewall is dropping it)"
    except ConnectionRefusedError:
        return None, f"Port {target.port} is closed (connection refused) - SSH is not listening there"
    except socket.gaierror:
        return None, f"Cannot resolve the host name {target.host!r}"
    except OSError as e:
        return None, f"Cannot reach {target}: {e}"


def port_check(target: Target, timeout: float) -> Optional[str]:
    """Quick TCP connect test. Returns None if the port answers, else a reason.

    Used so dead hosts fail in a couple of seconds instead of waiting out the
    full SSH connect timeout. (ICMP ping is deliberately not used: firewalls
    often block it even when SSH works.)
    """
    sock, reason = _connect_or_reason(target, timeout)
    if sock is not None:
        sock.close()
    return reason


def _open_transport(
    target: Target, username: str, password: str, timeout: float, verifier=None, sock=None
):
    if sock is None:
        sock = socket.create_connection((target.host, target.port), timeout=timeout)
    transport = paramiko.Transport(sock)
    try:
        transport.start_client(timeout=timeout)
        if verifier is not None:
            # Before any credentials are sent.
            verifier.verify(target, transport.get_remote_server_key())
        try:
            transport.auth_password(username, password)
        except paramiko.BadAuthenticationType as e:
            if "keyboard-interactive" not in e.allowed_types:
                raise
            # Many switches/routers only offer keyboard-interactive.
            transport.auth_interactive(
                username, lambda title, instr, prompts: [password] * len(prompts)
            )
        if not transport.is_authenticated():
            raise paramiko.AuthenticationException("Authentication failed")
    except BaseException:
        transport.close()
        raise
    return transport


def split_commands(text: str, separate: bool = True) -> list[str]:
    """Turn the command box text into the list of commands to run.

    separate=True: one command per line; blank lines and lines starting with
    '#' are ignored. separate=False: the whole text is one command/script.
    """
    if not separate:
        text = text.strip()
        return [text] if text else []
    lines = (line.strip() for line in text.splitlines())
    return [line for line in lines if line and not line.startswith("#")]


# One-click command templates for APC rack PDUs (NMC CLI). "<n>" is replaced
# by an outlet number (or "all") chosen in the GUI.
APC_PRESETS = [
    ("Outlet status (all)", "olStatus all"),
    ("Outlet status (one)", "olStatus <n>"),
    ("Device power reading", "devReading power"),
    ("Device energy reading", "devReading energy"),
    ("Phase reading (all)", "phReading all"),
    ("About / firmware", "about"),
    ("Active alarm count", "alarmcount"),
    ("Event log", "eventlog"),
    ("Date / time", "date"),
    ("Outlet ON", "olOn <n>"),
    ("Outlet OFF", "olOff <n>"),
    ("Outlet REBOOT", "olReboot <n>"),
    ("Reboot management card", "reboot"),
]

# First words that change power or device state; running them asks for an
# explicit confirmation.
DISRUPTIVE_COMMANDS = {
    "olon", "oloff", "olreboot", "oldlyon", "oldlyoff", "oldlyreboot",
    "reboot", "resettodef", "format", "shutdown", "poweroff", "halt", "reload", "restart",
}


def disruptive_commands(commands: list[str]) -> list[str]:
    """The subset of `commands` that switch power or reboot/reset a device."""
    found = []
    for command in commands:
        words = command.split()
        if words and words[0].lower() in DISRUPTIVE_COMMANDS:
            found.append(command)
    return found


def _read_exec(chan, command_timeout: float, cancel: threading.Event):
    out, err = bytearray(), bytearray()
    deadline = time.monotonic() + command_timeout
    timed_out = False
    while True:
        got = False
        while chan.recv_ready():
            out += chan.recv(65536)
            got = True
        while chan.recv_stderr_ready():
            err += chan.recv_stderr(65536)
            got = True
        if got:
            continue
        if chan.exit_status_ready() or chan.closed:
            # one last drain: data can still be in flight right after exit
            time.sleep(0.05)
            if not (chan.recv_ready() or chan.recv_stderr_ready()):
                break
            continue
        if cancel.is_set() or time.monotonic() > deadline:
            timed_out = True
            break
        time.sleep(0.05)
    return out, err, timed_out


def _read_shell(chan, commands: list[str], command_timeout: float, cancel: threading.Event):
    """Interactive shell mode, for devices that reject `exec` requests.

    All commands are typed into the same session one after another (so
    `enable`, `terminal length 0`, `show ...` work as on a console). The
    session's own echo shows which output belongs to which command.
    """
    chan.get_pty(width=250, height=50)
    chan.invoke_shell()
    out = bytearray()
    state = {"deadline": time.monotonic() + command_timeout}

    def drain(idle: float) -> bool:
        """Read until the channel is quiet for `idle` seconds."""
        last = time.monotonic()
        while time.monotonic() < state["deadline"] and not cancel.is_set():
            if chan.recv_ready():
                out.extend(chan.recv(65536))
                last = time.monotonic()
            elif chan.closed or chan.exit_status_ready():
                break
            elif time.monotonic() - last > idle:
                return True
            else:
                time.sleep(0.05)
        return not (cancel.is_set() or time.monotonic() >= state["deadline"])

    drain(1.0)  # banner / prompt
    out.clear()
    finished = True
    for command in commands:
        state["deadline"] = time.monotonic() + command_timeout  # per command
        chan.send(command.rstrip("\n") + "\n")
        if not drain(2.0):
            finished = False
            break
    try:
        chan.send("exit\n")
    except Exception:
        pass
    return out, bytearray(), not finished


def _run_exec_commands(
    transport, commands, connect_timeout, command_timeout, cancel, stop_on_error, result
) -> bool:
    """Run commands one by one over `exec`, filling result. Returns timed_out."""
    multi = len(commands) > 1
    out_parts, err_parts = [], []
    first_failure: Optional[int] = None
    timed_out = False
    skipped = False
    for command in commands:
        if skipped or cancel.is_set():
            if multi:
                out_parts.append(f"$ {command}\n(skipped - an earlier command failed)")
            continue
        try:
            chan = transport.open_session(timeout=connect_timeout)
            chan.exec_command(command)
        except (paramiko.SSHException, EOFError, OSError) as e:
            # Some devices (APC PDUs) drop the connection or refuse further
            # sessions after the first exec; keep what we already collected.
            if not out_parts:
                raise
            out_parts.append(
                f"$ {command}\n(could not run: {e} - the device may accept only one "
                "command per connection; try 'Interactive shell mode')"
            )
            if first_failure is None:
                first_failure = 1
            skipped = True
            continue
        out, err, cmd_timed_out = _read_exec(chan, command_timeout, cancel)
        exit_status = None if cmd_timed_out else chan.recv_exit_status()
        if exit_status == -1:  # device never reported one (e.g. APC NMC CLI)
            exit_status = None
        out_text = out.decode("utf-8", errors="replace").rstrip("\n")
        err_text = err.decode("utf-8", errors="replace").rstrip("\n")
        if not multi:
            result.output, result.error = out_text, err_text
        else:
            tag = "" if exit_status in (0, None) else f"   [exit {exit_status}]"
            out_parts.append(f"$ {command}{tag}" + (f"\n{out_text}" if out_text else ""))
            if err_text:
                err_parts.append(f"$ {command}\n{err_text}")
        if cmd_timed_out:
            timed_out = True
            skipped = True
            if multi:
                out_parts[-1] += "\n(timed out)"
            continue
        if exit_status not in (0, None):
            if first_failure is None:
                first_failure = exit_status
            if stop_on_error:
                skipped = True
        elif not multi:
            result.exit_status = exit_status
    if multi:
        result.output = "\n\n".join(out_parts)
        result.error = "\n\n".join(err_parts)
        result.exit_status = first_failure if first_failure is not None else (None if timed_out else 0)
    elif first_failure is not None:
        result.exit_status = first_failure
    return timed_out


def run_command(
    target: Target,
    username: str,
    password: str,
    command: str,
    connect_timeout: float = 10.0,
    command_timeout: float = 60.0,
    use_shell: bool = False,
    cancel: Optional[threading.Event] = None,
    verifier: Optional[HostKeyVerifier] = None,
    dry_run: bool = False,
    precheck_timeout: Optional[float] = None,
    separate_commands: bool = True,
    stop_on_error: bool = True,
) -> Result:
    """Run `command` on one host.

    With separate_commands=True every non-blank, non-# line is its own command
    (own exit status, output under a "$ command" header; if one fails the
    rest are skipped when stop_on_error). The command timeout applies to each
    command separately.
    """
    cancel = cancel or threading.Event()
    result = Result(target)
    started = time.monotonic()
    transport = None
    try:
        if cancel.is_set():
            result.status = CANCELLED
            return result
        sock = None
        if precheck_timeout:
            # The checked connection is reused for the SSH session, so devices
            # don't see an extra, immediately dropped connection.
            sock, reason = _connect_or_reason(target, precheck_timeout)
            if sock is None:
                result.status, result.error = UNREACHABLE, reason
                return result
        transport = _open_transport(target, username, password, connect_timeout, verifier, sock)
        commands = split_commands(command, separate_commands)
        if dry_run:  # connected, host key trusted and logged in - stop here
            result.status = OK
            n = len(commands)
            result.output = (
                "Login OK (dry run) - "
                + (f"{n} command{'s' if n != 1 else ''} " if n else "the command ")
                + ("were" if n != 1 and n else "was")
                + " NOT executed."
            )
            return result
        if not commands:
            result.status, result.error = ERROR, "No command to run"
            return result
        if use_shell:
            chan = transport.open_session(timeout=connect_timeout)
            out, err, timed_out = _read_shell(chan, commands, command_timeout, cancel)
            result.output = out.decode("utf-8", errors="replace")
            result.error = err.decode("utf-8", errors="replace")
        else:
            timed_out = _run_exec_commands(
                transport, commands, connect_timeout, command_timeout, cancel, stop_on_error, result
            )
        if cancel.is_set():
            result.status = CANCELLED
        elif timed_out:
            result.status = TIMEOUT
            result.error = (result.error + "\nCommand timed out").strip()
        elif result.exit_status not in (None, 0):
            result.status = FAILED
        else:
            result.status = OK
    except HostKeyRejected as e:
        result.status, result.error = ERROR, str(e)
    except paramiko.AuthenticationException:
        result.status, result.error = ERROR, "Authentication failed (wrong username/password?)"
    except socket.timeout:
        result.status, result.error = TIMEOUT, "Connection timed out"
    except (OSError, paramiko.SSHException, EOFError) as e:
        result.status, result.error = ERROR, f"{type(e).__name__}: {e}"
        if "Incompatible ssh peer" in str(e):
            result.error += (
                " - the device only offers algorithms this paramiko version dropped"
                " (e.g. ssh-rsa host key, removed in paramiko 5); use paramiko<5"
            )
    finally:
        if transport is not None:
            transport.close()
        result.seconds = time.monotonic() - started
    return result


def run_all(
    targets: list[Target],
    username: str,
    password: str,
    command: str,
    connect_timeout: float = 10.0,
    command_timeout: float = 60.0,
    use_shell: bool = False,
    workers: int = 10,
    cancel: Optional[threading.Event] = None,
    on_result: Optional[Callable[[Result], None]] = None,
    verifier: Optional[HostKeyVerifier] = None,
    dry_run: bool = False,
    precheck_timeout: Optional[float] = None,
    separate_commands: bool = True,
    stop_on_error: bool = True,
) -> list[Result]:
    """Run `command` on every target in parallel; results keep target order."""
    cancel = cancel or threading.Event()
    results: list[Optional[Result]] = [None] * len(targets)

    def work(index: int):
        r = run_command(
            targets[index], username, password, command,
            connect_timeout, command_timeout, use_shell, cancel, verifier,
            dry_run, precheck_timeout, separate_commands, stop_on_error,
        )
        results[index] = r
        if on_result:
            on_result(r)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        list(pool.map(work, range(len(targets))))
    return [r for r in results if r is not None]
