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

The due scanner treats this opportunity as an absolute instant, including after
restart or a missed tick. Cron-expression repair, timezone migration and the
original one-shot grace window must not discard a functional continuation.
The latest native execution must still be the waiting receipt that requested
it; a newer claimed, running, failed or unknown attempt consumes the old request.
Advancing `next_run_at` before creating an execution does not consume it. This
repairs legacy stores whose next opportunity was incorrectly moved to the next
regular cron occurrence, without editing their historical receipts. Pause,
execution claims, producer admission and failure history remain authoritative.

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

## MCP discovery probes

`hermes mcp test <server> --json` emits this same result contract and saves an
immutable discovery sample and receipt under `state/mcp-probes/<execution_id>`
in the selected Hermes home. It returns 1 for connection/discovery failure,
2 for a missing configured server and 74 for an invalid observation clock.
The human CLI preserves these failure codes as well. A successful handshake
and `tools/list` prove discovery only: `metrics.tool_calls_verified` remains
false. Callers requiring actual search/get-page output must verify those calls.

Nested probes allocate a separate identity and retain the parent's execution
ID as context; they never publish over its `HERMES_RESULT_PATH`. Descriptions,
credentials and configured URLs are excluded from the sample. Failures preserve
the exception type and structured protocol code, with unknown codes left null;
opaque exception text is excluded. The producer hash identifies the exact
adapter used. Probe failure does not imply a credential or provider cause.


## Update receipt continuity

The updater's receipt holds transaction state across the checkout replacement.
It is protected from stale-module eviction together with the executing updater;
lazy imports during restart and finalization must reach that same instance.
Otherwise the restart can occur while its receipt silently disappears. The
regression exercises begin, module purge, restart recording and exactly-once
finalization, preserving the pre-update inventory step and affected process IDs.
All unrelated runtime modules remain eligible for refresh.


## Falhas do scheduler em português

Jobs com `execution_policy.locale = "pt-BR"` registram a observação do scheduler
em `cron/scheduler-failures/<execution_id>.json`, pelo mesmo contrato versionado,
com histórico imutável. O tipo vem do ramo de controle: execução falhou, exceção
do scheduler, configuração recusada ou divergência de configuração de inferência.
Palavras no erro opaco não classificam credencial, provedor ou timeout. Os logs
e o erro técnico da execução continuam separados da mensagem curta.

O leitor do ledger e o renderizador usam essa mesma observação. Sem recibo do
produtor, uma falha deixa o resultado funcional desconhecido; recusa comprovada
antes da chamada ao modelo registra oportunidade não executada. Um script de
contexto anterior continua representado nas observações de processo. Um recibo funcional já
comprovado mantém seu resultado, mesmo se o scheduler falhar depois. A falha de
entrega continua independente e não autoriza repetir efeitos externos. A primeira
observação do scheduler não é substituída por uma nova chamada de renderização.

A próxima oportunidade vem do estado válido da agenda ou do cálculo canônico
`cron.jobs.compute_next_run` para jobs recorrentes habilitados, sempre condicionada
à elegibilidade. Timestamp inválido permanece desconhecido. Campos da política
inválidos ainda impedem a execução, mas não impedem a renderização do diagnóstico
em português. O booleano de `run_one_job` indica que a oportunidade foi tratada;
a conclusão funcional vem exclusivamente do recibo.

Payload genérico, identidade/contrato divergente ou observação inválida não
passam como prova do scheduler. O resultado funcional válido anterior é preservado,
e a perda de observação continua acionável. Não há anúncio de recuperação nem
recomendação automática de pausa no aviso de recorrência. O modo inglês anterior
permanece compatível.

As recusas em português vêm de `record_dispatch_refusal` no ponto real do
preflight e seguem no contexto da execução até o recibo de runtime. Marcadores
como `[blocked_config]` ou `[drift_skip:silent]` copiados para um erro opaco
não comprovam recusa e não silenciam mensagens. Se o próprio verificador da
configuração falhar, a operação Atlas não inicia inferência: registra
`configuration_check_failed`, resultado desconhecido e condição de retomada.
O caminho anterior em inglês permanece compatível.


## Hard cancellation and process birth during observation

On POSIX, a hard cancellation freezes the root and newly discovered descendant
identities before rescanning the tree. A child born during the first process
snapshot is therefore included before termination destroys its ancestry. The
rescan has a two-second/32-pass bound and logs exhaustion; successful signalling
is not proof that an uninterruptible kernel task has exited. Previously detached
processes outside the observed ancestry still require their owning supervisor
or cgroup. Atlas stage wrappers retain their systemd containment and deadlines.

The helper signals identity-checked descendants before the root. A failure to
signal does not leave processes suspended by the helper, and a process already
stopped before cancellation is not resumed. Explicit graceful signals retain
their earlier behavior. Linux regression tests synchronize a real fork during
a process snapshot and simulate signal refusal against only fixture-owned
identities. They use the repository's explicit native-signal test marker: the
default test guard cannot recognize a fixture descendant after reparenting.
