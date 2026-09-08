"""Read-only, versioned projection of execution history for external readers."""
from __future__ import annotations
import json
from hermes_cli.execution_result import build_result, validate_result, render_result


def execution(record: dict) -> dict:
    ident, job = record['id'], record['job_id']
    try:
        raw = record.get('functional_result_json')
        result = validate_result(json.loads(raw), expected_execution_id=ident) if raw else None
        if result is not None and result['job_id'] != job:
            raise ValueError('receipt belongs to another job')
    except (TypeError, ValueError) as exc:
        result = build_result(subject_type='job', execution_id=ident, job_id=job,
            reason={'code':'invalid_persisted_functional_result','detail':str(exc)})
    if result is None:
        result = build_result(subject_type='job', execution_id=ident, job_id=job,
            reason={'code':'legacy_functional_result_unknown',
                    'detail':'Registro anterior sem recibo funcional; resultado não inferido do processo.'})
    result['delivery']['status'] = record.get('delivery_outcome')
    validate_result(result)
    return {**{key:record.get(key) for key in ('id','job_id','status','source','claimed_at',
              'started_at','finished_at','output_file','error','delivery_outcome')},
            'functional_result':result,'message':render_result(result,locale='pt-BR')}


def job_record(job: dict) -> dict:
    from cron.executions import latest_execution
    latest = latest_execution(job['id'])
    fields = ('id','name','enabled','state','schedule','schedule_display','next_run_at',
              'last_run_at','last_error','last_status','last_fire_error','continuation','last_failure','script','skills','workdir','no_agent','execution_policy')
    return {**{key:job.get(key) for key in fields},
            'latest_execution':execution(latest) if latest else None}
