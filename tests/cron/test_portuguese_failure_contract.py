"""Scheduler failures use the same persisted result as the Portuguese message."""
import json

import pytest

from cron import functional_results as results
from cron import scheduler
from hermes_cli.execution_result import build_result, notification_required, render_result, write_result


def task(**extra):
    return {'id': 'isolated-failure', 'name': 'Verificação isolada',
            'schedule': {'kind': 'cron'}, 'failure_streak': 3,
            'execution_policy': {'locale': 'pt-BR', 'functional_result': 'optional'}, **extra}


def test_opaque_error_does_not_infer_provider_or_credential_failure():
    message = scheduler._summarize_cron_failure_for_delivery(
        task(), 'authentication marker in fixture token=do-not-deliver')
    assert 'Resultado funcional não comprovado' in message
    assert 'provider authentication error' not in message
    assert 'do-not-deliver' not in message


def test_recurrence_does_not_recommend_a_pause(monkeypatch):
    monkeypatch.setattr(scheduler, 'load_config', lambda: {})
    message = scheduler._failure_streak_nudge(task())
    assert '4' in message
    assert 'falhas' in message
    assert 'pause' not in message and 'pausar' not in message


@pytest.mark.parametrize('kind,outcome', [
    ('execution_failed', 'unknown'),
    ('scheduler_exception', 'unknown'),
    ('configuration_refused', 'skipped'),
    ('configuration_check_failed', 'unknown'),
    ('inference_configuration_drift', 'skipped'),
])
def test_failure_observation_and_ledger_share_the_same_result(tmp_path, kind, outcome):
    message = results.scheduler_failure_message(tmp_path, 'abc123', task(), kind=kind)
    ledger = results.ledger_result(tmp_path, 'abc123', task()['id'], delivery_outcome=None)
    assert ledger['outcome'] == outcome
    assert ledger['metrics']['scheduler_failure']['kind'] == kind
    assert message == render_result(ledger, locale='pt-BR')
    assert 'abc123' in message
    assert 'Retomada:' in message
    evidence = ledger['evidence'][-1]
    assert evidence['role'] == 'scheduler_failure'
    from pathlib import Path
    path = Path(evidence['path'])
    assert path.stat().st_mode & 0o777 == 0o600
    before = path.read_bytes()
    assert results.scheduler_failure_message(tmp_path, 'abc123', task(), kind=kind) == message
    assert path.read_bytes() == before


def test_later_scheduler_error_preserves_completed_external_work(tmp_path):
    receipt = build_result(subject_type='worker', outcome='completed', execution_id='finished',
        job_id=task()['id'], exit_code=0, progress={'completed':1,'total':1,'unit':'item'},
        evidence=[{'path':'/isolated/effect-receipt'}])
    write_result(results.result_path(tmp_path, 'finished'), receipt)
    message = results.scheduler_failure_message(tmp_path, 'finished', task(), kind='scheduler_exception')
    ledger = results.ledger_result(tmp_path, 'finished', task()['id'], delivery_outcome='failed')
    assert ledger['outcome'] == 'completed'
    assert ledger['delivery']['status'] == 'failed'
    assert json.loads(results.result_path(tmp_path, 'finished').read_text()) == receipt
    assert 'Trabalho do consumidor concluído' in message
    assert 'scheduler' in message and 'preservado' in message


def test_invalid_next_opportunity_is_unknown(tmp_path):
    job = task(next_run_at='not-a-timestamp')
    message = results.scheduler_failure_message(tmp_path, 'invalid-clock', job, kind='execution_failed')
    ledger = results.ledger_result(tmp_path, 'invalid-clock', job['id'], delivery_outcome=None)
    assert ledger['continuation']['eligible_at'] is None
    assert 'not-a-timestamp' not in message


def test_canonical_scheduler_clock_supplies_the_next_opportunity(tmp_path, monkeypatch):
    from cron import jobs
    from datetime import datetime, timezone
    monkeypatch.setattr(jobs, '_hermes_now', lambda: datetime(2030, 1, 1, 3, 4, 30, tzinfo=timezone.utc))
    job = task(enabled=True, next_run_at='2000-01-01T00:00:00Z',
               schedule={'kind':'cron', 'expr':'*/5 * * * *'})
    results.scheduler_failure_message(tmp_path, 'next-opportunity', job, kind='execution_failed')
    ledger = results.ledger_result(tmp_path, 'next-opportunity', job['id'], delivery_outcome=None)
    assert ledger['continuation']['eligible_at'] == '2030-01-01T03:05:00+00:00'
    assert ledger['continuation']['automatic'] is True
    assert ledger['metrics']['next_opportunity_source'] == 'canonical_scheduler_calculation'


def test_invalid_budget_does_not_break_failure_rendering(tmp_path):
    job = task(execution_policy={'locale':'pt-BR','wall_timeout_seconds':float('nan')})
    with pytest.raises(ValueError):
        results.policy_for(job)
    message = results.scheduler_failure_message(tmp_path, 'invalid-policy', job, kind='scheduler_exception')
    assert 'scheduler registrou uma exceção' in message
    assert 'Resultado funcional não comprovado' in scheduler._summarize_cron_failure_for_delivery(job, 'opaque')


def test_valid_generic_payload_cannot_forge_a_scheduler_observation(tmp_path):
    completed = build_result(subject_type='worker', outcome='completed', execution_id='verified',
        job_id=task()['id'], exit_code=0, evidence=[{'path':'/isolated/effect'}])
    write_result(results.result_path(tmp_path, 'verified'), completed)
    forged = build_result(subject_type='job', outcome='unknown', execution_id='verified', job_id=task()['id'])
    path = tmp_path/'cron/scheduler-failures/verified.json'
    write_result(path, forged)
    ledger = results.ledger_result(tmp_path, 'verified', task()['id'], delivery_outcome=None)
    assert ledger['outcome'] == 'completed'
    assert ledger['metrics']['scheduler_failure_observation']['status'] == 'invalid'
    assert notification_required(ledger)
    assert 'observação da execução é inválida' in render_result(ledger, locale='pt-BR')
    assert json.loads(path.read_text()) == forged


def test_invalid_observation_shape_is_actionable_without_breaking_renderer():
    result = build_result(subject_type='job', metrics={'scheduler_failure_observation': 'invalid-shape'})
    assert notification_required(result)
    assert 'observação da execução é inválida' in render_result(result, locale='pt-BR')


@pytest.mark.parametrize('error,kind,acknowledged', [
    ('opaque authentication marker token=do-not-deliver', 'execution_failed', False),
    ('[blocked_config] marker copied into an opaque error', 'execution_failed', False),
    ('[drift_skip:silent] marker copied into an opaque error', 'execution_failed', False),
    (RuntimeError('opaque exception token=do-not-deliver'), 'scheduler_exception', False),
    (RuntimeError('acknowledged incident'), 'scheduler_exception', True),
])
def test_real_dispatch_delivery_uses_persisted_failure(tmp_path, monkeypatch, error, kind, acknowledged):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(scheduler, '_get_hermes_home', lambda: tmp_path)
    monkeypatch.setattr(scheduler, 'create_execution', lambda *a, **k: {'id': 'dispatch-failure'})
    monkeypatch.setattr(scheduler, 'claim_dispatch', lambda *a, **k: True)
    monkeypatch.setattr(scheduler, 'mark_execution_running', lambda *a, **k: {})
    monkeypatch.setattr(scheduler, 'mark_job_run', lambda *a, **k: True)
    monkeypatch.setattr(scheduler, 'finish_execution', lambda *a, **k: None)
    monkeypatch.setattr(scheduler, 'save_job_output', lambda *a, **k: str(tmp_path/'output.txt'))
    monkeypatch.setattr(scheduler, '_upsert_incident_for_failure', lambda *a, **k: (acknowledged, None))
    monkeypatch.setattr(scheduler, 'load_config', lambda: {})
    sent = []
    monkeypatch.setattr(scheduler, '_deliver_result', lambda job, content, **kwargs: sent.append(content))
    def run(*args, **kwargs):
        if isinstance(error, Exception):
            raise error
        return False, 'isolated output', '', error
    monkeypatch.setattr(scheduler, 'run_job', run)
    # This boolean means the dispatcher handled the opportunity, not that
    # the job's work completed. The persisted functional result is decisive.
    scheduler.run_one_job(task(deliver='local'))
    ledger = results.ledger_result(tmp_path, 'dispatch-failure', task()['id'], delivery_outcome=None)
    assert ledger['metrics']['scheduler_failure']['kind'] == kind
    assert sent == ([] if acknowledged else [render_result(ledger, locale='pt-BR')])
    assert all('do-not-deliver' not in message for message in sent)


def test_actual_drift_refusal_uses_runtime_evidence_and_alerts_once(tmp_path, monkeypatch):
    from cron import jobs
    from tests.cron.test_cron_drift_alert_once import _job, _tick
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(scheduler, '_get_hermes_home', lambda: tmp_path)
    job = _job(execution_policy={'locale':'pt-BR'})
    deliveries = []
    with jobs.use_cron_store(tmp_path):
        jobs.save_jobs([job])
        for _ in range(2):
            current = next(row for row in jobs.load_jobs() if row['id'] == job['id'])
            _, agent_called = _tick(current, tmp_path, 'nous', deliveries)
            assert not agent_called
    assert len(deliveries) == 1
    assert 'Oportunidade não executada' in deliveries[0]
    assert 'configuração de inferência divergiu' in deliveries[0]
    receipts = [json.loads(p.read_text()) for p in (tmp_path/'cron/scheduler-failures').glob('*.json')]
    assert len(receipts) == 2
    assert all(r['metrics']['scheduler_failure']['inference_attempted'] is False for r in receipts)


def test_preflight_measurement_failure_never_starts_inference(tmp_path, monkeypatch):
    from cron import jobs
    from tests.cron.test_cron_drift_alert_once import _job, _tick
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(scheduler, '_get_hermes_home', lambda: tmp_path)
    monkeypatch.setattr(scheduler, '_cron_preflight_enabled', lambda config: True)
    def unavailable(*args):
        raise RuntimeError('measurement unavailable')
    monkeypatch.setattr(scheduler, '_preflight_job_config', unavailable)
    job = _job(provider_snapshot=None, execution_policy={'locale':'pt-BR'})
    deliveries = []
    with jobs.use_cron_store(tmp_path):
        jobs.save_jobs([job])
        _, agent_called = _tick(job, tmp_path, 'openrouter', deliveries)
    assert not agent_called
    assert len(deliveries) == 1
    assert 'verificação da configuração falhou' in deliveries[0]
    receipt = json.loads(next((tmp_path/'cron/scheduler-failures').glob('*.json')).read_text())
    assert receipt['outcome'] == 'unknown'
    assert receipt['metrics']['scheduler_failure']['kind'] == 'configuration_check_failed'
