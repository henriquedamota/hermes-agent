"""A process return must neither heal a deferred failure nor lose its next chance."""
from datetime import datetime, timedelta, timezone
import json
from zoneinfo import ZoneInfo

import pytest
from cron import jobs
from hermes_cli.execution_result import build_result


def persist_wait(job, now, monkeypatch, *, minutes=5):
    """Use the same receipt, ledger and job completion path as a real producer."""
    from cron import executions, functional_results
    monkeypatch.setattr(jobs, '_hermes_now', lambda: now)
    entry = executions.create_execution(job['id'], source='isolated')
    result = build_result(subject_type='maintenance', job_id=job['id'],
        execution_id=entry['id'], outcome='deferred', exit_code=0,
        observed_at=now.isoformat(), reason={'code':'writer_busy'},
        continuation={'automatic':True, 'eligible_at':(now+timedelta(minutes=minutes)).astimezone(timezone.utc).isoformat()})
    path = functional_results.result_path(executions.get_hermes_home(), entry['id'])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result))
    jobs.mark_job_run(job['id'], True, functional_result=result)
    executions.finish_execution(entry['id'], success=True)
    return result


@pytest.mark.parametrize('zone', ['UTC', 'America/Sao_Paulo'])
@pytest.mark.parametrize('delay_minutes', [5, 180])
def test_daily_continuation_is_dispatched_off_cron_grid_after_reload(job, monkeypatch, zone, delay_minutes):
    now = datetime(2026, 9, 9, 5, 20, 20, tzinfo=ZoneInfo(zone))
    persist_wait(job, now, monkeypatch)
    monkeypatch.setattr(jobs, '_hermes_now', lambda: now+timedelta(minutes=delay_minutes))
    due = jobs.get_due_jobs()
    assert [row['id'] for row in due] == [job['id']]
    assert due[0]['last_dispatch']['scheduled_at'] == jobs.get_job(job['id'])['continuation']['eligible_at']


@pytest.mark.parametrize('lost_next', [None, '2026-09-10T12:00:00+00:00'])
def test_pending_receipt_repairs_next_opportunity_without_manual_rearm(job, monkeypatch, lost_next):
    now = datetime(2026, 9, 9, 5, 20, 20, tzinfo=timezone.utc)
    persist_wait(job, now, monkeypatch)
    records = jobs.load_jobs()
    records[0]['next_run_at'] = lost_next  # legacy scheduler rewrite / interrupted dispatch
    jobs.save_jobs(records)
    monkeypatch.setattr(jobs, '_hermes_now', lambda: now+timedelta(minutes=6))
    assert [row['id'] for row in jobs.get_due_jobs()] == [job['id']]


@pytest.mark.parametrize('successor_status', ['claimed', 'running', 'unknown', 'completed'])
def test_old_wait_cannot_replay_after_a_successor_attempt(job, monkeypatch, successor_status):
    from cron import executions
    now = datetime(2026, 9, 9, 5, 20, 20, tzinfo=timezone.utc)
    persist_wait(job, now, monkeypatch)
    jobs.advance_next_run(job['id'])
    monkeypatch.setattr(executions, '_hermes_now', lambda: now-timedelta(hours=1))
    newer = executions.create_execution(job['id'], source='isolated')
    if successor_status == 'running':
        executions.mark_execution_running(newer['id'])
    elif successor_status == 'unknown':
        monkeypatch.setattr(executions, '_owner_is_live', lambda *_: False)
        executions.recover_interrupted_executions()
    elif successor_status == 'completed':
        executions.finish_execution(newer['id'], success=True, delivery_outcome='failed')
    monkeypatch.setattr(jobs, '_hermes_now', lambda: now+timedelta(minutes=6))
    assert jobs.get_due_jobs() == []


def test_continuation_waits_for_absolute_time_and_respects_pause(job, monkeypatch):
    now = datetime(2026, 9, 9, 5, 20, 20, tzinfo=ZoneInfo('America/Sao_Paulo'))
    persist_wait(job, now, monkeypatch)
    monkeypatch.setattr(jobs, '_hermes_now', lambda: now+timedelta(minutes=4))
    assert jobs.get_due_jobs() == []
    jobs.pause_job(job['id'], 'isolated maintenance')
    monkeypatch.setattr(jobs, '_hermes_now', lambda: now+timedelta(minutes=6))
    assert jobs.get_due_jobs() == []
    jobs.resume_job(job['id'])
    assert [row['id'] for row in jobs.get_due_jobs()] == [job['id']]


def test_late_oneshot_continuation_keeps_dispatch_budget(job, monkeypatch):
    now = datetime(2026, 9, 9, 5, 20, 20, tzinfo=timezone.utc)
    monkeypatch.setattr(jobs, '_hermes_now', lambda: now)
    task = jobs.create_job('isolated one-shot', (now+timedelta(minutes=1)).isoformat(), deliver='local')
    assert jobs.claim_dispatch(task['id'])
    persist_wait(task, now, monkeypatch)
    monkeypatch.setattr(jobs, '_hermes_now', lambda: now+timedelta(hours=3))
    due = jobs.get_due_jobs()
    assert [row['id'] for row in due] == [task['id']]
    assert jobs.get_due_jobs() == []  # durable one-shot claim prevents a duplicate tick
    assert jobs.claim_dispatch(task['id'])
    assert jobs.get_job(task['id'])['repeat']['completed'] == 1


@pytest.mark.parametrize('invalid', ['invalid', '2026-09-09T05:25:20', '2026-09-09T05:21:20+00:00'])
def test_invalid_pending_timestamp_cannot_override_regular_schedule(job, monkeypatch, invalid):
    now = datetime(2026, 9, 9, 5, 20, 20, tzinfo=timezone.utc)
    persist_wait(job, now, monkeypatch)
    jobs.advance_next_run(job['id'])
    records = jobs.load_jobs()
    records[0]['continuation']['eligible_at'] = invalid
    jobs.save_jobs(records)
    monkeypatch.setattr(jobs, '_hermes_now', lambda: now+timedelta(minutes=6))
    assert jobs.get_due_jobs() == []


def test_pending_summary_without_ledger_receipt_cannot_override_schedule(job, monkeypatch):
    now = datetime(2026, 9, 9, 5, 20, 20, tzinfo=timezone.utc)
    monkeypatch.setattr(jobs, '_hermes_now', lambda: now)
    jobs.mark_job_run(job['id'], True, functional_result=receipt(job,
        observed_at=now.isoformat(), continuation={'automatic':True,
            'eligible_at':(now+timedelta(minutes=5)).isoformat()}))
    jobs.advance_next_run(job['id'])
    monkeypatch.setattr(jobs, '_hermes_now', lambda: now+timedelta(minutes=6))
    assert jobs.get_due_jobs() == []


def test_competing_fire_claims_cannot_dispatch_same_continuation(job, monkeypatch):
    now = datetime(2026, 9, 9, 5, 20, 20, tzinfo=timezone.utc)
    persist_wait(job, now, monkeypatch)
    monkeypatch.setattr(jobs, '_hermes_now', lambda: now+timedelta(minutes=6))
    assert [row['id'] for row in jobs.get_due_jobs()] == [job['id']]
    first = jobs.claim_job_for_fire(job['id'], return_job=True)
    assert first and first['fire_claim']['by']
    monkeypatch.setattr(jobs, '_machine_id', lambda: 'competing-process')
    assert jobs.claim_job_for_fire(job['id'], return_job=True) is False


@pytest.fixture
def job(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(jobs, 'JOBS_FILE', tmp_path/'jobs.json')
    monkeypatch.setattr(jobs, 'CRON_DIR', tmp_path)
    return jobs.create_job('isolated task', '0 12 * * *', deliver='local')


def receipt(job, outcome='deferred', **fields):
    return build_result(subject_type='maintenance', job_id=job['id'], execution_id='isolated',
        outcome=outcome, reason={'code':'writer_busy'}, **fields)


def test_deferred_preserves_failure_streak_and_does_not_consume_completion(job):
    jobs.mark_job_run(job['id'], False, 'original failure')
    before = jobs.get_job(job['id'])
    jobs.mark_job_run(job['id'], True, functional_result=receipt(job))
    after = jobs.get_job(job['id'])
    assert after['failure_streak'] == before['failure_streak'] == 1
    assert after['last_status'] == 'deferred'
    assert after['repeat']['completed'] == before['repeat']['completed']
    assert after['last_failure']['detail'] == 'original failure'


def test_explicit_continuation_survives_store_reload_before_daily_schedule(job):
    now = datetime.now(timezone.utc)
    eligible = now + timedelta(minutes=2)
    jobs.mark_job_run(job['id'], True, functional_result=receipt(job,
        continuation={'automatic':True, 'eligible_at':eligible.isoformat(), 'condition':'writer exclusion available'}))
    reloaded = jobs.get_job(job['id'])
    assert datetime.fromisoformat(reloaded['next_run_at']) == eligible
    assert reloaded['continuation']['execution_id'] == 'isolated'
    assert datetime.fromisoformat(reloaded['continuation']['scheduled_next_at']) > eligible


def test_completed_work_with_delivery_failure_never_schedules_continuation(job):
    now = datetime.now(timezone.utc)
    jobs.mark_job_run(job['id'], True, delivery_error='transport unavailable',
        functional_result=receipt(job, 'completed', exit_code=0, evidence=[{'path':'durable/receipt'}],
            continuation={'automatic':True,'eligible_at':(now+timedelta(seconds=1)).isoformat()}))
    reloaded = jobs.get_job(job['id'])
    assert 'continuation' not in reloaded
    assert reloaded['last_status'] == 'completed'
    assert reloaded['last_delivery_error'] == 'transport unavailable'


def test_foreign_receipt_cannot_change_another_job(job):
    value = receipt(job)
    value['job_id'] = 'foreign'
    with pytest.raises(ValueError, match='job'):
        jobs.mark_job_run(job['id'], True, functional_result=value)
    assert jobs.get_job(job['id'])['last_run_at'] is None


def test_past_eligibility_is_rate_limited_and_failure_is_not_auto_replayed(job):
    now = datetime.now(timezone.utc)
    value = receipt(job, continuation={'automatic':True,'eligible_at':(now-timedelta(days=1)).isoformat()})
    jobs.mark_job_run(job['id'], True, functional_result=value)
    assert datetime.fromisoformat(jobs.get_job(job['id'])['next_run_at']) >= now+timedelta(seconds=59)
    value['outcome'] = 'failed'
    jobs.mark_job_run(job['id'], False, 'real failure', functional_result=value)
    assert 'continuation' not in jobs.get_job(job['id'])
