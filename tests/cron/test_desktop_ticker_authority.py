"""Desktop fallback respects an existing gateway, including a single profile."""
from pathlib import Path

import pytest


class ControlledStop:
    """Three opportunities: gateway active, absent, then active again."""

    def __init__(self):
        self.phase = 0

    def is_set(self):
        return self.phase >= 3

    def wait(self, _seconds):
        self.phase += 1
        return self.is_set()


@pytest.mark.parametrize('enumeration', ['single', 'empty', 'failed'])
def test_desktop_stands_down_and_takes_over_only_without_gateway(tmp_path, monkeypatch, enumeration):
    from cron.scheduler_provider import InProcessCronScheduler
    from hermes_cli import profiles, web_server
    from hermes_constants import get_hermes_home

    home = tmp_path / '.hermes'
    (home / 'cron').mkdir(parents=True)
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(home))
    clock = ControlledStop()
    gateway_checks = []
    dispatched = []

    def homes(*, multiplex):
        assert multiplex is True
        if enumeration == 'failed':
            raise OSError('profile registry unavailable')
        return [] if enumeration == 'empty' else [('default', home)]

    def gateway_running(observed_home):
        gateway_checks.append((clock.phase, observed_home))
        assert observed_home == home
        return clock.phase != 1

    def tick(**kwargs):
        dispatched.append((clock.phase, get_hermes_home()))
        return 0

    monkeypatch.setattr(profiles, 'profiles_to_serve', homes)
    monkeypatch.setattr(profiles, '_check_gateway_running', gateway_running)
    monkeypatch.setattr('cron.scheduler.tick', tick)
    monkeypatch.setattr('cron.scheduler_provider.resolve_cron_scheduler', InProcessCronScheduler)
    monkeypatch.setattr('hermes_logging.enable_profile_log_routing', lambda homes: None)

    web_server._start_desktop_cron_ticker(clock, interval=1)

    assert dispatched == [(1, home)]
    assert {phase for phase, _ in gateway_checks} == {0, 1, 2}
    # The real provider scopes its bookkeeping to the fixture, never the live home.
    assert (home / 'cron' / 'ticker_last_success').exists()


def test_desktop_does_not_gate_external_scheduler_provider(monkeypatch):
    from cron import scheduler_provider
    from hermes_cli import profiles, web_server

    captured = {}

    class ExternalProvider:
        name = 'external-fixture'

        def start(self, stop, **kwargs):
            captured.update(kwargs)

    def unexpected_probe(*args, **kwargs):
        raise AssertionError('external provider must retain its own authority')

    monkeypatch.setattr(scheduler_provider, 'resolve_cron_scheduler', ExternalProvider)
    monkeypatch.setattr(profiles, 'profiles_to_serve', unexpected_probe)
    monkeypatch.setattr(profiles, '_check_gateway_running', unexpected_probe)
    web_server._start_desktop_cron_ticker(ControlledStop(), interval=7)
    assert captured == {'interval': 7}
