"""A process return must neither heal a deferred failure nor lose its next chance."""
from datetime import datetime, timedelta, timezone

import pytest
from cron import jobs
from hermes_cli.execution_result import build_result


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
