"""Versioned functional results, independent of process and message delivery.

Producers supply evidence. Neither exit zero nor a delivered message is evidence
of functional completion. This dependency-free module is also the command-line
adapter used by installed producers outside the Hermes Python environment.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import sys
from typing import Any

CONTRACT = 'hermes.execution-result/v1'
OUTCOMES = ('completed', 'noop', 'skipped', 'deferred', 'partial', 'failed', 'unknown')
COMPLETED_OUTCOMES = ('completed', 'noop')
WAITING_OUTCOMES = ('skipped', 'deferred', 'partial')
FAILED_OUTCOMES = ('failed', 'unknown')
SUBJECTS = ('job', 'stage', 'cycle', 'worker', 'probe', 'maintenance')
IDENTITIES = ('job_id', 'execution_id', 'run_id', 'cycle_id', 'code_revision', 'input_revision', 'output_revision')
TIMES = ('started_at', 'finished_at', 'observed_at')


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError('timestamp must be a string with timezone')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise ValueError('invalid timestamp') from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError('timestamp requires an explicit timezone')
    return parsed.astimezone(timezone.utc)


def _finite(value: Any, name: str) -> None:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError(f'{name} must be finite and nonnegative')


def validate_result(value: Any, *, expected_execution_id: str | None = None,
                    now: datetime | None = None) -> dict:
    if not isinstance(value, dict) or value.get('contract') != CONTRACT or type(value.get('schema_version')) is not int or value['schema_version'] != 1:
        raise ValueError('unsupported functional result contract')
    if value.get('subject_type') not in SUBJECTS or value.get('outcome') not in OUTCOMES:
        raise ValueError('invalid subject or functional outcome')
    for key in IDENTITIES:
        if key not in value or (value[key] is not None and (not isinstance(value[key], str) or not value[key])):
            raise ValueError(f'invalid or missing {key}; unknown must be null')
    if expected_execution_id is not None and value['execution_id'] != expected_execution_id:
        raise ValueError('result does not belong to this execution')
    attempt = value.get('attempt')
    if attempt is not None and (type(attempt) is not int or attempt < 1):
        raise ValueError('invalid attempt')
    code = value.get('exit_code')
    if code is not None and (type(code) is not int or not -255 <= code <= 255):
        raise ValueError('invalid exit code')
    duration = value.get('duration_s')
    if duration is not None:
        _finite(duration, 'duration_s')
    times = {}
    for key in TIMES:
        if key not in value:
            raise ValueError(f'missing {key}')
        if value[key] is not None:
            times[key] = _timestamp(value[key])
    if 'observed_at' not in times:
        raise ValueError('observation time is required')
    if times.get('started_at') and times.get('finished_at') and times['finished_at'] < times['started_at']:
        raise ValueError('execution finished before it started')
    if now is not None:
        if now.tzinfo is None:
            raise ValueError('validation clock requires timezone')
        if (times['observed_at']-now).total_seconds() > 60:
            raise ValueError('result observation is in the future')
    reason = value.get('reason')
    if not isinstance(reason, dict) or not isinstance(reason.get('code'), str) or not reason['code']:
        raise ValueError('structured reason is required')
    for key in ('detail', 'blocked_stage'):
        if reason.get(key) is not None and not isinstance(reason[key], str):
            raise ValueError('invalid structured reason')
    for key in ('metrics', 'policy', 'progress', 'continuation', 'delivery'):
        if not isinstance(value.get(key), dict):
            raise ValueError(f'{key} must be an object')
    for key in ('notification_due', 'incident_active', 'recovered'):
        if key in value['metrics'] and type(value['metrics'][key]) is not bool:
            raise ValueError(f'metrics.{key} must be boolean')
    for key in ('completed', 'total'):
        if value['progress'].get(key) is not None:
            _finite(value['progress'][key], f'progress.{key}')
    continuation = value['continuation']
    if continuation.get('eligible_at') is not None:
        _timestamp(continuation['eligible_at'])
    if continuation.get('automatic') is not None and type(continuation['automatic']) is not bool:
        raise ValueError('continuation.automatic must be boolean or null')
    evidence = value.get('evidence')
    if not isinstance(evidence, list):
        raise ValueError('evidence must be an array')
    for entry in evidence:
        if not isinstance(entry, dict) or not isinstance(entry.get('path'), str) or not entry['path']:
            raise ValueError('evidence requires a traceable path')
        digest = entry.get('sha256')
        if digest is not None and (not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest)):
            raise ValueError('invalid evidence SHA-256')
    presentation=value.get('presentation', {})
    if not isinstance(presentation,dict):
        raise ValueError('presentation must be an object')
    if presentation.get('body') is not None:
        import hashlib
        body=presentation['body']
        if not isinstance(body,str) or not body.strip() or len(body.encode('utf-8')) > 256_000:
            raise ValueError('invalid presentation body')
        digest=hashlib.sha256(body.encode('utf-8')).hexdigest()
        if presentation.get('sha256') != digest or not any(entry.get('sha256')==digest and entry.get('role')=='message_body' for entry in evidence):
            raise ValueError('presentation requires matching immutable evidence')
    runtime=value['metrics'].get('runtime', {})
    if not isinstance(runtime,dict) or not isinstance(runtime.get('processes', []),list):
        raise ValueError('runtime process observations must be an array')
    if any(not isinstance(item,dict) or (item.get('reason') is not None and not isinstance(item['reason'],str)) for item in runtime.get('processes', [])):
        raise ValueError('invalid runtime process observation')
    if value['outcome'] in ('completed', 'noop') and code != 0:
        raise ValueError('functional completion requires exit zero')
    if value['outcome'] == 'completed' and not evidence:
        raise ValueError('functional completion requires evidence')
    if value['outcome'] == 'noop' and not evidence and not value['metrics']:
        raise ValueError('noop requires a measurement or evidence')
    if value['delivery'].get('status') not in (None, 'delivered', 'failed', 'not_configured', 'suppressed', 'suppressed_acked'):
        raise ValueError('invalid delivery outcome')
    # Reject non-JSON numbers in nested producer metrics as well.
    try:
        json.dumps(value, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise ValueError('result contains non-JSON data') from exc
    return value


def build_result(*, subject_type: str, outcome: str = 'unknown', **fields: Any) -> dict:
    value = {
        'contract': CONTRACT, 'schema_version': 1, 'subject_type': subject_type,
        'outcome': outcome, **dict.fromkeys(IDENTITIES), 'attempt': None,
        'started_at': None, 'finished_at': None, 'observed_at': datetime.now(timezone.utc).isoformat(),
        'timezone': 'UTC', 'duration_s': None, 'exit_code': None,
        'reason': {'code': 'functional_result_unknown', 'detail': None, 'blocked_stage': None},
        'progress': {'completed': None, 'total': None, 'unit': None, 'details': {}},
        'continuation': {'eligible_at': None, 'condition': None, 'automatic': None},
        'metrics': {}, 'policy': {'id': None, 'thresholds': {}}, 'evidence': [],
        'presentation': {'body': None, 'sha256': None},
        'delivery': {'status': None, 'reason': None},
    }
    for key, field in fields.items():
        if key not in value:
            raise ValueError(f'unknown result field: {key}')
        if key in ('reason','progress','continuation','policy','delivery') and isinstance(field,dict):
            value[key].update(field)
        else:
            value[key] = field
    return validate_result(value)


def _has_invalid_observation(metrics: dict) -> bool:
    return any(key in metrics and (not isinstance(metrics[key], dict)
               or metrics[key].get('status') == 'invalid')
               for key in ('runtime_observation', 'scheduler_failure_observation'))


def render_result(value: dict, *, locale: str = 'en') -> str:
    r = validate_result(value)
    pt = locale.lower().startswith('pt')
    titles = ({'completed':'Concluído', 'noop':'Verificado: nada a fazer', 'skipped':'Oportunidade não executada',
               'deferred':'Aguardando condição de execução', 'partial':'Progresso parcial preservado',
               'failed':'Execução falhou', 'unknown':'Resultado funcional não comprovado'} if pt else
              {'completed':'Completed', 'noop':'Checked: no work required', 'skipped':'Opportunity skipped',
               'deferred':'Waiting for execution conditions', 'partial':'Partial progress preserved',
               'failed':'Execution failed', 'unknown':'Functional result not verified'})
    title = titles[r['outcome']]
    if pt and r['outcome'] == 'completed':
        subject_titles = {'stage':'Etapa concluída','cycle':'Ciclo concluído',
                          'worker':'Trabalho do consumidor concluído','maintenance':'Manutenção concluída',
                          'probe':'Verificação concluída'}
        title = subject_titles.get(r['subject_type'], title)
        if r['subject_type'] == 'stage' and r['metrics'].get('stage'):
            title += ': ' + str(r['metrics']['stage'])
    body=r.get('presentation',{}).get('body')
    lines = [title] + ([body] if body else [])
    reason = r['reason']
    if reason.get('detail'):
        lines.append(reason['detail'])
    elif reason.get('blocked_stage'):
        lines.append(('Dependência: ' if pt else 'Dependency: ') + reason['blocked_stage'])
    elif r['outcome'] in ('failed', 'unknown'):
        lines.append(('Motivo: ' if pt else 'Reason: ') + reason['code'])
    metrics = r['metrics']
    if pt and metrics.get('scheduler_failure') and r['outcome'] in ('completed', 'noop', 'partial', 'deferred'):
        lines.append('O scheduler registrou falha nesta execução; o resultado funcional acima foi preservado.')
    if pt and _has_invalid_observation(metrics):
        lines.append('Uma observação da execução é inválida; o resultado funcional já comprovado foi preservado.')
    if pt and metrics.get('incident_active'):
        cause = metrics.get('cause')
        if cause == 'cycle_slo_exceeded':
            lines.append(f"O ciclo ainda não terminou: {metrics.get('cycle_age_s', 'desconhecido')} s "
                         f"(limite: {metrics.get('cycle_slo_seconds', 'desconhecido')} s).")
        elif cause == 'no_progress_slo_exceeded':
            lines.append(f"Sem avanço funcional há {metrics.get('no_progress_age_s', 'desconhecido')} s "
                         f"(limite: {metrics.get('dependency_slo_seconds', 'desconhecido')} s).")
        elif cause:
            lines.append('Condição pendente: ' + str(cause))
    if pt and metrics.get('recovered'):
        lines.append('Avanço funcional confirmado; o incidente foi encerrado com evidência.')
    runtime = metrics.get('runtime', {})
    abnormal = [p for p in runtime.get('processes', []) if p.get('reason') in ('wall_deadline', 'cancelled')]
    if pt and abnormal:
        lines.append('O processo foi interrompido por prazo ou cancelamento; o resultado já comprovado foi preservado.')
    progress = r['progress']
    if progress.get('completed') is not None:
        total = f"/{progress['total']}" if progress.get('total') is not None else ''
        lines.append(f"{progress['completed']}{total} {progress.get('unit') or ''}".rstrip())
    continuation = r['continuation']
    if continuation.get('condition'):
        lines.append(('Retomada: ' if pt else 'Continuation: ') + continuation['condition'])
    if continuation.get('eligible_at'):
        lines.append(('Próxima oportunidade: ' if pt else 'Next opportunity: ') + continuation['eligible_at'])
    if r['delivery'].get('status') == 'failed':
        lines.append('Entrega da mensagem falhou; o trabalho não será repetido.' if pt else
                     'Message delivery failed; the work will not be repeated.')
    ref = r['execution_id'] or r['run_id'] or r['cycle_id']
    lines.append(r['observed_at'] + (f' · ref. {ref}' if ref else ''))
    return '\n'.join(lines)


def notification_required(value: dict) -> bool:
    """A causal reminder decision never changes the execution history."""
    result = validate_result(value)
    metrics = result['metrics']
    if _has_invalid_observation(metrics):
        return True
    if any(p.get('reason') in ('wall_deadline', 'cancelled')
           for p in metrics.get('runtime', {}).get('processes', [])):
        return True
    if result['outcome'] in ('failed', 'unknown') and not (metrics.get('incident_active') is True and metrics.get('cause')):
        return True
    if 'notification_due' in metrics:
        return metrics['notification_due']
    return result['outcome'] in ('failed', 'unknown') or metrics.get('incident_active', False)


def write_result(path, value: dict) -> None:
    """Atomic transport plus immutable event history; each observation survives."""
    import hashlib
    import os
    from pathlib import Path
    import tempfile

    result = validate_result(value)
    target = Path(path)
    if target.name != f"{result['execution_id']}.json":
        raise ValueError('receipt destination does not match execution identity')
    data = (json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False)+'\n').encode()
    archive = target.parent/'history'/target.stem
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    archive.parent.mkdir(exist_ok=True, mode=0o700)
    archive.mkdir(parents=True, exist_ok=True, mode=0o700)
    history = archive/(hashlib.sha256(data).hexdigest()+'.json')
    fd, name = tempfile.mkstemp(prefix='.result-', dir=target.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        try:
            os.link(temp, history)
        except FileExistsError:
            if history.read_bytes() != data:
                raise ValueError('receipt history checksum collision')
        # fsync of directories is a POSIX durability boundary; Windows uses
        # atomic replace without claiming a directory-fsync guarantee.
        if os.name == 'posix':
            directory = os.open(archive, os.O_RDONLY)
            try: os.fsync(directory)
            finally: os.close(directory)
        os.replace(temp, target)
        if os.name == 'posix':
            directory = os.open(target.parent, os.O_RDONLY)
            try: os.fsync(directory)
            finally: os.close(directory)
    finally:
        temp.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('validate','build','render'))
    parser.add_argument('--locale', default='en')
    parser.add_argument('--execution-id')
    args = parser.parse_args()
    payload = json.load(sys.stdin)
    value = build_result(**payload) if args.command == 'build' else validate_result(payload, expected_execution_id=args.execution_id)
    print(render_result(value,locale=args.locale) if args.command=='render' else json.dumps(value,ensure_ascii=False,sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
