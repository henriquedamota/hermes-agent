from datetime import datetime, timezone
import pytest
from hermes_cli.execution_result import build_result, validate_result, render_result


def result(**kw):
    return build_result(subject_type='stage', job_id='job', execution_id='exec',
                        run_id='run', outcome='completed', exit_code=0,
                        evidence=[{'path':'/evidence/result.json','sha256':None,'role':'functional'}], **kw)


def test_zero_exit_does_not_create_functional_completion():
    r=build_result(subject_type='job',exit_code=0)
    assert r['outcome']=='unknown'
    assert 'não comprovado' in render_result(r,locale='pt-BR')


def test_deferred_zero_never_renders_complete():
    r=build_result(subject_type='stage',outcome='deferred',exit_code=0,
                   reason={'code':'dependency_pending','blocked_stage':'extract'},
                   continuation={'condition':'extract concluído no mesmo ciclo','automatic':True})
    text=render_result(r,locale='pt-BR')
    assert 'aguardando' in text.lower() and 'extract' in text
    assert 'concluído' not in text.splitlines()[0].lower()
    assert 'Recuperado' not in text


def test_exit_75_is_not_inferred_success_or_expected_wait():
    r=build_result(subject_type='stage',exit_code=75)
    assert r['outcome']=='unknown'
    r=result(); r["exit_code"]=75
    with pytest.raises(ValueError):validate_result(r)


def test_noop_requires_measurement_evidence():
    with pytest.raises(ValueError):build_result(subject_type='probe',outcome='noop',exit_code=0)
    assert build_result(subject_type='probe',outcome='noop',exit_code=0,metrics={'pending':0})['outcome']=='noop'


def test_completed_requires_exit_zero_and_evidence():
    with pytest.raises(ValueError):build_result(subject_type='stage',outcome='completed',exit_code=75)
    with pytest.raises(ValueError):build_result(subject_type='stage',outcome='completed',exit_code=0)
    assert result()['outcome']=='completed'


def test_timestamp_without_zone_or_invalid_is_refused():
    for value in ('tomorrow','2026-09-08T10:00:00'):
        with pytest.raises(ValueError):result(started_at=value)


def test_duration_must_be_finite_and_fields_unknown_stay_null():
    for value in (-1,float('nan'),float('inf'),True):
        with pytest.raises(ValueError):result(duration_s=value)
    r=result()
    assert r['duration_s'] is None and r['attempt'] is None and r['cycle_id'] is None


def test_expected_execution_identity_is_required_at_ingestion():
    r=result()
    validate_result(r,expected_execution_id='exec')
    with pytest.raises(ValueError):validate_result(r,expected_execution_id='other')


def test_delivery_failure_does_not_change_functional_outcome():
    r=result()
    r['delivery']={'status':'failed','reason':'network'}
    validate_result(r)
    assert r['outcome']=='completed'
    assert 'entrega' in render_result(r,locale='pt-BR').lower()


def test_future_observation_and_reversed_execution_are_refused():
    with pytest.raises(ValueError):
        result(started_at='2026-09-08T06:01:00Z',finished_at='2026-09-08T06:00:00Z')
    r=result(observed_at='2099-01-01T00:00:00Z')
    with pytest.raises(ValueError):validate_result(r,now=datetime(2026,9,8,tzinfo=timezone.utc))


def test_stage_completion_is_not_rendered_as_cycle_completion():
    from hermes_cli.execution_result import build_result, render_result
    stage=build_result(subject_type='stage',outcome='completed',exit_code=0,
        evidence=[{'path':'durable/receipt'}],metrics={'stage':'extract'})
    message=render_result(stage,locale='pt-BR')
    assert message.startswith('Etapa concluída: extract')
    assert 'Ciclo concluído' not in message
    stage['subject_type']='cycle'
    assert render_result(stage,locale='pt-BR').startswith('Ciclo concluído')


def test_active_incident_is_rendered_with_impact_and_saved_thresholds():
    from hermes_cli.execution_result import build_result, render_result
    value=build_result(subject_type='stage',outcome='deferred',metrics={
        'incident_active':True,'notification_due':True,'cause':'cycle_slo_exceeded',
        'cycle_age_s':22000,'cycle_slo_seconds':21600})
    message=render_result(value,locale='pt-BR')
    assert '21600' in message
    assert '22000' in message
    assert 'ainda não terminou' in message


def test_false_reminder_without_causal_incident_cannot_hide_failure():
    from hermes_cli.execution_result import build_result, notification_required
    value=build_result(subject_type='stage',outcome='failed',exit_code=75,
        reason={'code':'lineage_refused'},metrics={'notification_due':False,'incident_active':False})
    assert notification_required(value)
    value['metrics'].update(incident_active=True,cause='lineage_refused')
    assert not notification_required(value)
