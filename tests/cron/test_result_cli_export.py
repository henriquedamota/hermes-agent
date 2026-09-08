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


def test_latest_attempt_uses_durable_order_across_timezone_and_clock_changes(tmp_path, monkeypatch):
    from datetime import datetime
    from cron import executions as ledger
    monkeypatch.setattr(ledger,'EXECUTIONS_FILE',tmp_path/'execution.sqlite')
    monkeypatch.setenv('HERMES_HOME',str(tmp_path))
    clocks=iter([datetime.fromisoformat('2026-09-08T09:00:00+00:00'),
                 datetime.fromisoformat('2026-09-08T06:30:00-03:00'),
                 datetime.fromisoformat('2026-09-08T06:00:00-03:00')])
    monkeypatch.setattr(ledger,'_hermes_now',lambda:next(clocks))
    first=ledger.create_execution('job',source='isolated')
    second=ledger.create_execution('job',source='isolated')
    third=ledger.create_execution('job',source='isolated')
    assert ledger.latest_execution('job')['id']==third['id']
    assert ledger.latest_executions(['job'])['job']['id']==third['id']
    page=ledger.list_executions(limit=2)
    assert [row['id'] for row in page]==[third['id'],second['id']]
    older=ledger.list_executions(before_sequence=page[-1]['sequence'])
    assert [row['id'] for row in older]==[first['id']]


def test_durable_cursor_survives_database_vacuum(tmp_path, monkeypatch):
    import sqlite3
    from cron import executions as ledger
    target=tmp_path/'execution.sqlite'
    monkeypatch.setattr(ledger,'EXECUTIONS_FILE',target)
    monkeypatch.setenv('HERMES_HOME',str(tmp_path))
    first=ledger.create_execution('job',source='isolated')
    second=ledger.create_execution('job',source='isolated')
    third=ledger.create_execution('job',source='isolated')
    conn=sqlite3.connect(target)
    conn.execute('DELETE FROM executions WHERE id=?',(first['id'],));conn.commit()
    conn.execute('VACUUM');conn.close()
    assert [row['id'] for row in ledger.list_executions(before_sequence=third['sequence'])]==[second['id']]
    later=ledger.create_execution('job',source='isolated')
    assert later['sequence']>third['sequence']


def test_legacy_writer_after_migration_does_not_reuse_cursor(tmp_path, monkeypatch):
    import sqlite3
    from cron import executions as ledger
    target=tmp_path/'execution.sqlite'
    monkeypatch.setattr(ledger,'EXECUTIONS_FILE',target)
    monkeypatch.setenv('HERMES_HOME',str(tmp_path))
    first=ledger.create_execution('job',source='isolated')
    with sqlite3.connect(target) as conn:
        conn.execute("INSERT INTO executions(id,job_id,source,process_id,pid,status,claimed_at) VALUES('legacy','job','isolated','old',1,'completed','2026-09-08T09:00:00+00:00')")
    old=ledger.latest_execution('job')
    later=ledger.create_execution('job',source='isolated')
    assert old['id']=='legacy'
    assert first['sequence']<old['sequence']<later['sequence']
