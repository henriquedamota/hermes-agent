"""Exercise subprocess receipts through the real no-agent runtime, without delivery."""
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from cron import scheduler, executions
from hermes_cli.execution_result import build_result


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(scheduler, '_get_hermes_home', lambda: tmp_path)
    monkeypatch.setattr(scheduler, '_parse_wake_gate', lambda _: True)
    (tmp_path/'scripts').mkdir()
    return tmp_path


def job(script='example.py'):
    return {'id':'job','name':'isolated producer','no_agent':True,'script':script,'deliver':'local',
            'execution_policy':{'functional_result':'required','locale':'pt-BR','wall_timeout_seconds':10}}


def test_zero_exit_without_receipt_is_not_functional_completion(runtime):
    (runtime/'scripts/example.py').write_text("print('completed')\n", encoding='utf-8')
    result=scheduler.run_job(job(),execution_id='a'*32)
    assert result[0] is False
    assert 'comprovado' in result[2].lower()
    from cron.functional_results import ledger_result
    receipt = ledger_result(runtime,'a'*32,'job',delivery_outcome=None)
    assert receipt['outcome'] == 'unknown'
    assert receipt['metrics']['runtime']['processes'][0]['exit_code'] == 0
    assert receipt['metrics']['runtime']['returned_success'] is True


def test_deferred_receipt_survives_scheduler_without_complete_heading(runtime):
    module=Path(__import__('hermes_cli.execution_result',fromlist=['']).__file__)
    code=f'''import importlib.util,json,os
from pathlib import Path
s=importlib.util.spec_from_file_location('result', {str(module)!r}); m=importlib.util.module_from_spec(s); s.loader.exec_module(m)
r=m.build_result(subject_type='stage',outcome='deferred',exit_code=0,
    execution_id=os.environ['HERMES_EXECUTION_ID'],job_id=os.environ['HERMES_JOB_ID'],
    reason={{'code':'dependency_pending','blocked_stage':'extract'}},
    continuation={{'condition':'extract no mesmo ciclo','automatic':True}})
Path(os.environ['HERMES_RESULT_PATH']).write_text(json.dumps(r),encoding='utf-8')
print('completed stdout is not the functional result')
'''
    (runtime/'scripts/example.py').write_text(code,encoding='utf-8')
    ok,doc,message,error=scheduler.run_job(job(),execution_id='b'*32)
    assert ok and error is None
    assert 'aguardando' in message.lower()
    assert 'completed stdout' not in message
    assert 'execution-result/v1' in doc


def test_receipt_from_another_execution_is_refused(runtime):
    r=build_result(subject_type='probe',outcome='noop',exit_code=0,execution_id='foreign',metrics={'checked':1})
    code=f"import os\nfrom pathlib import Path\nPath(os.environ['HERMES_RESULT_PATH']).write_text({json.dumps(r)!r},encoding='utf-8')\n"
    (runtime/'scripts/example.py').write_text(code,encoding='utf-8')
    assert scheduler.run_job(job(),execution_id='c'*32)[0] is False


def test_functional_completion_and_delivery_failure_persist_independently(runtime, monkeypatch):
    monkeypatch.setattr(executions,'EXECUTIONS_FILE',runtime/'executions.db')
    entry=executions.create_execution('job',source='isolated')
    executions.mark_execution_running(entry['id'])
    r=build_result(subject_type='worker',outcome='completed',exit_code=0,
        job_id='job',execution_id=entry['id'],evidence=[{'path':'/test/effect-receipt'}])
    directory=runtime/'cron/functional-results';directory.mkdir(parents=True)
    (directory/f"{entry['id']}.json").write_text(json.dumps(r),encoding='utf-8')
    with patch('cron.executions.get_hermes_home',lambda:runtime):
        terminal=executions.finish_execution(entry['id'],success=True,delivery_outcome='failed')
    assert terminal['status']=='completed'
    assert terminal['functional_status']=='completed'
    assert terminal['delivery_outcome']=='failed'
    assert json.loads(terminal['functional_result_json'])['delivery']['status']=='failed'
    assert executions.finish_execution(entry['id'],success=False) is None


@pytest.mark.live_system_guard_bypass
def test_wall_deadline_reaps_script_grandchild_in_another_session(runtime):
    import os
    import sys
    import time
    import psutil
    if sys.platform == 'win32':
        pytest.skip('POSIX session boundary')
    pidfile=runtime/'child.pid'
    code=f'''import subprocess,sys,time
from pathlib import Path
p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],start_new_session=True)
Path({str(pidfile)!r}).write_text(str(p.pid),encoding='utf-8')
while True:
    print('heartbeat',flush=True)
    time.sleep(.02)
'''
    (runtime/'scripts/example.py').write_text(code,encoding='utf-8')
    task=job();task['execution_policy']['wall_timeout_seconds']=.4
    assert scheduler.run_job(task,execution_id='d'*32)[0] is False
    pid=int(pidfile.read_text())
    for _ in range(100):
        try:
            process=psutil.Process(pid)
            if not process.is_running() or process.status()==psutil.STATUS_ZOMBIE:break
        except psutil.NoSuchProcess:break
        time.sleep(.01)
    else:pytest.fail('script descendant survived its wall deadline')


def test_active_agent_heartbeat_does_not_reset_wall_budget(runtime):
    from threading import Event
    from unittest.mock import MagicMock
    stop=Event();interruptions=[]
    task={'id':'agent-budget','name':'isolated active agent','prompt':'isolated task',
          'execution_policy':{'wall_timeout_seconds':.4,'locale':'pt-BR'}}
    agent=MagicMock()
    agent.get_activity_summary.return_value={'seconds_since_activity':0}
    agent.run_conversation.side_effect=lambda *a,**k: (stop.wait(5) or {'final_response':'late'})
    def interrupt(_,message):
        interruptions.append(message);stop.set()
    with patch('cron.scheduler._hermes_home',runtime), \
         patch('cron.scheduler._resolve_origin',return_value=None), \
         patch('dotenv.load_dotenv'), \
         patch('hermes_state.get_shared_session_db',return_value=MagicMock()), \
         patch('hermes_cli.runtime_provider.resolve_runtime_provider',return_value={
             'api_key':'isolated-test','base_url':'https://example.invalid/v1','provider':'custom'}), \
         patch('run_agent.AIAgent',return_value=agent), \
         patch('cron.scheduler.request_hard_interrupt',side_effect=interrupt):
        try:
            ok,_,_,error=scheduler.run_job(task,execution_id='e'*32)
        finally:
            stop.set()
    assert not ok
    assert 'wall deadline' in error
    assert interruptions and 'wall deadline' in interruptions[0]


def test_invalid_runtime_observation_does_not_erase_functional_success(runtime):
    from cron.functional_results import ledger_result, result_path, _runtime_path
    from hermes_cli.execution_result import write_result
    execution_id='f'*32
    receipt=build_result(subject_type='worker',outcome='completed',exit_code=0,
        job_id='job',execution_id=execution_id,evidence=[{'path':'/test/preserved-effect'}])
    write_result(result_path(runtime,execution_id),receipt)
    observation=_runtime_path(runtime,execution_id)
    observation.parent.mkdir(parents=True)
    observation.write_text('{broken',encoding='utf-8')
    result=ledger_result(runtime,execution_id,'job',delivery_outcome='failed')
    assert result['outcome']=='completed'
    assert result['delivery']['status']=='failed'
    assert result['metrics']['runtime_observation']['status']=='invalid'
    assert result['metrics']['runtime_observation']['reason']=='invalid_runtime_observation'
    assert observation.read_text(encoding='utf-8')=='{broken'
