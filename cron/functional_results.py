"""Generic producer receipt boundary shared by scripts, scheduler and ledger."""
from __future__ import annotations

from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
import json
import math
import os
from pathlib import Path
import re
import stat
import time

from hermes_cli.execution_result import build_result, notification_required, render_result, validate_result, write_result

_CONTEXT: ContextVar[dict | None] = ContextVar('cron_execution_result', default=None)


def policy_for(job: dict) -> dict:
    policy = job.get('execution_policy')
    if policy is None:
        policy = {}
    if not isinstance(policy, dict):
        raise ValueError('execution_policy must be an object')
    allowed = {'functional_result', 'locale', 'wall_timeout_seconds', 'notification_mode', 'lane', 'priority'}
    if set(policy) - allowed:
        raise ValueError('unsupported execution policy field')
    if policy.get('functional_result', 'optional') not in ('optional', 'required'):
        raise ValueError('functional_result must be optional or required')
    if policy.get('locale', 'en') not in ('en', 'pt-BR'):
        raise ValueError('unsupported result locale')
    if policy.get('notification_mode', 'all') not in ('all', 'exceptions'):
        raise ValueError('notification_mode must be all or exceptions')
    if policy.get('lane') is not None and (not isinstance(policy['lane'], str)
            or not re.fullmatch(r'[a-z][a-z0-9_-]{0,31}', policy['lane'])):
        raise ValueError('invalid execution lane identity')
    priority = policy.get('priority', 100)
    if type(priority) is not int or not 0 <= priority <= 100:
        raise ValueError('execution priority must be between 0 and 100')
    budget = policy.get('wall_timeout_seconds')
    if budget is not None and (type(budget) not in (float,int) or not math.isfinite(budget) or budget <= 0):
        raise ValueError('wall timeout must be finite and positive')
    return policy


def result_path(home: Path, execution_id: str) -> Path:
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', execution_id):
        raise ValueError('unsafe execution result identity')
    return home / 'cron/functional-results' / f'{execution_id}.json'


def read_result(home: Path, execution_id: str, *, job_id: str | None = None) -> dict | None:
    path = result_path(home, execution_id)
    return _read_receipt(path, execution_id, job_id=job_id)


def _read_receipt(path: Path, execution_id: str, *, job_id: str | None = None) -> dict | None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    except FileNotFoundError:
        return None
    with os.fdopen(fd, encoding='utf-8') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 2 * 1024 * 1024:
            raise ValueError('functional result is not a bounded regular file')
        value = json.load(stream)
    result = validate_result(value, expected_execution_id=execution_id, now=datetime.now(timezone.utc))
    if job_id is not None and result['job_id'] != job_id:
        raise ValueError('result does not belong to this job')
    return result


def environment() -> dict[str, str]:
    context = _CONTEXT.get()
    return dict(context['environment']) if context else {}


def remaining_wall_seconds() -> float | None:
    context = _CONTEXT.get()
    deadline = context.get('deadline') if context else None
    return max(0,deadline-time.monotonic()) if deadline is not None else None


def record_process(*, script: str, pid: int | None, exit_code: int | None,
                   started_at: str, duration_s: float, reason: str) -> None:
    context = _CONTEXT.get()
    if context is not None:
        context['processes'].append({'script':script,'pid':pid,'exit_code':exit_code,
            'started_at':started_at,'finished_at':datetime.now(timezone.utc).isoformat(),
            'duration_s':duration_s,'reason':reason})


def _runtime_path(home: Path, execution_id: str) -> Path:
    return result_path(home,execution_id).parent.parent/'execution-observations'/f'{execution_id}.json'


def execution_scope(home_resolver):
    """Keep per-execution data out of process-global environment and other jobs."""
    def decorate(function):
        @wraps(function)
        def run(job, *args, **kwargs):
            execution_id = kwargs.get('execution_id')
            policy = policy_for(job)
            if not execution_id:
                if policy.get('functional_result') == 'required':
                    raise ValueError('required functional result needs an execution identity')
                return function(job, *args, **kwargs)
            home = home_resolver()
            path = result_path(home, execution_id)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            budget = policy.get('wall_timeout_seconds')
            started = time.monotonic()
            context = {'deadline': started+budget if budget else None,
                'started_at':datetime.now(timezone.utc).isoformat(), 'processes':[],
                'returned_success':None,
                'environment': {'HERMES_EXECUTION_ID':execution_id, 'HERMES_JOB_ID':str(job['id']),
                                'HERMES_RESULT_PATH':str(path)}}
            token = _CONTEXT.set(context)
            try:
                success, output, response, error = function(job, *args, **kwargs)
                context['returned_success'] = success
                try:
                    receipt = read_result(home, execution_id, job_id=str(job['id']))
                    if receipt is None and policy.get('functional_result') == 'required':
                        raise ValueError('required functional receipt missing')
                except (OSError, ValueError) as exc:
                    receipt = build_result(subject_type='job', outcome='unknown',
                        execution_id=execution_id, job_id=str(job['id']),
                        reason={'code':'invalid_functional_result', 'detail':str(exc)})
                    # Process completion is preserved in the ledger; unknown work
                    # must not become a success announcement or a cycle completion.
                    return False, output, render_result(receipt,locale=policy.get('locale','en')), str(exc)
                if receipt is None:
                    return success, output, response, error
                output += '\n\n' + json.dumps(receipt,ensure_ascii=False,sort_keys=True)
                if receipt['outcome'] in ('failed','unknown'):
                    success, error = False, receipt['reason']['code']
                # Failure of the process still matters even if its sidecar exists.
                response = render_result(receipt, locale=policy.get('locale','en'))
                if (success and policy.get('notification_mode') == 'exceptions'
                        and receipt['outcome'] in ('completed','noop','skipped','deferred','partial')
                        and not notification_required(receipt)):
                    response = '[SILENT]'
                return success, output, response, error
            finally:
                try:
                    runtime = build_result(subject_type='job',execution_id=execution_id,job_id=str(job['id']),
                        started_at=context['started_at'],finished_at=datetime.now(timezone.utc).isoformat(),
                        duration_s=time.monotonic()-started,
                        reason={'code':'runtime_observed'},
                        metrics={'processes':context['processes'],'returned_success':context['returned_success']},
                        policy={'wall_timeout_seconds':budget})
                    write_result(_runtime_path(home,execution_id),runtime)
                finally:
                    _CONTEXT.reset(token)
        return run
    return decorate


def ledger_result(home: Path, execution_id: str, job_id: str, *, delivery_outcome: str | None) -> dict:
    try:
        result = read_result(home, execution_id, job_id=job_id)
        if result is None:
            result = build_result(subject_type='job',execution_id=execution_id,job_id=job_id)
    except (OSError,ValueError) as exc:
        result = build_result(subject_type='job',execution_id=execution_id,job_id=job_id,
            reason={'code':'invalid_functional_result','detail':str(exc)})
    result['delivery']['status'] = delivery_outcome
    runtime_path = _runtime_path(home,execution_id)
    try:
        runtime = _read_receipt(runtime_path, execution_id, job_id=job_id)
    except (OSError, ValueError) as exc:
        # Observation loss is independently actionable; it never authorizes
        # repeating a producer's already proven external effect.
        result['metrics']['runtime_observation'] = {
            'status': 'invalid', 'reason': 'invalid_runtime_observation',
            'detail': str(exc), 'path': str(runtime_path)}
        runtime = None
    if runtime is not None:
        result['metrics']['runtime'] = runtime['metrics']
        result['evidence'].append({'path':str(runtime_path),'role':'process_observation'})
    return validate_result(result)


def failure_message(home: Path, execution_id: str, job: dict) -> str | None:
    policy = policy_for(job)
    if policy.get('functional_result') != 'required' and not result_path(home,execution_id).exists():
        return None
    result = ledger_result(home,execution_id,str(job['id']),delivery_outcome=None)
    return render_result(result,locale=policy.get('locale','en'))


def should_notify(home: Path, execution_id: str, job: dict) -> bool:
    if policy_for(job).get('notification_mode') != 'exceptions':
        return True
    result = ledger_result(home, execution_id, str(job['id']), delivery_outcome=None)
    return notification_required(result)
