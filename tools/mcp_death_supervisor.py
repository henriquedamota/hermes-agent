#!/usr/bin/env python3
"""One parent-death supervisor per Hermes process, shared by all stdio MCP servers.

Why this exists
---------------
When Hermes dies without running its cleanup path (SIGKILL, OOM killer, a hard
crash), stdio MCP servers it spawned are reparented to init and keep running
forever.  macOS has no ``PR_SET_PDEATHSIG``, so something has to outlive Hermes
and reap them.

This module is deliberately standard-library-only and must not import anything
from ``tools/``: it runs after Hermes may already be dead, and pulling in
``mcp_tool`` would drag the whole agent with it. The TERM -> grace -> KILL
``killpg`` sweep in ``_reap`` therefore duplicates similar sweeps elsewhere in
the tree on purpose.

The predecessor (``mcp_stdio_watchdog.py``) solved this with one CPython
*per MCP server*, wrapping each server command and polling ``getppid()`` every
two seconds.  That costs ~10 MB of resident memory per server and detects death
up to one poll interval late.  This module replaces the whole fleet of pollers
with a single supervisor per Hermes process:

* **Death detection is a blocking read on a pipe.**  Hermes holds the only write
  end.  When Hermes dies -- by any means, including SIGKILL -- the write end
  closes and the read returns EOF.  Exact, instant, and free.
* **Servers are spawned unwrapped.**  The MCP SDK already spawns stdio children
  with ``start_new_session=True``, so each one is its own process-group leader
  and ``killpg`` still reaches its descendants.  Removing the wrapper also
  removes the signal-forwarding layer the wrapper needed to avoid inverting the
  bug it fixed.

Protocol (line-based, on stdin)
-------------------------------
    register <pgid>\n     start reaping this process group on parent death
    unregister <pgid>\n   stop reaping it (its server shut down cleanly)

On EOF the supervisor SIGTERMs every still-registered process group, waits a
short grace period, SIGKILLs the survivors, and exits.  A registered group that
Hermes never unregistered *is* the orphan set, so a clean Hermes shutdown --
which unregisters as it tears each server down -- ends with nothing to kill.

Unparseable lines are ignored rather than fatal: a corrupted byte on the control
pipe must not cost us the reaping guarantee for every other server.

Residual risk: process-group reuse
----------------------------------
We reap by pgid, so a registration is only as meaningful as the group's
identity.  A group we deliberately keep registered -- an orphan that teardown
failed to kill, such as the ``node`` ``mcp-remote`` leaves behind -- can
eventually exit on its own, after which the kernel is free to hand that pgid to
an unrelated process owned by the same user.  If Hermes then dies ungracefully
while the registration is still stale, we would signal a stranger.
``_is_safe_target`` cannot catch this: the value is stale, not invalid.

Two things narrow the window.  Hermes prunes registrations whose group has no
members left (``_prune_dead_supervised_pgids``) on every registration change,
and the orphan sweep unregisters whatever it reaps.  Neither closes it -- a
group can die and its pgid be recycled between two probes -- so the exposure is
real but bounded to that gap, and requires an ungraceful death inside it.

Closing it completely means proving group identity at reap time, e.g. stamping
MCP children with a boot-unique env marker and checking that some member still
carries it before signalling.  That was judged not worth putting a ``ps`` parse
into the one process whose job is to stay simple enough to always work; it is
the obvious next step if this class of bug ever actually bites.  Note the same
exposure already exists in Hermes's own killpg-based orphan cleanup, which this
module did not introduce (see upstream issue #88350).
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import struct
import sys
import threading
import time

# Matches the grace period the per-server watchdog used before it escalated.
_TERM_GRACE_S = 3.0
# How often we re-check for survivors during that grace period.
_REAP_POLL_S = 0.1
# A command is "unregister <pgid>" -- around 20 characters. The cap only has to
# be generous enough for a legitimate line; see _serve for why it exists.
_MAX_LINE_CHARS = 256
_MAX_STATUS_BYTES = 64 * 1024


def process_identity(pid: int) -> dict:
    """Linux process identity; an unreadable/reused process is never proof."""
    fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
    return {"pid": pid, "ppid": int(fields[1]), "pgid": int(fields[2]),
            "start_ticks": int(fields[19]),
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()}


def _linux_uid() -> int:
    getuid = getattr(os, "getuid", None)
    if sys.platform != "linux" or getuid is None:
        raise OSError("coverage peer authentication requires Linux UID support")
    return getuid()


def _status_address(pid: int) -> str:
    # Linux abstract sockets disappear with their owner. No stale file, path
    # truncation or cross-profile filesystem state; peer credentials are checked.
    return f"\0hermes-mcp-supervisor-{_linux_uid()}-{pid}"


class CoverageStatus:
    """Read-only, same-UID live query of registrations received by this child.

    Observation runs independently of stdin: an idle/malicious status client
    cannot delay the parent's EOF or the existing reaper. No commands accepted
    here can register groups, change policy or send signals.
    """

    def __init__(self):
        self.identity = process_identity(os.getpid())
        self.parent = process_identity(os.getppid())
        self.registrations: dict[int, dict | None] = {}
        self.lock = threading.Lock()
        self.closed = threading.Event()
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            self.listener.bind(_status_address(os.getpid()))
            self.listener.listen(4)
            self.listener.settimeout(0.25)
        except BaseException:
            self.listener.close()
            raise
        self.thread = threading.Thread(target=self._serve_queries, daemon=True)
        try:
            self.thread.start()
        except RuntimeError:
            self.listener.close()
            raise

    def register(self, pgid: int) -> None:
        try:
            identity = process_identity(pgid)
        except (OSError, ValueError, IndexError, RuntimeError):
            identity = None
        with self.lock:
            # A duplicate register must not certify a recycled PID as the old
            # group. Only an explicit unregister/register creates a new entry.
            self.registrations.setdefault(pgid, identity)

    def unregister(self, pgid: int) -> None:
        with self.lock:
            self.registrations.pop(pgid, None)

    def _serve_queries(self) -> None:
        while not self.closed.is_set():
            try:
                connection, _ = self.listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with connection:
                try:
                    connection.settimeout(0.25)
                    _, uid, _ = struct.unpack("3i", connection.getsockopt(
                        socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
                    if uid != _linux_uid():
                        continue
                    nonce = connection.recv(128).decode("ascii")
                    if len(nonce) != 32 or any(c not in "0123456789abcdef" for c in nonce):
                        continue
                    with self.lock:
                        registrations = dict(self.registrations)
                    body = json.dumps({"schema": 1, "nonce": nonce,
                        "supervisor": self.identity, "parent": self.parent,
                        "registrations": registrations}).encode()
                    if len(body) <= _MAX_STATUS_BYTES:
                        connection.sendall(body)
                except (OSError, UnicodeError, ValueError):
                    continue

    def close(self) -> None:
        self.closed.set()
        try:
            self.listener.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.listener.close()
        self.thread.join(timeout=0.5)


def query_coverage(pid: int, timeout: float = 1.0) -> dict:
    """Challenge the live child, verifying peer credentials and process identity.

    Consumers must also compare each returned registration with the current
    target process; this function authenticates the reply, not desired coverage.
    """
    before = process_identity(pid)
    nonce = secrets.token_hex(16)
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as client:
        client.settimeout(timeout)
        client.connect(_status_address(pid))
        peer_pid, uid, _ = struct.unpack("3i", client.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
        if peer_pid != pid or uid != _linux_uid():
            raise ValueError("supervisor peer identity mismatch")
        client.sendall(nonce.encode())
        raw = client.recv(_MAX_STATUS_BYTES + 1)
    if len(raw) > _MAX_STATUS_BYTES:
        raise ValueError("oversized supervisor reply")
    reply = json.loads(raw)
    if (reply.get("schema") != 1 or reply.get("nonce") != nonce
            or reply.get("supervisor") != before or process_identity(pid) != before):
        raise ValueError("stale or invalid supervisor reply")
    parent = reply.get("parent") or {}
    if (parent.get("pid") != before["ppid"]
            or process_identity(before["ppid"]) != parent):
        raise ValueError("supervisor owner identity mismatch")
    return reply


def _is_safe_target(pgid: int, *, own_pgid: int, parent_pgid: int) -> bool:
    """Return True if ``pgid`` is a process group we may signal.

    Defensive only -- Hermes already filters non-MCP children before it
    registers anything (see ``_filter_mcp_children`` in ``tools/mcp_tool.py``).
    But this process signals whole process *groups*, so a bad value here is
    unusually expensive: ``killpg(0, ...)`` signals our own group, and pgid 1
    is init.  A caller bug should cost us one unreaped server, never the
    Hermes process tree or the session.
    """
    if pgid <= 1:
        return False
    if pgid == own_pgid or pgid == parent_pgid:
        return False
    return True


def _reap(pgids: set[int]) -> None:
    """SIGTERM every group, then SIGKILL whatever is still alive.

    Every process-group call below is POSIX-only by construction: this whole
    module only ever runs as a child of ``_update_death_supervisor``, which
    returns early unless ``os.name == "posix"``, so the supervisor is never
    spawned on Windows in the first place.
    """
    if not pgids:
        return

    alive = set()
    for pgid in pgids:
        try:
            os.killpg(pgid, signal.SIGTERM)  # windows-footgun: ok — POSIX-only process
            alive.add(pgid)
        except (ProcessLookupError, PermissionError, OSError):
            # Already gone, or not ours to signal. Either way, nothing to reap.
            pass

    deadline = time.monotonic() + _TERM_GRACE_S
    while alive and time.monotonic() < deadline:
        time.sleep(_REAP_POLL_S)
        for pgid in list(alive):
            try:
                # Signal 0 probes liveness: succeeds iff some member survives.
                os.killpg(pgid, 0)  # windows-footgun: ok — POSIX-only process
            except (ProcessLookupError, PermissionError, OSError):
                alive.discard(pgid)

    for pgid in alive:
        try:
            os.killpg(pgid, signal.SIGKILL)  # windows-footgun: ok — POSIX-only
        except (ProcessLookupError, PermissionError, OSError):
            pass


def _serve(stream, *, own_pgid: int, parent_pgid: int, coverage=None) -> set[int]:
    """Read control lines until EOF; return the groups still registered.

    Reads are length-capped rather than newline-terminated. Iterating the
    stream instead lets a writer that never sends a newline grow this process
    without bound -- feeding it ``/dev/zero`` reached 15 GB before it was
    stopped. Nothing in Hermes can produce that today, but this process is the
    last line of defense against leaked servers, so it must not be the thing
    that dies under memory pressure. A line truncated by the cap fails to parse
    and is skipped; the remainder resyncs at the next newline.
    """
    registered: set[int] = set()
    while True:
        line = stream.readline(_MAX_LINE_CHARS)
        if not line:
            break  # EOF: the parent is gone.
        if not line.endswith("\n"):
            # Truncated by the cap, or an unterminated tail at EOF. Either way
            # it is not a command we are willing to act on.
            continue
        parts = line.split()
        if len(parts) != 2:
            continue
        verb, raw = parts
        try:
            pgid = int(raw)
        except ValueError:
            continue
        if verb == "register":
            if _is_safe_target(pgid, own_pgid=own_pgid, parent_pgid=parent_pgid):
                registered.add(pgid)
                if coverage is not None:
                    coverage.register(pgid)
        elif verb == "unregister":
            registered.discard(pgid)
            if coverage is not None:
                coverage.unregister(pgid)
    return registered


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Reap registered process groups when the parent dies."
    )
    parser.add_argument(
        "--parent-pgid",
        type=int,
        help="Process group of the spawning Hermes process; never signalled.",
    )
    parser.add_argument("--query", type=int, metavar="PID",
                        help="Read live Linux supervisor coverage; never changes registrations.")
    args = parser.parse_args(argv)
    if args.query is not None:
        try:
            print(json.dumps(query_coverage(args.query)))
            return 0
        except (OSError, ValueError, KeyError, IndexError, AttributeError) as exc:
            print(json.dumps({"error": type(exc).__name__, "coverage": "unverifiable"}))
            return 1
    if args.parent_pgid is None:
        parser.error("--parent-pgid is required unless --query is used")

    # The parent may be torn down with killpg on its own group. We are spawned
    # with start_new_session=True precisely so that sweep cannot take us with
    # it before we have reaped -- assert that here rather than trust the caller.
    own_pgid = os.getpgid(0)
    if own_pgid == args.parent_pgid:
        print(
            "mcp_death_supervisor: refusing to run inside the parent's process "
            "group (a killpg of the parent would kill us before we can reap)",
            file=sys.stderr,
        )
        return 2

    # A dying parent's SIGINT/SIGHUP must not preempt the reap; the pipe's EOF
    # is our only shutdown signal. SIGHUP is POSIX-only, which is fine here --
    # this process is never spawned on Windows (see _reap's docstring).
    for sig in (signal.SIGINT, signal.SIGHUP):  # windows-footgun: ok — POSIX-only process
        try:
            signal.signal(sig, signal.SIG_IGN)
        except (ValueError, OSError):
            pass

    coverage = None
    if sys.platform == "linux":
        try:
            coverage = CoverageStatus()
        except (OSError, ValueError, IndexError, RuntimeError):
            # Failure of observation must not remove the reaping safety net.
            # External probes fail closed when the query cannot be answered.
            print("mcp_death_supervisor: live coverage unavailable", file=sys.stderr)
    try:
        registered = _serve(sys.stdin, own_pgid=own_pgid,
                            parent_pgid=args.parent_pgid, coverage=coverage)
        _reap(registered)
    finally:
        if coverage is not None:
            coverage.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
