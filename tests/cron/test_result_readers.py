"""Real producer/store/CLI boundary; no external delivery or live configuration."""
import json
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import pytest

from cron import jobs, executions
from hermes_cli import cron as cli
from hermes_cli.execution_result import build_result
from tools.cronjob_tools import cronjob


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(jobs, 'CRON_DIR', tmp_path / 'cron')
    monkeypatch.setattr(jobs, 'JOBS_FILE', tmp_path / 'cron/jobs.json')
    monkeypatch.setattr(jobs, 'OUTPUT_DIR', tmp_path / 'cron/output')
    monkeypatch.setattr(executions, 'EXECUTIONS_FILE', tmp_path / 'cron/executions.db')
    (tmp_path / 'scripts').mkdir()
    return tmp_path


def producer(home, outcome, *, create=True):
    import hermes_cli.execution_result as contract
    root = str(Path(contract.__file__).resolve().parents[1])
    code = 75 if outcome == 'failed' else 0
    (home / 'scripts/producer.py').write_text(f'''import sys, os
sys.path.insert(0, {root!r})
from hermes_cli.execution_result import build_result, write_result
r=build_result(subject_type='stage', outcome={outcome!r}, exit_code={code},
    execution_id=os.environ['HERMES_EXECUTION_ID'],job_id=os.environ['HERMES_JOB_ID'],
    reason={{'code':'isolated_' + {outcome!r}, 'detail':'Resultado do teste isolado.'}},
    metrics={{'measured_items':0}}, evidence=[{{'path':'/isolated/output-proof'}}],
    continuation={{'condition':'dependência do mesmo ciclo','automatic':True}})
write_result(os.environ['HERMES_RESULT_PATH'],r)
raise SystemExit({code})
''', encoding='utf-8')
    if not create:
        return None
    return jobs.create_job(prompt='Isolated result', schedule='every 1h',
        script='producer.py', no_agent=True, deliver='local',
        execution_policy={'functional_result':'required', 'locale':'pt-BR',
                          'wall_timeout_seconds':20})


@pytest.mark.parametrize('outcome', ['completed','noop','deferred','partial','skipped','failed','unknown'])
def test_real_manual_run_exports_functional_result_and_doctor_uses_it(isolated, capsys, outcome):
    job = producer(isolated, outcome)
    payload = json.loads(cronjob(action='run', job_id=job['id']))['job']
    assert payload['executed'] is True
    assert payload['functional_result']['outcome'] == outcome
    assert payload['execution_success'] is (outcome in ('completed','noop'))
    assert payload['functional_result']['exit_code'] == (75 if outcome == 'failed' else 0)
    rc = cli.cron_command(Namespace(cron_command='doctor'))
    output = capsys.readouterr().out
    assert rc == (1 if outcome in ('failed','unknown') else 0), output
    if outcome not in ('failed','unknown'):
        assert 'last run failed' not in output
        assert 'unknown error' not in output
    summary = jobs.list_jobs()[0]['last_result']
    assert summary['status'] == outcome
    assert summary['tone'] == ('success' if outcome in ('completed','noop') else
                               'destructive' if outcome in ('failed','unknown') else 'warning')


def test_manual_cli_renders_deferred_instead_of_failed(isolated, capsys):
    job = producer(isolated, 'deferred')
    cli.cron_command(Namespace(cron_command='run', job_id=job['id']))
    output = capsys.readouterr().out
    assert 'Aguardando condição de execução' in output
    assert 'Ran now: failed' not in output
    assert 'dependência do mesmo ciclo' in output


def test_wait_does_not_erase_a_previous_failure(isolated, capsys):
    job = producer(isolated, 'failed')
    cronjob(action='run', job_id=job['id'])
    failed = jobs.get_job(job['id'])
    # Same real producer now defers its next admitted attempt; preserve job identity.
    producer(isolated, 'deferred', create=False)
    cronjob(action='run', job_id=job['id'])
    after = jobs.get_job(job['id'])
    assert after['last_status'] == 'deferred'
    assert after['last_failure'] == failed['last_failure']
    assert after['failure_streak'] == 1
    assert cli.cron_command(Namespace(cron_command='doctor')) == 1
    output = capsys.readouterr().out
    assert 'unresolved earlier failure' in output
    assert 'last run failed: unknown error' not in output


def test_display_never_invents_none_as_error(isolated, capsys):
    job = producer(isolated, 'completed')
    cronjob(action='run', job_id=job['id'])
    cli.cron_command(Namespace(cron_command='list', all=True, json=False))
    assert 'completed: None' not in capsys.readouterr().out


def test_completed_work_and_delivery_failure_remain_separate(isolated):
    job = producer(isolated, 'completed')
    with patch('cron.scheduler._deliver_result', return_value='isolated delivery failure') as delivery:
        payload = json.loads(cronjob(action='run', job_id=job['id']))['job']
    assert delivery.call_count == 1
    assert payload['execution_success'] is False
    assert payload['functional_result']['outcome'] == 'completed'
    assert payload['functional_result']['delivery']['status'] == 'failed'
    assert payload['process_status'] == 'completed'
    assert payload['delivery_outcome'] == 'failed'
    summary = jobs.list_jobs()[0]['last_result']
    assert summary['status'] == 'completed'
    assert summary['tone'] == 'warning'
    assert not summary['failed']
    assert 'isolated delivery failure' in payload['execution_error']


def test_unknown_summary_is_not_invented_as_a_process_failure():
    from cron.result_export import last_result
    summary = last_result({'last_status':'unrecognized_status','last_error':None})
    assert summary['unknown'] and not summary['failed']
    assert summary['detail'] is None


@pytest.mark.parametrize('outcome,error,delivery,status', [
    ('deferred', None, None, 'completed'),
    ('completed', 'isolated delivery failure', 'failed', 'error'),
    ('unknown', 'receipt unavailable', None, 'error'),
])
def test_background_completion_uses_the_same_receipt(outcome, error, delivery, status):
    from tools.cronjob_tools import _try_dispatch_background_run
    receipt = build_result(subject_type='stage', outcome=outcome, exit_code=0,
        job_id='background', execution_id='isolated-background',
        reason={'code':'isolated_result'}, evidence=[{'path':'/isolated/proof'}],
        delivery={'status':delivery})
    task = {'id':'background', 'name':'isolated', 'deliver':'local',
            'execution_policy':{'locale':'pt-BR'}}
    observed = []
    def dispatch(**kwargs):
        observed.append(kwargs['runner']())
        return {'status':'dispatched', 'delegation_id':'isolated-handle'}
    with patch('gateway.session_context.async_delivery_supported', return_value=True), \
         patch('tools.approval.get_current_session_key', return_value='isolated-session'), \
         patch('tools.cronjob_tools.claim_job_for_fire', return_value=task), \
         patch('tools.cronjob_tools.get_job', return_value=task), \
         patch('tools.cronjob_tools._run_claimed_job', return_value={
             'success':False, 'error':error, 'functional_result':receipt}), \
         patch('tools.async_delegation.dispatch_async_delegation', side_effect=dispatch), \
         patch('tools.cronjob_tools._latest_job_output_excerpt') as old_excerpt:
        assert _try_dispatch_background_run(task)['dispatched']
    assert observed[0]['status'] == status
    assert observed[0]['functional_result']['outcome'] == outcome
    assert 'Result: FAILED' not in observed[0]['summary']
    assert 'isolated-background' in observed[0]['summary']
    old_excerpt.assert_not_called()


def test_manual_result_cannot_belong_to_another_job():
    from tools.cronjob_tools import _execute_job_now
    receipt = build_result(subject_type='stage', outcome='completed', exit_code=0,
        job_id='foreign', execution_id='isolated-execution', evidence=[{'path':'/isolated/proof'}])
    task = {'id':'expected', 'execution_id':'isolated-execution',
            'execution_policy':{'functional_result':'required'}}
    with patch('tools.cronjob_tools.claim_job_for_fire', return_value=task), \
         patch('cron.scheduler.run_one_job', return_value=True), \
         patch('tools.cronjob_tools.get_job', return_value={'last_status':'completed'}), \
         patch('cron.executions.get_execution', return_value={
             'id':'isolated-execution','job_id':'foreign','status':'completed',
             'functional_result_json':json.dumps(receipt)}):
        result = _execute_job_now(task)
    assert not result['success']
    assert result['functional_result']['outcome'] == 'unknown'
    assert result['functional_result']['job_id'] == 'expected'
    assert result['error'] == 'manual_execution_job_mismatch'
