"""Real local commands receive only their own execution receipt destination."""
import os
import sys

import pytest

from cron.functional_results import execution_scope


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX persistent bash snapshot')
def test_reused_terminal_does_not_reuse_another_jobs_receipt_identity(tmp_path):
    from tools.environments.local import LocalEnvironment
    terminal = LocalEnvironment(cwd=str(tmp_path), timeout=10)
    terminal.init_session()
    seen = []
    before = dict(os.environ)
    try:
        @execution_scope(lambda: tmp_path)
        def run(job, *, execution_id):
            result = terminal.execute('printf "%s|%s|%s" "$HERMES_EXECUTION_ID" "$HERMES_JOB_ID" "$HERMES_RESULT_PATH"')
            seen.append(result['output'])
            return True, '', '', None
        run({'id':'one'}, execution_id='aaa')
        run({'id':'two'}, execution_id='bbb')
        outside = terminal.execute('printf "[%s]" "$HERMES_EXECUTION_ID"')['output']
        assert 'aaa|one|' in seen[0] and seen[0].strip().endswith('aaa.json')
        assert 'bbb|two|' in seen[1] and seen[1].strip().endswith('bbb.json')
        assert outside.strip() == '[]'
        assert {k:os.environ.get(k) for k in ('HERMES_EXECUTION_ID','HERMES_JOB_ID','HERMES_RESULT_PATH')} == {
            k:before.get(k) for k in ('HERMES_EXECUTION_ID','HERMES_JOB_ID','HERMES_RESULT_PATH')}
    finally:
        terminal.cleanup()
