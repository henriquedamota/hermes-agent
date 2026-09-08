

def test_content_receipt_preserves_verified_body_and_delivery_failure():
    import hashlib
    from hermes_cli.execution_result import build_result, render_result
    body='Resumo publicado com fonte verificável.'
    digest=hashlib.sha256(body.encode()).hexdigest()
    receipt=build_result(subject_type='job',outcome='completed',execution_id='editorial',exit_code=0,
        evidence=[{'path':'/isolated/briefing.md','sha256':digest,'role':'message_body'}],
        presentation={'body':body,'sha256':digest})
    receipt['delivery']['status']='failed'
    rendered=render_result(receipt,locale='pt-BR')
    assert body in rendered
    assert 'não será repetido' in rendered


def test_content_without_matching_evidence_and_malformed_runtime_are_rejected():
    import pytest
    from hermes_cli.execution_result import build_result
    with pytest.raises(ValueError):
        build_result(subject_type='job',outcome='completed',exit_code=0,
            evidence=[{'path':'/isolated/file'}],presentation={'body':'unverified','sha256':'a'*64})
    with pytest.raises(ValueError):
        build_result(subject_type='job',metrics={'runtime':{'processes':None}})
