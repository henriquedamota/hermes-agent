"""A blocked work lane cannot consume the reserved observation worker."""
import threading
import pytest
from cron import scheduler as s


def test_real_tick_keeps_observation_capacity_while_work_is_blocked(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME',str(tmp_path))
    monkeypatch.setattr(s,'_get_hermes_home',lambda:tmp_path)
    monkeypatch.setattr(s,'_get_lock_paths',lambda:(tmp_path,tmp_path/'tick.lock'))
    stop=threading.Event();entered=threading.Event();observed=threading.Event()
    heavy={'id':'heavy','name':'heavy','enabled':True,'prompt':'isolated',
           'execution_policy':{'lane':'work','priority':20}}
    probe={'id':'probe','name':'probe','enabled':True,'prompt':'isolated',
           'execution_policy':{'lane':'observation','priority':0}}
    monkeypatch.setattr(s,'get_due_jobs',lambda:[heavy,probe])
    monkeypatch.setattr(s,'advance_next_runs',lambda _:None)
    monkeypatch.setattr(s,'claim_job_for_fire',lambda *_a,**_k:True)
    monkeypatch.setattr(s,'load_config',lambda:{'cron':{'max_parallel_jobs':1,'execution_lanes':{'work':1,'observation':1}}})
    def run(job,**kwargs):
        if job['id']=='heavy':entered.set();stop.wait(3)
        else:observed.set()
        from cron.executions import finish_execution
        finish_execution(job['execution_id'],success=True)
        return True
    monkeypatch.setattr(s,'run_one_job',run)
    s._running_job_ids.clear()
    try:
        s.tick(sync=False)
        assert entered.wait(1)
        assert observed.wait(.5),'observation job was queued behind the unrelated heavy job'
    finally:
        stop.set();s._shutdown_parallel_pool()
        try:
            from cron.dispatch_lanes import shutdown
            shutdown()
        except ImportError:pass
        s._running_job_ids.clear()


def test_invalid_lane_does_not_advance_its_tick_or_block_valid_jobs(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(s,'_get_hermes_home',lambda:tmp_path)
    monkeypatch.setattr(s,'_get_lock_paths',lambda:(tmp_path,tmp_path/'tick.lock'))
    bad={'id':'bad','name':'bad','enabled':True,'prompt':'isolated','execution_policy':{'lane':'missing'}}
    good={'id':'good','name':'good','enabled':True,'prompt':'isolated'}
    monkeypatch.setattr(s,'get_due_jobs',lambda:[bad,good])
    advanced=[];ran=[]
    monkeypatch.setattr(s,'advance_next_runs',lambda ids:advanced.extend(ids))
    monkeypatch.setattr(s,'claim_job_for_fire',lambda *_a,**_k:True)
    monkeypatch.setattr(s,'load_config',lambda:{'cron':{'max_parallel_jobs':1,'execution_lanes':{'work':1}}})
    def run(job,**kwargs):
        ran.append(job['id'])
        from cron.executions import finish_execution
        finish_execution(job['execution_id'],success=True)
        return True
    monkeypatch.setattr(s,'run_one_job',run)
    s._running_job_ids.clear()
    try:
        s.tick(sync=True)
        assert ran==['good']
        assert advanced==['good']
        assert list((tmp_path/'cron/dispatch-observations').glob('history/*/*.json'))
    finally:
        s._shutdown_parallel_pool()
        from cron.dispatch_lanes import shutdown
        shutdown();s._running_job_ids.clear()
