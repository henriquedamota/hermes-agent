"""Tests for _purge_stale_hermes_modules — the class fix for stale
sys.modules breaking the gateway auto-restart after `hermes update`.

Field failure (2026-08-20, Teknium's Linux box): `hermes update` pulled a
checkout where hermes_cli/gateway.py newly imports `line_input` from
hermes_cli.cli_output, but the updater process had cli_output cached from
before that symbol existed. The function-level `from hermes_cli.gateway
import ...` in the restart phase raised ImportError, the whole phase
aborted, and the running gateway kept serving pre-update code.

The old mitigation (_UPDATE_RUNTIME_RELOAD_MODULES) reloaded 3 hardcoded
modules — re-fixed per symptom. The purge evicts EVERY cached module under
the Hermes package prefixes so later imports rebuild a self-consistent
module graph from the updated checkout.
"""

from __future__ import annotations

import sys
import types
import importlib
import json

import pytest

from hermes_cli import main as cli_main
from hermes_cli import update_cmd


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    """Snapshot & restore sys.modules around each test.

    The purge under test evicts real Hermes modules from the cache; later
    tests in the same process may hold references to the evicted module
    objects (e.g. `patch.object` targets), so put the originals back.
    """
    snapshot = dict(sys.modules)
    yield
    for name, mod in snapshot.items():
        sys.modules[name] = mod
    for name in list(sys.modules):
        if name not in snapshot:
            del sys.modules[name]


def _fake_module(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__stale_sentinel__ = True
    return mod


def test_purge_evicts_hermes_prefixed_modules():
    victims = [
        "hermes_cli.cli_output",
        "hermes_cli.gateway",
        "gateway.status",
        "tools.ansi_strip",
        "tui_gateway.server",
        "agent.memory_store",
    ]
    added = []
    for name in victims:
        if name not in sys.modules:
            sys.modules[name] = _fake_module(name)
            added.append(name)
    try:
        cli_main._purge_stale_hermes_modules()
        for name in victims:
            mod = sys.modules.get(name)
            assert mod is None or not getattr(mod, "__stale_sentinel__", False), (
                f"{name} survived the purge"
            )
    finally:
        for name in added:
            sys.modules.pop(name, None)


def test_purge_protects_executing_modules():
    # The updater's own modules must survive — they're running this code.
    cli_main._purge_stale_hermes_modules()
    assert sys.modules.get("hermes_cli.update_cmd") is update_cmd
    assert sys.modules.get("hermes_cli.main") is cli_main
    assert "hermes_cli" in sys.modules


def test_inflight_update_receipt_survives_purge_and_records_restart(monkeypatch, tmp_path):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    receipt = importlib.import_module('hermes_cli.update_receipt')
    monkeypatch.setattr(receipt, '_receipt_dir', lambda: tmp_path / 'receipts')
    receipt.begin_update_receipt()
    receipt.record_step('pre_update_inventory', True, 'two observed runtimes')
    update_cmd._purge_stale_hermes_modules()
    current = importlib.import_module('hermes_cli.update_receipt')
    current.record_gateway_restart(restarted_services=['hermes-gateway'], killed_pids=[1234])
    path = current.finalize_update_receipt('partial')
    assert path is not None, 'the pre-pull receipt was lost when stale modules were evicted'
    value = json.loads(path.read_text())
    assert value['steps'][0]['name'] == 'pre_update_inventory'
    assert value['gateway_restart']['killed_pids'] == [1234]
    assert value['gateway_restart']['restarted_services'] == ['hermes-gateway']
    assert value['outcome'] == 'partial'
    assert current.finalize_update_receipt('success') is None
    assert json.loads(path.read_text())['outcome'] == 'partial'


def test_purge_leaves_prefix_lookalikes_alone():
    # `gateway_foo` starts with the string prefix "gateway" but is NOT the
    # gateway package — the root-segment check must spare it.
    lookalikes = ["gatewayd", "toolshed", "agents_external"]
    added = []
    for name in lookalikes:
        if name not in sys.modules:
            sys.modules[name] = _fake_module(name)
            added.append(name)
    try:
        cli_main._purge_stale_hermes_modules()
        for name in lookalikes:
            assert name in sys.modules, f"{name} was wrongly purged"
    finally:
        for name in added:
            sys.modules.pop(name, None)


def test_purge_never_raises_on_weird_sys_modules():
    # Entries with None values (import machinery quirk) must not break it.
    sys.modules["hermes_cli._purge_test_none"] = None  # type: ignore[assignment]
    try:
        cli_main._purge_stale_hermes_modules()
    finally:
        sys.modules.pop("hermes_cli._purge_test_none", None)


def test_stale_symbol_scenario_end_to_end():
    """Reproduce the field failure shape: a cached module missing a symbol
    that freshly-imported code needs — purge, then re-import resolves it."""
    name = "hermes_cli.cli_output"
    real = sys.modules.get(name)
    # Install a stale stand-in WITHOUT line_input (pre-d0132b582 world).
    stale = types.ModuleType(name)
    sys.modules[name] = stale
    try:
        # The failure mode: importing the symbol from the stale cache dies.
        try:
            from hermes_cli.cli_output import line_input  # noqa: F401
            raised = False
        except ImportError:
            raised = True
        assert raised, "precondition: stale module must lack line_input"

        cli_main._purge_stale_hermes_modules()

        # Post-purge, the import resolves against real on-disk source.
        from hermes_cli.cli_output import line_input  # noqa: F401
    finally:
        sys.modules.pop(name, None)
        if real is not None:
            sys.modules[name] = real
