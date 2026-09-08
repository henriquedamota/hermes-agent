"""A slow fenced delivery must not revoke its own execution heartbeat.

Regression from an Atlas run whose model completed and whose message was
delivered, but was then classified as interrupted by shutdown. No network or
Telegram delivery is performed here; the actual job store and locks are used.
"""
import threading

from cron import jobs


def test_owned_heartbeat_continues_while_another_thread_holds_delivery_fence(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(jobs, "_JOBS_LOCK_TIMEOUT_SECONDS", 0.05)
    job = jobs.create_job(prompt="isolated contract test", schedule="every 5m", name="delivery")
    assert jobs.claim_job_for_fire(job["id"])
    owner = jobs.get_job(job["id"])["fire_claim"]["by"]
    result = []
    finished = threading.Event()

    def renew():
        try:
            result.append(jobs.heartbeat_fire_claim(job["id"], expected_owner=owner))
        finally:
            finished.set()

    with jobs.fire_claim_fence(job["id"], expected_owner=owner) as owns_delivery:
        assert owns_delivery
        thread = threading.Thread(target=renew)
        thread.start()
        try:
            assert finished.wait(2), "heartbeat waited behind its own delivery"
            assert result == [True], "delivery fence revoked a still-owned claim"
        finally:
            thread.join(timeout=2)
    assert jobs.get_job(job["id"])["fire_claim"]["by"] == owner


def test_wrong_owner_cannot_renew_even_while_delivery_is_fenced(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(jobs, "_JOBS_LOCK_TIMEOUT_SECONDS", 0.05)
    job = jobs.create_job(prompt="isolated contract test", schedule="every 5m", name="stale")
    assert jobs.claim_job_for_fire(job["id"])
    claim = jobs.get_job(job["id"])["fire_claim"]
    result = []
    with jobs.fire_claim_fence(job["id"], expected_owner=claim["by"]) as owns_delivery:
        assert owns_delivery
        thread = threading.Thread(target=lambda: result.append(
            jobs.heartbeat_fire_claim(job["id"], expected_owner="stale-owner")))
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert result == [False]
    assert jobs.get_job(job["id"])["fire_claim"] == claim
