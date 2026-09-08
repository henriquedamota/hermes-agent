"""Bounded execution capacity inside the existing scheduler, not another ticker."""
from __future__ import annotations
import atexit
from concurrent.futures import ThreadPoolExecutor
import re
import threading

_POOLS: dict[str,tuple[int,ThreadPoolExecutor]] = {}
_LOCK=threading.RLock()


def validate(config) -> dict[str,int]:
    if not isinstance(config,dict):raise ValueError('cron.execution_lanes must be an object')
    for name,count in config.items():
        if not isinstance(name,str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}',name) or name=='default':
            raise ValueError('invalid execution lane identity')
        if type(count) is not int or not 1<=count<=32:
            raise ValueError('execution lane capacity must be between 1 and 32')
    if sum(config.values())>64:raise ValueError('combined execution lane capacity exceeds 64')
    return config


def pool(name: str, config: dict) -> ThreadPoolExecutor:
    policy=validate(config)
    if name not in policy:raise ValueError(f'execution lane is not configured: {name}')
    with _LOCK:
        current=_POOLS.get(name)
        if current and current[0]!=policy[name]:
            raise ValueError('changing live lane capacity requires scheduler drain/restart')
        if current:return current[1]
        executor=ThreadPoolExecutor(max_workers=policy[name],thread_name_prefix='cron-lane-'+name)
        _POOLS[name]=(policy[name],executor)
        return executor


def shutdown():
    with _LOCK:
        pending=list(_POOLS.values());_POOLS.clear()
    for _,executor in pending:executor.shutdown(wait=True,cancel_futures=False)


atexit.register(shutdown)


def record_refusal(home, job: dict, detail: str) -> None:
    """Persist an unconsumed scheduling opportunity independently of any run."""
    from datetime import datetime, timedelta, timezone
    import uuid
    from hermes_cli.execution_result import build_result, write_result
    from cron.jobs import note_fire_forward_failure
    ident = uuid.uuid4().hex
    value = build_result(subject_type='job', outcome='failed', execution_id=ident,
        job_id=str(job['id']), reason={'code':'dispatch_configuration_invalid','detail':detail},
        continuation={'automatic':True, 'eligible_at':(datetime.now(timezone.utc)+timedelta(seconds=60)).isoformat(),
            'condition':'corrigir a configuração de capacidade; a oportunidade permanece pendente'},
        metrics={'process_started':False,'schedule_advanced':False})
    write_result(home/'cron/dispatch-observations'/f'{ident}.json', value)
    note_fire_forward_failure(job['id'], detail)
