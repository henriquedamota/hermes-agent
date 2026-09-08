"""History and message delivery remain independently queryable after reopen."""
import sqlite3

from cron import executions


def test_completed_work_with_failed_delivery_keeps_both_facts(tmp_path, monkeypatch):
    path = tmp_path / "cron/executions.db"
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", path)
    record = executions.create_execution("delivery-failure", source="builtin")
    executions.mark_execution_running(record["id"])
    executions.finish_execution(record["id"], success=True, delivery_outcome="failed")
    persisted = executions.get_execution(record["id"])
    assert persisted["status"] == "completed"
    assert persisted["delivery_outcome"] == "failed"
    with sqlite3.connect(path) as independent_reader:
        assert independent_reader.execute(
            "SELECT status,delivery_outcome FROM executions WHERE id=?", (record["id"],)
        ).fetchone() == ("completed", "failed")
    assert executions.finish_execution(record["id"], success=False) is None
    assert executions.get_execution(record["id"]) == persisted


def test_absent_delivery_evidence_stays_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "cron/executions.db")
    record = executions.create_execution("legacy", source="builtin")
    executions.finish_execution(record["id"], success=True)
    assert executions.get_execution(record["id"])["delivery_outcome"] is None


def test_old_ledger_migrates_without_inventing_delivery(tmp_path, monkeypatch):
    path = tmp_path / "cron/executions.db"
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", path)
    record = executions.create_execution("old-row", source="builtin")
    executions.finish_execution(record["id"], success=True)
    with sqlite3.connect(path) as old_schema:
        old_schema.execute("ALTER TABLE executions DROP COLUMN delivery_outcome")
    migrated = executions.get_execution(record["id"])
    assert migrated["status"] == "completed"
    assert migrated["delivery_outcome"] is None
    assert migrated["claimed_at"] == record["claimed_at"]


def test_default_retention_does_not_silently_erase_the_1001st_record(tmp_path, monkeypatch):
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "cron/executions.db")
    oldest = executions.create_execution("original-failure", source="builtin")
    executions.finish_execution(oldest["id"], success=False, error="preserved cause")
    saved = executions.get_execution(oldest["id"])
    # Seed a realistic full ledger without 1,000 redundant schema migrations.
    with executions._transaction() as conn:
        conn.executemany(
            """INSERT INTO executions
               (id,job_id,source,process_id,pid,status,claimed_at,finished_at)
               VALUES (?, 'fixture', 'builtin', 'previous', 1, 'completed', ?, ?)""",
            [(f"fixture-{i}", "2030-01-01T00:00:00+00:00", "2030-01-01T00:00:00+00:00")
             for i in range(1000)],
        )
    current = executions.create_execution("current", source="builtin")
    executions.finish_execution(current["id"], success=True, delivery_outcome="suppressed")
    assert executions.get_execution(oldest["id"]) == saved
    with executions._transaction() as conn:
        assert conn.execute("SELECT count(*) FROM executions").fetchone()[0] == 1002
