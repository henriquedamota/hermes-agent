# Functional results and scheduled opportunities

Hermes distinguishes a scheduled opportunity, a process, a producer's functional
result, and delivery. A completed process does not prove completed work. The
public, dependency-free authority is `hermes_cli/execution_result.py`, contract
`hermes.execution-result/v1`. Unknown values are null; historical records without
a receipt remain `unknown` even when their process exited successfully.

## Producer boundary

A job opts into `execution_policy.functional_result = "required"` through the
normal cron create/edit API. Its script and terminal commands receive isolated
`HERMES_JOB_ID`, `HERMES_EXECUTION_ID`, and `HERMES_RESULT_PATH` values. These are
per-execution context, never shared process environment or restored shell state.
The producer uses `build_result` and `write_result`; the latter writes an atomic
transport plus immutable, content-addressed history. IDs, timezone-aware times,
finite numbers, status, evidence, and destination identity are validated. The
scheduler rejects missing or foreign receipts for required jobs.

| Outcome | Meaning |
|---|---|
| `completed` | Producer proves its subject finished; exit zero and evidence required. |
| `noop` | Producer measured that no work was needed; measurement/evidence required. |
| `skipped` | Opportunity was not executed. No inferred completion or recovery. |
| `deferred` | A prerequisite or admission condition prevented completion. |
| `partial` | Work remains and the producer preserves the completed portion. |
| `failed` | A real refusal, deadline, or execution failure, with a structured cause. |
| `unknown` | Available evidence does not prove the functional result. |

`subject_type` identifies job, stage, cycle, worker, probe, or maintenance. An
individual stage's completed receipt must not be rendered as a completed cycle.
The schema also carries code/data revisions, attempt/run/cycle identity,
progress, observed metrics, applied policy, evidence and continuation. Producers
remain responsible for verifying their domain outputs and for idempotent resume.
A sidecar cannot authorize replay of an already completed external effect.

## Runtime, history, and delivery

`cron/executions.py` preserves process status, canonical receipt, exact output
file, and delivery outcome separately. Output location and delivery observation
are written under ownership before terminal teardown, so an interruption cannot
erase an already delivered result. Terminal writes use the existing execution
and fire-claim fences. Runtime observations record actual subprocess return
codes, cancellation/deadline reasons and measured duration independently of the
producer. Missing process IDs remain unknown.

The default ledger and text-output retention is unlimited. Explicit finite
retention remains an operator policy. Output names include a random identity
suffix, preventing same-clock overwrite; explicit pruning uses real modification
time, not UUID lexical order. Receipts retain all observed content versions.

Readers use `hermes cron runs --json` (`hermes.execution-history/v1`) and
`hermes cron list --all --json` (`hermes.job-list/v1`), not text greps or nearby
log timestamps. History uses a persisted monotonic sequence and a
`next_before_sequence` cursor, preserving creation order through clock changes,
timezone changes, database compaction and retained legacy rows. Delivery failure remains visible and does not schedule work
again. `last_status` uses the functional outcome for migrated jobs; older jobs
retain their legacy process status, with unknown functional state in JSON.

## Continuation and capacity

A validated `deferred` or `partial` receipt may request `continuation.automatic`
and a timezone-aware `eligible_at`. Authenticated completion persists the earlier
of that opportunity and the regular schedule, with a minimum 60-second delay for
past eligibility. The original regular opportunity and originating execution
remain recorded. This does not pause or replace the recurring schedule. A
completed/noop/failed receipt or a delivery failure never requests generic replay.
Expected waits preserve the failure streak and do not consume the completion
counter. Only verified completed/noop heals a migrated job's streak.

`execution_policy.wall_timeout_seconds` sets a monotonic per-execution wall
budget across script and agent. Heartbeat activity cannot reset it. Inactivity
limits, agent/tool deadlines, memory protection, process-tree cancellation and
ownership remain separate. Shutdown or timeout recollects subprocess descendants,
including children in another POSIX session, through the runtime's deadline API.

`cron.execution_lanes` configures named, bounded pools inside the existing
scheduler. Per-job `execution_policy.lane` chooses a pool; `priority` orders
arrivals within a tick. Older queued work keeps FIFO position. Pools are local
to the scheduler process, not a cross-process resource lock. Domain writers must
still hold their shared exclusion/claims. Changing live pool capacity requires
a drained scheduler restart. Invalid lane configuration leaves the opportunity
due, persists its refusal and does not prevent valid jobs from dispatching.

## Notifications

`locale = "pt-BR"` uses Portuguese functional titles and a short job reference.
`notification_mode = "exceptions"` keeps measured nominal activity in the
ledger without sending routine messages. A producer's persistent causal tracker
may supply boolean `notification_due`, `incident_active`, and `recovered` metrics
with its preserved incident evidence. This controls reminders, not execution
history. New runtime cancellation/deadline evidence remains actionable even
when the producer requested deduplication. Recovery requires the producer's
functional proof; a successful query or preflight is not recovery.

Tests in `tests/cron/test_*result*`, `test_functional_continuation.py`,
`test_dispatch_lanes.py`, and `test_output_evidence_history.py` exercise the
actual subprocess boundary, canonical validation, store reload, interrupted
teardown, independent delivery, clock ordering, explicit continuation and
reserved observation capacity. Run them with `scripts/run_tests.sh`.

Content-producing jobs may include `presentation.body` and its SHA-256. The
receipt must reference immutable `message_body` evidence with that exact hash.
The canonical renderer preserves this verified content together with the
functional outcome, trace and delivery failure. This lets briefings/editorial
jobs deliver their actual artifact without interpreting arbitrary stdout or
repeating completed work after transport failure. Legacy receipts without this
optional field remain valid. Runtime process observations are validated before
rendering and cannot crash a notification through a malformed nested payload.
