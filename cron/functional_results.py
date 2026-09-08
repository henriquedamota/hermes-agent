"""Generic producer receipt boundary shared by scripts, scheduler and ledger."""
from __future__ import annotations

from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time

from hermes_cli.execution_result import build_result, notification_required, render_result, validate_result, write_result

_CONTEXT: ContextVar[dict | None] = ContextVar('cron_execution_result', default=None)


def result_locale(job: dict) -> str:
    """Rendering must remain available when another policy field is invalid."""
    policy = job.get('execution_policy')
    return 'pt-BR' if isinstance(policy, dict) and policy.get('locale') == 'pt-BR' else 'en'


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


def record_dispatch_refusal(kind: str, *, already_alerted: bool = False) -> None:
    context = _CONTEXT.get()
    if context is not None and context['locale'] == 'pt-BR':
        if kind not in ('configuration_refused', 'configuration_check_failed', 'inference_configuration_drift'):
            raise ValueError('unsupported dispatch refusal')
        context['dispatch_refusal'] = {'kind': kind, 'already_alerted': already_alerted}


def dispatch_refusal(home: Path, execution_id: str, job_id: str) -> dict | None:
    runtime = _read_receipt(_runtime_path(home, execution_id), execution_id, job_id=job_id)
    refusal = runtime['metrics'].get('dispatch_refusal') if runtime else None
    if refusal is None:
        return None
    if (not isinstance(refusal, dict) or type(refusal.get('already_alerted')) is not bool
            or refusal.get('kind') not in ('configuration_refused', 'configuration_check_failed', 'inference_configuration_drift')):
        raise ValueError('invalid dispatch refusal observation')
    return refusal


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
                'locale': result_locale(job), 'dispatch_refusal': None,
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
                        metrics={'processes':context['processes'],'returned_success':context['returned_success'],
                                 **({'dispatch_refusal':context['dispatch_refusal']} if context['dispatch_refusal'] else {})},
                        policy={'wall_timeout_seconds':budget})
                    write_result(_runtime_path(home,execution_id),runtime)
                finally:
                    _CONTEXT.reset(token)
        return run
    return decorate


def ledger_result(home: Path, execution_id: str, job_id: str, *, delivery_outcome: str | None) -> dict:
    missing = False
    try:
        result = read_result(home, execution_id, job_id=job_id)
        if result is None:
            missing = True
            result = build_result(subject_type='job',execution_id=execution_id,job_id=job_id)
    except (OSError,ValueError) as exc:
        result = build_result(subject_type='job',execution_id=execution_id,job_id=job_id,
            reason={'code':'invalid_functional_result','detail':str(exc)})
    failure_path = _scheduler_failure_path(home, execution_id)
    try:
        failure = _read_scheduler_failure(failure_path, execution_id, job_id)
    except (OSError, ValueError) as exc:
        result['metrics']['scheduler_failure_observation'] = {
            'status': 'invalid', 'error_type': type(exc).__name__, 'path': str(failure_path)}
        failure = None
    if failure is not None:
        if missing:
            result = failure
        result['metrics']['scheduler_failure'] = failure['metrics']['scheduler_failure']
        result['evidence'].append({'path': str(failure_path), 'role': 'scheduler_failure',
            'sha256': hashlib.sha256(failure_path.read_bytes()).hexdigest()})
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


_SCHEDULER_FAILURES = {
    'execution_failed': ('unknown', 'A execução terminou com falha; a conclusão funcional não foi comprovada.',
                         'classificação da causa e verificação dos efeitos já executados'),
    'scheduler_exception': ('unknown', 'O scheduler registrou uma exceção; a conclusão funcional precisa de evidência.',
                            'correção da causa registrada e verificação dos efeitos já executados'),
    'configuration_refused': ('skipped', 'A validação da configuração recusou a chamada ao modelo.',
                              'configuração declarada válida e próxima oportunidade elegível'),
    'configuration_check_failed': ('unknown', 'A verificação da configuração falhou; a chamada ao modelo não foi iniciada.',
                                   'verificação da configuração disponível e próxima oportunidade elegível'),
    'inference_configuration_drift': ('skipped', 'A configuração de inferência divergiu; o disparo foi recusado antes da chamada ao modelo.',
                                      'reconciliação de modelo/provedor e próxima oportunidade elegível'),
}


def _scheduler_failure_path(home: Path, execution_id: str) -> Path:
    return result_path(home, execution_id).parent.parent / 'scheduler-failures' / f'{execution_id}.json'


def _read_scheduler_failure(path: Path, execution_id: str, job_id: str) -> dict | None:
    value = _read_receipt(path, execution_id, job_id=job_id)
    if value is None:
        return None
    failure = value['metrics'].get('scheduler_failure')
    if (value['subject_type'] != 'job' or value['policy'].get('id') != 'hermes-scheduler-failure/v1'
            or not isinstance(failure, dict) or failure.get('kind') not in _SCHEDULER_FAILURES
            or failure.get('scheduler_status') != 'failed'
            or value['outcome'] != _SCHEDULER_FAILURES[failure['kind']][0]):
        raise ValueError('invalid scheduler failure observation')
    return value


def _failure_observation(job: dict, kind: str, execution_id: str | None = None) -> dict:
    # Kind comes from the scheduler branch, never from opaque exception prose.
    outcome, detail, continuation = _SCHEDULER_FAILURES[kind]
    next_at = None
    next_source = None
    valid_clock = job.get('next_run_at') is None
    raw = job.get('next_run_at')
    if isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw.replace('Z', '+00:00'))
            valid_clock = parsed.tzinfo is not None
            if parsed.tzinfo is not None and parsed > datetime.now(timezone.utc):
                next_at = parsed.isoformat()
                next_source = 'persisted_schedule'
        except ValueError:
            pass
    schedule = job.get('schedule')
    recurring = isinstance(schedule, dict) and schedule.get('kind') in ('cron', 'interval')
    if next_at is None and valid_clock and recurring and job.get('enabled') is True:
        try:
            from cron.jobs import compute_next_run
            calculated = compute_next_run(schedule)
            parsed = datetime.fromisoformat(calculated.replace('Z', '+00:00')) if calculated else None
            if parsed is not None and parsed.tzinfo is not None and parsed > datetime.now(timezone.utc):
                next_at = parsed.isoformat()
                next_source = 'canonical_scheduler_calculation'
        except (OSError, ValueError, TypeError, OverflowError, ImportError):
            pass
    name = str(job.get('name') or job.get('id') or 'Job')[:100]
    return build_result(subject_type='job', outcome=outcome,
        execution_id=execution_id, job_id=str(job['id']) if job.get('id') else None,
        reason={'code': kind, 'detail': name + ': ' + detail},
        metrics={'next_opportunity_source': next_source,
                 'scheduler_failure': {'kind': kind, 'scheduler_status': 'failed',
                                      'inference_attempted': False if kind in ('configuration_refused', 'configuration_check_failed', 'inference_configuration_drift') else None}},
        continuation={'condition': continuation, 'eligible_at': next_at,
                      'automatic': True if recurring and job.get('enabled') is True else None},
        policy={'id': 'hermes-scheduler-failure/v1'})


def unattributed_failure_message(job: dict) -> str:
    """Emergency rendering without an execution identity; no cause is inferred."""
    return render_result(_failure_observation(job, 'execution_failed'), locale='pt-BR')


def scheduler_failure_message(home: Path, execution_id: str, job: dict, *, kind: str) -> str | None:
    if result_locale(job) != 'pt-BR':
        return failure_message(home, execution_id, job)
    path = _scheduler_failure_path(home, execution_id)
    previous = _read_scheduler_failure(path, execution_id, str(job['id']))
    if previous is None:
        write_result(path, _failure_observation(job, kind, execution_id))
    result = ledger_result(home, execution_id, str(job['id']), delivery_outcome=None)
    return render_result(result, locale='pt-BR')


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
