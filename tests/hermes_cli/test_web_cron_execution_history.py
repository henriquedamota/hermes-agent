"""The Desktop history reads real execution receipts, including script-only jobs."""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import pytest

from cron import executions
from hermes_cli import web_server
from hermes_cli.execution_result import build_result, write_result
from cron.functional_results import result_path
from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override
from hermes_state import SessionDB


@pytest.fixture
def homes(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    homes = {name: tmp_path / name for name in ('default', 'worker')}
    for home in homes.values():
        home.mkdir()
        SessionDB(home / 'state.db').close()
    monkeypatch.setattr(executions, 'EXECUTIONS_FILE', None)
    monkeypatch.setattr(web_server, '_cron_profile_home', lambda name: (name or 'default', homes[name or 'default']))
    monkeypatch.setattr(web_server, '_find_cron_job_profile', lambda job: 'worker')
    monkeypatch.setattr(web_server, '_call_cron_for_profile', lambda *args: {'id': 'script-job'})
    return homes


def record(home, outcome='deferred', delivery='suppressed'):
    token = set_hermes_home_override(home)
    try:
        row = executions.create_execution('script-job', source='isolated')
        executions.mark_execution_running(row['id'])
        receipt = build_result(subject_type='stage', outcome=outcome,
            job_id='script-job', execution_id=row['id'], exit_code=75 if outcome == 'failed' else 0,
            reason={'code': 'dependency_pending', 'blocked_stage': 'extract'},
            evidence=[{'path': '/isolated/functional-proof'}],
            continuation={'condition': 'same cycle prerequisite', 'automatic': True})
        write_result(result_path(home, row['id']), receipt)
        executions.finish_execution(row['id'], success=outcome != 'failed', delivery_outcome=delivery)
        return row
    finally:
        reset_hermes_home_override(token)


@pytest.mark.parametrize('outcome', ['completed', 'noop', 'deferred', 'partial', 'skipped', 'failed'])
def test_script_execution_without_session_is_visible(homes, outcome):
    row = record(homes['worker'], outcome, delivery='failed')
    result = web_server._list_cron_job_runs_sync('human-job-name', 'worker')
    assert result['runs'] == []  # No fabricated conversation.
    history = result['execution_history']
    assert history['contract'] == 'hermes.execution-history/v1'
    assert history['profile'] == 'worker'
    assert len(history['records']) == 1
    run = history['records'][0]
    assert run['id'] == row['id']
    assert run['status'] == ('failed' if outcome == 'failed' else 'completed')
    assert run['functional_result']['outcome'] == outcome
    assert run['functional_result']['exit_code'] == (75 if outcome == 'failed' else 0)
    assert run['delivery_outcome'] == run['functional_result']['delivery']['status'] == 'failed'
    assert 'o trabalho não será repetido' in run['message']


def test_concurrent_profiles_and_legacy_conversations_remain_separate(homes):
    rows = {name: record(home) for name, home in homes.items()}
    db = SessionDB(homes['worker'] / 'state.db')
    sid = 'cron_script-job_20260909_000000'
    try:
        db.create_session(session_id=sid, source='cron')
        db.append_message(sid, role='assistant', content='Historical conversation')
        db.end_session(sid, 'completed')
    finally:
        db.close()
    original_home = get_hermes_home()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda name: web_server._list_cron_job_runs_sync('script-job', name), homes))
    for name, result in zip(homes, results):
        assert [r['id'] for r in result['execution_history']['records']] == [rows[name]['id']]
    assert results[0]['runs'] == []
    assert results[1]['runs'][0]['id'] == sid
    assert get_hermes_home() == original_home


def test_history_uses_sequence_despite_clock_reversal_and_invalid_receipt(homes, monkeypatch):
    clocks = iter([datetime.fromisoformat(t) for t in [
        '2026-09-09T09:00:00+00:00', '2026-09-09T08:00:00+00:00']])
    token = set_hermes_home_override(homes['worker'])
    try:
        monkeypatch.setattr(executions, '_hermes_now', lambda: next(clocks))
        first = executions.create_execution('script-job', source='isolated')
        second = executions.create_execution('script-job', source='isolated')
    finally:
        reset_hermes_home_override(token)
    import sqlite3
    with sqlite3.connect(homes['worker'] / 'cron/executions.db') as conn:
        conn.execute('UPDATE executions SET functional_result_json=? WHERE id=?', (json.dumps({'observed_at': 'invalid'}), second['id']))
    result = web_server._list_cron_job_runs_sync('script-job', limit=1)
    records = result['execution_history']['records']
    assert [r['id'] for r in records] == [second['id']]
    assert records[0]['sequence'] > first['sequence']
    assert records[0]['functional_result']['outcome'] == 'unknown'
    assert records[0]['functional_result']['reason']['code'] == 'invalid_persisted_functional_result'


def test_ledger_read_failure_is_not_empty_history(homes, monkeypatch):
    def unavailable(**kwargs):
        raise RuntimeError('isolated unavailable ledger')
    monkeypatch.setattr(executions, 'list_executions', unavailable)
    with pytest.raises(RuntimeError, match='unavailable ledger'):
        web_server._list_cron_job_runs_sync('script-job', 'worker')


def test_completed_process_without_receipt_keeps_functional_result_unknown(homes):
    token = set_hermes_home_override(homes['worker'])
    try:
        row = executions.create_execution('script-job', source='isolated')
        executions.finish_execution(row['id'], success=True)
    finally:
        reset_hermes_home_override(token)
    history = web_server._list_cron_job_runs_sync('script-job', 'worker')['execution_history']
    run = history['records'][0]
    assert run['status'] == 'completed'
    assert run['functional_result']['outcome'] == 'unknown'
    assert run['functional_result']['exit_code'] is None


def test_authenticated_http_route_exports_the_same_receipt(homes):
    from starlette.testclient import TestClient
    row = record(homes['worker'], 'partial')
    client = TestClient(web_server.app)
    try:
        client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
        response = client.get('/api/cron/jobs/script-job/runs?profile=worker&limit=5')
        assert response.status_code == 200
        payload = response.json()
        assert payload['limit'] == 5
        assert payload['runs'] == []
        exported = payload['execution_history']['records'][0]
        assert exported['id'] == row['id']
        assert exported['functional_result']['outcome'] == 'partial'
        assert exported['message'].startswith('Progresso parcial preservado')
    finally:
        client.close()
