from datetime import datetime, timezone
from unittest.mock import patch
from cron import jobs


def test_same_clock_outputs_keep_both_artifacts(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME',str(tmp_path))
    monkeypatch.setattr(jobs,'_job_output_dir',lambda _: tmp_path/'job')
    monkeypatch.setattr(jobs,'ensure_dirs',lambda:None)
    with patch('cron.jobs._hermes_now',return_value=datetime(2026,9,8,tzinfo=timezone.utc)):
        first=jobs.save_job_output('job','first outcome')
        second=jobs.save_job_output('job','second outcome')
    assert first != second
    assert first.read_text(encoding='utf-8')=='first outcome'
    assert second.read_text(encoding='utf-8')=='second outcome'


def test_default_output_policy_does_not_prune_incident_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME',str(tmp_path))
    monkeypatch.setattr(jobs,'_job_output_dir',lambda _:tmp_path/'job')
    monkeypatch.setattr(jobs,'ensure_dirs',lambda:None)
    directory=tmp_path/'job';directory.mkdir()
    for index in range(60): (directory/f'old-{index}.md').write_text(f'evidence {index}',encoding='utf-8')
    with patch('hermes_cli.config.load_config',return_value={}):
        jobs.save_job_output('job','new evidence')
    assert len(list(directory.glob('*.md')))==61


def test_installer_defaults_do_not_reintroduce_pruning(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME',str(tmp_path))
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    with patch('hermes_cli.config.load_config',return_value=DEFAULT_CONFIG):
        assert jobs._cron_output_keep()==0
