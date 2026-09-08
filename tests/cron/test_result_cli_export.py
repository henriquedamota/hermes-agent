"""CLI consumers receive the canonical receipt, never a guess from process status."""
import json
from unittest.mock import patch
from hermes_cli.cron import cron_runs
from hermes_cli.execution_result import build_result


def test_json_history_keeps_deferred_and_failed_delivery_separate(capsys):
    receipt=build_result(subject_type='stage',outcome='deferred',exit_code=0,
        execution_id='e'*32,job_id='job',reason={'code':'dependency_pending','blocked_stage':'extract'})
    receipt['delivery']['status']='failed'
    row={'id':'e'*32,'job_id':'job','status':'completed','source':'builtin',
         'claimed_at':'2026-09-08T00:00:00-03:00','delivery_outcome':'failed',
         'functional_result_json':json.dumps(receipt)}
    with patch('cron.executions.list_executions',return_value=[row]):
        cron_runs(json_output=True)
    payload=json.loads(capsys.readouterr().out)
    assert payload['contract']=='hermes.execution-history/v1'
    result=payload['records'][0]
    assert result['status']=='completed'
    assert result['functional_result']['outcome']=='deferred'
    assert result['functional_result']['delivery']['status']=='failed'
    assert result['message'].startswith('Aguardando')


def test_legacy_zero_does_not_gain_functional_success(capsys):
    row={'id':'a'*32,'job_id':'job','status':'completed','source':'builtin',
         'claimed_at':'2026-09-08T00:00:00-03:00','functional_result_json':None}
    with patch('cron.executions.list_executions',return_value=[row]):
        cron_runs(json_output=True)
    result=json.loads(capsys.readouterr().out)['records'][0]
    assert result['functional_result']['outcome']=='unknown'
    assert result['functional_result']['observed_at']
    assert result['functional_result']['started_at'] is None


def test_artifact_and_delivery_survive_interruption_before_terminal(tmp_path, monkeypatch):
    from cron import executions as ledger
    monkeypatch.setattr(ledger,'EXECUTIONS_FILE',tmp_path/'execution.sqlite')
    monkeypatch.setenv('HERMES_HOME',str(tmp_path))
    row=ledger.create_execution('job',source='isolated')
    ledger.mark_execution_running(row['id'])
    assert ledger.record_observation(row['id'],output_file='/isolated/output.md')
    assert ledger.record_observation(row['id'],delivery_outcome='delivered')
    finished=ledger.finish_execution(row['id'],success=False,error='isolated interruption after delivery')
    assert finished['output_file']=='/isolated/output.md'
    assert finished['delivery_outcome']=='delivered'
    assert not ledger.record_observation(row['id'],delivery_outcome='failed')
