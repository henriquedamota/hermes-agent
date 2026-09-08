# Cron execution evidence

`cron/executions.py` owns the profile-local SQLite audit ledger. A scheduled
opportunity, process execution, functional work result and message delivery are
different facts. The existing `status` describes execution; it must not be
presented as proof that an application pipeline cycle completed.

Terminal execution rows remain immutable and are retained without a count cap
by default. The previous implicit 1,000-row limit could erase incident evidence
as normal ticks arrived. An explicit internal `MAX_TERMINAL_EXECUTIONS` override
still supports bounded stores, but its deletion policy must be chosen by the
operator; it is not the default. Pagination bounds query results independently
of retention.

`delivery_outcome` is now persisted alongside terminal execution status:
`delivered`, `failed`, `not_configured`, `suppressed`, or `suppressed_acked`.
Absent evidence remains SQL NULL, including rows from older schemas. Additive
migration does not infer delivery from an execution's success. Completed work
with failed delivery remains completed work; this field does not enqueue a
second execution or authorize repeating external effects.

Regression coverage in `tests/cron/test_ledger_preservation_delivery.py` uses a
real SQLite ledger, an independent reader, old-schema migration and a populated
ledger beyond 1,000 rows. Delivery is simulated; no external messages are sent.
The quick updater snapshot includes this database. Code rollback must preserve
the newer database and its evidence; it must not restore an older snapshot over
live execution history.
