"""Live Linux coverage is a nonce reply from the child, never a PID count."""
import contextlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import pytest

from tools import mcp_death_supervisor as supervisor

pytestmark = pytest.mark.linux_only


def wait_for(predicate):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            result = predicate()
            if result:
                return result
        except (OSError, ValueError):
            pass
        time.sleep(0.02)
    raise AssertionError("coverage condition was not reached")


@pytest.fixture
def live_supervisor():
    child = subprocess.Popen(
        [sys.executable, supervisor.__file__, "--parent-pgid", str(os.getpgid(0))],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True, text=True,
    )
    wait_for(lambda: supervisor.query_coverage(child.pid))
    try:
        yield child
    finally:
        child.stdin.close()
        child.wait(timeout=10)
        child.stdout.close()
        child.stderr.close()


@pytest.fixture
def victim():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"],
                             start_new_session=True)
    try:
        yield child
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)


def test_live_registration_removal_identity_and_nonce(live_supervisor, victim):
    child = live_supervisor
    identity = supervisor.process_identity(victim.pid)
    child.stdin.write(f"register {victim.pid}\n")
    child.stdin.flush()
    reply = wait_for(lambda: (r if str(victim.pid) in r["registrations"] else None)
                     if (r := supervisor.query_coverage(child.pid)) else None)
    assert reply["registrations"][str(victim.pid)] == identity
    assert reply["supervisor"] == supervisor.process_identity(child.pid)
    assert reply["parent"] == supervisor.process_identity(os.getpid())
    assert supervisor.query_coverage(child.pid)["nonce"] != reply["nonce"]
    child.stdin.write(f"unregister {victim.pid}\n")
    child.stdin.flush()
    wait_for(lambda: str(victim.pid) not in supervisor.query_coverage(child.pid)["registrations"])
    assert victim.poll() is None


def test_query_cannot_register_or_unregister(live_supervisor, victim):
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as client:
        client.settimeout(2)
        client.connect(supervisor._status_address(live_supervisor.pid))
        client.sendall(f"register {victim.pid}".encode())
        assert client.recv(100) == b""
    assert supervisor.query_coverage(live_supervisor.pid)["registrations"] == {}


def test_unavailable_endpoint_never_returns_coverage(victim):
    with pytest.raises(OSError):
        supervisor.query_coverage(victim.pid)


def test_status_thread_failure_preserves_reaper(monkeypatch):
    from unittest.mock import Mock
    monkeypatch.setattr(supervisor, "CoverageStatus", Mock(side_effect=RuntimeError("thread unavailable")))
    monkeypatch.setattr(sys, "stdin", io.StringIO("register 987654321\n"))
    reap = Mock()
    monkeypatch.setattr(supervisor, "_reap", reap)
    assert supervisor.main(["--parent-pgid", str(os.getpgid(0) + 1)]) == 0
    reap.assert_called_once_with({987654321})


def test_same_uid_socket_with_wrong_owner_is_rejected(victim):
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as fake:
        fake.bind(supervisor._status_address(victim.pid))
        fake.listen(1)
        with pytest.raises(ValueError, match="peer identity"):
            supervisor.query_coverage(victim.pid)


@pytest.mark.parametrize("field", ["identity", "parent"])
def test_stale_identity_cannot_be_reported_as_live(field):
    status = supervisor.CoverageStatus()
    try:
        setattr(status, field, {**getattr(status, field), "start_ticks": -1})
        with pytest.raises(ValueError, match="identity|reply"):
            supervisor.query_coverage(os.getpid())
    finally:
        status.close()


def test_slow_observer_does_not_block_eof_reaping(live_supervisor, victim):
    child = live_supervisor
    child.stdin.write(f"register {victim.pid}\n")
    child.stdin.flush()
    wait_for(lambda: str(victim.pid) in supervisor.query_coverage(child.pid)["registrations"])
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as observer:
        observer.connect(supervisor._status_address(child.pid))
        child.stdin.close()
        victim.wait(timeout=10)
        child.wait(timeout=10)
    with pytest.raises(OSError):
        supervisor.query_coverage(child.pid)


def test_parent_death_keeps_reaper_and_query_contract(tmp_path):
    code = """
import json,os,subprocess,sys,time
victim = subprocess.Popen([sys.executable,'-c','import time; time.sleep(300)'],start_new_session=True)
supervisor = subprocess.Popen([sys.executable,sys.argv[1],'--parent-pgid',str(os.getpgid(0))],stdin=subprocess.PIPE,start_new_session=True,text=True)
supervisor.stdin.write(f'register {victim.pid}\\n'); supervisor.stdin.flush()
print(json.dumps({'victim':victim.pid,'supervisor':supervisor.pid}),flush=True)
time.sleep(300)
"""
    parent = subprocess.Popen([sys.executable, "-c", code, supervisor.__file__],
                              stdout=subprocess.PIPE, text=True, start_new_session=True)
    pids = json.loads(parent.stdout.readline())
    identities = {pid: supervisor.process_identity(pid) for pid in pids.values()}
    try:
        reply = wait_for(lambda: (r if str(pids["victim"]) in r["registrations"] else None)
                         if (r := supervisor.query_coverage(pids["supervisor"])) else None)
        assert reply["parent"]["pid"] == parent.pid
        parent.kill()
        parent.wait(timeout=10)
        def stopped(pid):
            try:
                return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z"
            except FileNotFoundError:
                return True
        wait_for(lambda: stopped(pids["victim"]) and stopped(pids["supervisor"]))
        with pytest.raises((OSError, ValueError)):
            supervisor.query_coverage(pids["supervisor"])
    finally:
        if parent.poll() is None:
            parent.kill()
        parent.wait(timeout=10)
        parent.stdout.close()
        for pid in pids.values():
            with contextlib.suppress(OSError):
                current = supervisor.process_identity(pid)
                if current["start_ticks"] == identities[pid]["start_ticks"]:
                    os.kill(pid, 9)
