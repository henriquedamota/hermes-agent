"""MCP connection failures must survive the CLI/process boundary."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'context'))


def invoke(tmp_path, server, *options):
    home = tmp_path / 'hermes-home'
    home.mkdir(exist_ok=True)
    (home / 'config.yaml').write_text(yaml.safe_dump({'mcp_servers': {'local': server}}))
    return subprocess.run(
        [sys.executable, '-m', 'hermes_cli.main', 'mcp', 'test', 'local', *options],
        cwd=ROOT, env=os.environ | {'HERMES_HOME': str(home), 'PYTHONPATH': str(ROOT)},
        capture_output=True, text=True, timeout=35,
    )


def test_connection_failure_is_nonzero_in_human_cli(tmp_path):
    result = invoke(tmp_path, {'command': str(tmp_path / 'absent-server')})
    assert result.returncode == 1, result.stdout + result.stderr
    assert 'Connection or tool discovery failed' in result.stdout, result.stderr


def test_json_probe_uses_canonical_receipt_and_real_stdio_discovery(tmp_path, monkeypatch):
    from hermes_cli.execution_result import validate_result

    server = tmp_path / 'server.py'
    parent = tmp_path / 'parent-result.json'
    parent.write_text('parent execution remains open')
    monkeypatch.setenv('HERMES_EXECUTION_ID', 'parent-execution')
    monkeypatch.setenv('HERMES_RESULT_PATH', str(parent))
    server.write_text('''import json, sys
for line in sys.stdin:
    request = json.loads(line)
    if 'id' not in request:
        continue
    response = {'jsonrpc': '2.0', 'id': request['id']}
    if request['method'] == 'initialize':
        response['result'] = {'protocolVersion': request['params']['protocolVersion'],
            'capabilities': {'tools': {}}, 'serverInfo': {'name': 'isolated-probe', 'version': '1'}}
    elif request['method'] == 'tools/list':
        response['result'] = {'tools': [{'name': 'echo', 'description': 'Fixture only',
            'inputSchema': {'type': 'object', 'properties': {'value': {'type': 'string'}}}}]}
    elif request['method'] == 'ping':
        response['result'] = {}
    else:
        response['error'] = {'code': -32601, 'message': 'Method not found'}
    print(json.dumps(response), flush=True)
''')
    result = invoke(tmp_path, {'command': sys.executable, 'args': [str(server)]}, '--json')
    log = tmp_path / 'hermes-home' / 'logs' / 'mcp-stderr.log'
    assert result.returncode == 0, result.stdout + result.stderr + (log.read_text() if log.exists() else '')
    receipt = validate_result(json.loads(result.stdout))
    assert receipt['subject_type'] == 'probe'
    assert receipt['outcome'] == 'completed'
    assert receipt['metrics']['tool_names'] == ['echo']
    assert receipt['metrics']['operations_verified'] == ['initialize', 'tools/list']
    assert receipt['metrics']['tool_calls_verified'] is False
    assert receipt['evidence']
    for proof in receipt['evidence']:
        assert Path(proof['path']).is_relative_to(tmp_path)
        assert Path(proof['path']).is_file()
    assert receipt['execution_id'] != os.environ.get('HERMES_EXECUTION_ID')
    assert parent.read_text() == 'parent execution remains open'


def test_json_connection_failure_preserves_failure_without_secret(tmp_path):
    result = invoke(tmp_path, {'command': str(tmp_path / 'absent-server')}, '--json')
    assert result.returncode == 1, result.stdout + result.stderr
    receipt = json.loads(result.stdout)
    assert receipt['outcome'] == 'failed'
    assert receipt['reason']['code'] == 'mcp_connection_failed'
    assert receipt['metrics']['tool_names'] is None


def test_dispatcher_propagates_missing_server_status(monkeypatch, capsys):
    from hermes_cli import mcp_config
    monkeypatch.setattr(mcp_config, '_get_mcp_servers', lambda: {})
    with pytest.raises(SystemExit) as result:
        mcp_config.mcp_command(argparse.Namespace(mcp_action='test', name='absent', json=False))
    assert result.value.code == 2


def test_failure_evidence_does_not_record_exception_credentials(monkeypatch, capsys):
    from hermes_cli import mcp_config
    monkeypatch.setattr(mcp_config, '_get_mcp_servers', lambda: {'local': {}})
    def fail(*_args):
        raise RuntimeError('token=supersecret https://user:password@example.invalid')
    monkeypatch.setattr(mcp_config, '_probe_single_server', fail)
    assert mcp_config.cmd_mcp_test(argparse.Namespace(name='local', json=True)) == 1
    output = capsys.readouterr().out
    receipt = json.loads(output)
    assert 'supersecret' not in output
    assert 'password' not in output
    assert receipt['metrics']['error_type'] == 'RuntimeError'
    assert 'supersecret' not in Path(receipt['evidence'][0]['path']).read_text()


def test_clock_reversal_never_publishes_completion():
    from datetime import datetime, timedelta, timezone
    import time
    from hermes_cli.mcp_config import _record_mcp_probe
    receipt = _record_mcp_probe('local', started_at=datetime.now(timezone.utc) + timedelta(hours=1),
        started_monotonic=time.monotonic(), tools=[('echo', '')], code=0, reason='mcp_discovery_verified')
    assert receipt['outcome'] == 'unknown'
    assert receipt['exit_code'] == 74
    assert receipt['started_at'] is None
