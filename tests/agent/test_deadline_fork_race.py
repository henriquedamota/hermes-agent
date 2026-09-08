"""Exercise process birth during a real Linux descendant snapshot."""
import signal
import os
import subprocess
import sys
import time

import psutil
import pytest

from agent.deadline import kill_process_tree


@pytest.mark.linux_only
@pytest.mark.live_system_guard_bypass
def test_process_born_during_snapshot_cannot_escape_hard_cancellation(tmp_path, monkeypatch):
    ready = tmp_path / 'ready'
    trigger = tmp_path / 'fork'
    escaped = tmp_path / 'escaped'
    child_code = (
        'import subprocess,sys,time; from pathlib import Path\n'
        f'Path({str(ready)!r}).write_text("ready")\n'
        f'while not Path({str(trigger)!r}).exists(): time.sleep(.01)\n'
        'p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"],start_new_session=True)\n'
        f'Path({str(escaped)!r}).write_text(str(p.pid))\n'
        'time.sleep(60)\n'
    )
    parent_code = (
        'import subprocess,sys,time\n'
        f'subprocess.Popen([sys.executable,"-c",{child_code!r}],start_new_session=True)\n'
        'time.sleep(60)\n'
    )
    proc = subprocess.Popen([sys.executable, '-c', parent_code], start_new_session=True)
    owned = [psutil.Process(proc.pid)]
    original_children = psutil.Process.children
    triggered = False

    def snapshot_with_concurrent_birth(process, *args, **kwargs):
        nonlocal triggered
        snapshot = original_children(process, *args, **kwargs)
        if process.pid == proc.pid and not triggered:
            triggered = True
            owned.extend(snapshot)
            trigger.write_text('fork now')
            deadline = time.monotonic() + 5
            while not escaped.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            assert escaped.exists(), 'controlled descendant did not fork'
            owned.append(psutil.Process(int(escaped.read_text())))
        return snapshot

    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.exists(), 'process tree fixture did not become ready'
        monkeypatch.setattr(psutil.Process, 'children', snapshot_with_concurrent_birth)
        assert kill_process_tree(proc.pid)
        assert triggered and len(owned) >= 3
        proc.wait(timeout=5)
        deadline = time.monotonic() + 3
        survivor = owned[-1]
        while survivor.is_running() and time.monotonic() < deadline:
            try:
                if survivor.status() == psutil.STATUS_ZOMBIE:
                    break
            except psutil.NoSuchProcess:
                break
            time.sleep(.01)
        try:
            assert not survivor.is_running() or survivor.status() == psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            pass
    finally:
        # Only identities created by this fixture; never production processes.
        for process in reversed(owned):
            try:
                process.send_signal(signal.SIGKILL)
            except psutil.Error:
                pass
        proc.wait(timeout=5)


@pytest.mark.linux_only
@pytest.mark.live_system_guard_bypass
@pytest.mark.parametrize('already_stopped', [False, True])
def test_failed_hard_signal_preserves_the_previous_process_state(monkeypatch, already_stopped):
    proc = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(60)'], start_new_session=True)
    identity = psutil.Process(proc.pid)
    original_signal = psutil.Process.send_signal
    original_group = os.killpg

    def refused_signal(process, sig):
        if process.pid == proc.pid and sig == signal.SIGKILL:
            raise psutil.AccessDenied(proc.pid)
        return original_signal(process, sig)

    def refused_group(pgid, sig):
        if pgid == proc.pid and sig == signal.SIGKILL:
            raise PermissionError('isolated signal refusal')
        return original_group(pgid, sig)

    try:
        if already_stopped:
            identity.suspend()
            deadline = time.monotonic() + 3
            while identity.status() != psutil.STATUS_STOPPED and time.monotonic() < deadline:
                time.sleep(.01)
            assert identity.status() == psutil.STATUS_STOPPED
        with monkeypatch.context() as patch:
            patch.setattr(psutil.Process, 'send_signal', refused_signal)
            patch.setattr(os, 'killpg', refused_group)
            assert not kill_process_tree(proc.pid)
        assert identity.is_running()
        deadline = time.monotonic() + 3
        while (identity.status() == psutil.STATUS_STOPPED) != already_stopped and time.monotonic() < deadline:
            time.sleep(.01)
        assert (identity.status() == psutil.STATUS_STOPPED) == already_stopped
    finally:
        identity.kill()
        proc.wait(timeout=5)
