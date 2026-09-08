"""A missing cross-process lock cannot authorize a jobs.json mutation."""
import threading

import pytest

from cron import jobs


@pytest.mark.skipif(jobs.fcntl is None, reason="POSIX flock")
def test_timeout_keeps_store_unchanged_and_later_acquisition_recovers(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(jobs, "_JOBS_LOCK_TIMEOUT_SECONDS", 0.05)
    job = jobs.create_job(prompt="isolated", schedule="every 5m", name="unchanged")
    store = jobs._current_cron_store().jobs_file
    before = store.read_bytes()
    held, release = threading.Event(), threading.Event()

    def hold():
        with jobs._jobs_lock_file().open("a+") as fd:
            jobs.fcntl.flock(fd, jobs.fcntl.LOCK_EX)
            held.set()
            release.wait(5)

    thread = threading.Thread(target=hold)
    thread.start()
    assert held.wait(2)
    try:
        with pytest.raises(RuntimeError, match="lock"):
            jobs.update_job(job["id"], {"name": "must-not-be-written"})
        assert store.read_bytes() == before
    finally:
        release.set()
        thread.join(timeout=2)
    assert not thread.is_alive()
    jobs.update_job(job["id"], {"name": "after-lock-recovery"})
    assert jobs.get_job(job["id"])["name"] == "after-lock-recovery"
