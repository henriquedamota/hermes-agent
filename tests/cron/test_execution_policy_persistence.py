"""Execution budgets must survive CLI updates and store reload, not just documentation."""
import argparse
import json
from unittest.mock import patch

import pytest
from cron import jobs
from hermes_cli.cron import cron_edit


@pytest.fixture
def store(tmp_path,monkeypatch):
    monkeypatch.setenv('HERMES_HOME',str(tmp_path))
    monkeypatch.setattr(jobs,'JOBS_FILE',tmp_path/'jobs.json')
    monkeypatch.setattr(jobs,'CRON_DIR',tmp_path)
    return jobs.create_job('isolated task','0 12 * * *',deliver='local')


def test_invalid_policy_does_not_mutate_job(store):
    for policy in ({'wall_timeout_seconds':0},{'wall_timeout_seconds':float('nan')},{'functional_result':'assume-completed'},{'legacy_cpu_quota':25}):
        with pytest.raises(ValueError):jobs.update_job(store['id'],{'execution_policy':policy})
    assert 'execution_policy' not in jobs.get_job(store['id'])


def test_policy_is_persisted_and_not_removed_by_unrelated_update(store):
    policy={'wall_timeout_seconds':900,'locale':'pt-BR','functional_result':'required'}
    jobs.update_job(store['id'],{'execution_policy':policy})
    jobs.update_job(store['id'],{'name':'updated name'})
    assert jobs.get_job(store['id'])['execution_policy']==policy
    assert json.loads(jobs.JOBS_FILE.read_text())['jobs'][0]['execution_policy']==policy


def test_cli_edit_forwards_policy_through_official_api(store):
    policy={'wall_timeout_seconds':600,'functional_result':'optional'}
    args=argparse.Namespace(job_id=store['id'],execution_policy=policy)
    with patch('hermes_cli.cron._cron_api',return_value={'success':True,'job':{'job_id':store['id'],'name':'test','schedule':'hourly'}}) as api:
        assert cron_edit(args)==0
    assert api.call_args.kwargs['execution_policy']==policy
