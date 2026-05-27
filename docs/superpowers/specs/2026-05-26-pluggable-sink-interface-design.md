# Pluggable Sink Interface — Design Spec

**Date:** 2026-05-26
**Status:** Revised after review (rev 2) — addresses all P1 findings from `*.review.md`
**Scope:** LLM Governance Gateway — audit log fan-out to observability and SIEM services

---

## Problem

The current plan writes audit entries exclusively to Postgres. Operators running the gateway in production need to forward audit and access log data to observability platforms (Datadog, Grafana, Honeycomb) and SIEM tools (Splunk, Elastic) without modifying gateway internals.

---

## Constraints

- Postgres remains the primary store — external delivery runs alongside it, never replaces it
- At-least-once delivery with retry — no silent discard
- Sink failures must never block or delay a request
- Dependency-free — no external broker; durability via Postgres outbox
- PII compliance: `pii_findings` matched text never leaves the system via any sink — enforced **structurally**, not by convention (see `SanitizedAuditEntry`)
- Per-sink retry independence — a slow or dead sink must not block delivery to a healthy sibling
- Project-wide toolchain: **uv** (packages), **ty** (types), **Ruff** (lint/format), **SQLAlchemy 2.x async** (ORM), **Alembic** (migrations), **Pydantic v2**

---

## Architecture

```
Request Pipeline
      │
      ▼
 write_audit()  ── single Postgres transaction ──▶  audit_log    (existing)
                                                 ▶  sink_queue   (new outbox)

      (transaction committed — request unblocked)

SinkWorker (background asyncio.Task, started by ASGI lifespan, one per process)
      │
      ├── BEGIN; SELECT rows WHERE next_attempt_at <= now() FOR UPDATE SKIP LOCKED
      │   (transaction held for entire batch — lock spans dispatch + update + COMMIT)
      │
      ├── for each row, in parallel (bounded by Semaphore):
      │       entry = SanitizedAuditEntry.from_audit(audit_row)
      │       active = {sink for sink, state in row.sink_state.items()
      │                 if state.status == "pending" and state.next_attempt_at <= now()}
      │       results = SinkDispatcher.dispatch(entry, active)
      │       _update_queue_row(session, row, results)   # jsonb_set per sink
      │
      └── COMMIT; sleep(poll_interval) if batch was empty/partial, else loop immediately
```

---

## Components

### `SanitizedAuditEntry` (the PII boundary)

```python
# governance/app/sinks/entry.py
from pydantic import BaseModel, ConfigDict

class SanitizedAuditEntry(BaseModel):
    """The only payload type any sink ever sees.

    Field allowlist is explicit. `extra="forbid"` makes new columns on
    audit_log fail loudly rather than silently flow to external systems.
    Adding a field here is a code-review gate.
    """
    model_config = ConfigDict(extra="forbid", frozen=True)

    audit_id: UUID
    queue_id: UUID
    occurred_at: datetime
    project_id: str
    actor_id: str | None
    model: str
    request_kind: str
    status_code: int
    latency_ms: int
    input_tokens: int
    output_tokens: int
    pii_finding_count: int   # COUNT only — matched text never crosses this boundary

    @classmethod
    def from_audit(cls, audit_row: AuditLog, queue_id: UUID) -> "SanitizedAuditEntry":
        return cls(
            audit_id=audit_row.audit_id,
            queue_id=queue_id,
            occurred_at=audit_row.occurred_at,
            project_id=audit_row.project_id,
            actor_id=audit_row.actor_id,
            model=audit_row.model,
            request_kind=audit_row.request_kind,
            status_code=audit_row.status_code,
            latency_ms=audit_row.latency_ms,
            input_tokens=audit_row.input_tokens,
            output_tokens=audit_row.output_tokens,
            pii_finding_count=len(audit_row.pii_findings or []),
        )
```

Constructed once per row in the worker. Sinks receive a frozen, allowlisted payload — they cannot reach back to the ORM row. `pii_findings` matched text is never read; only its count is exposed.

### `Sink` Protocol

```python
# governance/app/sinks/base.py
from typing import ClassVar, Protocol

class Sink(Protocol):
    name: ClassVar[str]

    async def send(self, entry: SanitizedAuditEntry) -> None:
        """Deliver one audit entry. Raise on failure — worker handles retry.
        Must honor an external timeout; the dispatcher wraps each call in
        asyncio.wait_for with the configured per-sink timeout.
        """
        ...

    async def close(self) -> None:
        """Flush buffers and close connections on shutdown."""
        ...
```

`ClassVar[str]` for `name` makes the identifier class-level — sinks declare it once and it cannot drift per instance. Concrete implementations duck-type the protocol. No registration magic, no base class.

### `SinkResult` and `SinkDispatcher`

```python
# governance/app/sinks/dispatcher.py
from dataclasses import dataclass

@dataclass(frozen=True, slots=True)
class SinkResult:
    sink_name: str
    error: Exception | None

    @property
    def ok(self) -> bool:
        return self.error is None


class SinkDispatcher:
    def __init__(self, sinks: list[Sink], per_sink_timeout: float = 10.0) -> None:
        self._sinks_by_name = {s.name: s for s in sinks}
        self._timeout = per_sink_timeout

    async def dispatch(
        self,
        entry: SanitizedAuditEntry,
        active: set[str],
    ) -> list[SinkResult]:
        """Call only sinks named in `active`, concurrently. One failure does
        not block siblings. CancelledError propagates — it is not a sink failure.
        """
        targets = [self._sinks_by_name[n] for n in active if n in self._sinks_by_name]

        async def _send(s: Sink) -> SinkResult:
            try:
                await asyncio.wait_for(s.send(entry), timeout=self._timeout)
                return SinkResult(s.name, None)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                return SinkResult(s.name, e)

        return await asyncio.gather(*[_send(s) for s in targets])

    async def close(self) -> None:
        await asyncio.gather(
            *[s.close() for s in self._sinks_by_name.values()],
            return_exceptions=True,
        )
```

`active` is the per-row subset of sinks that are still `pending` AND eligible (`next_attempt_at <= now()`). The worker computes this from `sink_state` before calling `dispatch()` — already-delivered sinks are never re-called, satisfying the at-least-once-per-sink contract. `CancelledError` is re-raised so shutdown signals propagate. Each `send()` is wrapped in its own `wait_for` so a slow sink cannot stall siblings beyond `per_sink_timeout`.

### Outbox Table

```sql
CREATE TABLE sink_queue (
    queue_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    audit_id        UUID NOT NULL REFERENCES audit_log(audit_id),   -- ON DELETE: see operations
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),  -- MIN over per-sink next_attempt_at
    delivered_at    TIMESTAMPTZ,
    failed_at       TIMESTAMPTZ,
    sink_state      JSONB NOT NULL    -- per-sink retry state, see below
);

-- Hot-path index: rows the worker needs to consider
CREATE INDEX sink_queue_pending
    ON sink_queue (next_attempt_at)
    WHERE delivered_at IS NULL AND failed_at IS NULL;

-- Health check index: dead-letter count
CREATE INDEX sink_queue_dead_letter
    ON sink_queue (failed_at)
    WHERE failed_at IS NOT NULL;
```

`sink_state` is seeded at insert time from the set of enabled sinks. Each entry tracks its own retry state:

```jsonc
{
  "otlp":   {"status": "pending",   "retry_count": 0, "next_attempt_at": "2026-05-26T16:00:00Z"},
  "splunk": {"status": "delivered", "retry_count": 2, "next_attempt_at": "2026-05-26T16:00:30Z"}
}
```

`status` is one of `"pending" | "delivered" | "dead" | "disabled"`.

- Row-level `delivered_at = now()` when every sink entry is `"delivered"`.
- Row-level `failed_at = now()` when every sink entry is terminal (`delivered`, `dead`, or `disabled`) AND at least one is `"dead"`.
- Row-level `next_attempt_at` is the MIN of per-sink `next_attempt_at` values across `"pending"` sinks. Updated by `_update_queue_row` after every dispatch.
- A sink removed from config is reconciled to `"disabled"` by a migration step at config-change time (see Operations).

This schema supports per-sink retry independence (P2-1 fix) without proliferating queue rows.

### `_update_queue_row`

```python
# governance/app/sinks/worker.py
from sqlalchemy import func, update
from sqlalchemy.dialects.postgresql import JSONB

_BACKOFF_CAP_S = 3600
_MAX_RETRIES = 10

async def _update_queue_row(
    session: AsyncSession,
    queue_id: UUID,
    results: list[SinkResult],
    now: datetime,
) -> None:
    """Apply per-sink dispatch results to sink_state atomically via jsonb_set.

    Each sink result mutates only its own JSONB subkey, so concurrent updates
    to different sinks on the same row can never lose each other's writes.
    (Within a single batch a row is locked, so the only concurrency to worry
    about is across batches after this transaction commits.)
    """
    # Apply each result with its own jsonb_set; chain via subquery for atomicity
    expr = SinkQueue.sink_state
    for r in results:
        if r.ok:
            new_state = {"status": "delivered", "next_attempt_at": now.isoformat()}
        else:
            # Read current retry_count for this sink (round-trip kept cheap by the row being in-page)
            current = await session.scalar(
                select(SinkQueue.sink_state[r.sink_name]).where(SinkQueue.queue_id == queue_id)
            )
            retry_count = (current or {}).get("retry_count", 0) + 1
            if retry_count >= _MAX_RETRIES:
                new_state = {"status": "dead", "retry_count": retry_count,
                             "next_attempt_at": now.isoformat(),
                             "last_error": type(r.error).__name__}
            else:
                backoff = min(2 ** retry_count, _BACKOFF_CAP_S)
                new_state = {"status": "pending", "retry_count": retry_count,
                             "next_attempt_at": (now + timedelta(seconds=backoff)).isoformat(),
                             "last_error": type(r.error).__name__}
        expr = func.jsonb_set(expr, [r.sink_name], func.to_jsonb(new_state))

    # Single UPDATE: chained jsonb_set + row-level terminal-state recompute
    await session.execute(
        update(SinkQueue)
        .where(SinkQueue.queue_id == queue_id)
        .values(
            sink_state=expr,
            # next_attempt_at = MIN over remaining pending sinks (computed in SQL)
            next_attempt_at=func.coalesce(
                _min_pending_next_attempt(expr),
                func.now() + timedelta(days=365),  # park terminal rows far in the future
            ),
            delivered_at=func.case(
                (_all_sinks_delivered(expr), func.now()), else_=None
            ),
            failed_at=func.case(
                (_all_terminal_with_dead(expr), func.now()), else_=None
            ),
        )
    )
```

`_min_pending_next_attempt`, `_all_sinks_delivered`, `_all_terminal_with_dead` are SQL helper expressions (jsonb_each + filter) — kept in a sibling module. The key point: every write is `jsonb_set` on a single subkey, never a full-column replacement, so concurrent batches on adjacent sinks cannot lose updates (P2-17 fix).

### SinkWorker

```python
# governance/app/sinks/worker.py
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

async def sink_worker(
    session_factory: async_sessionmaker[AsyncSession],
    dispatcher: SinkDispatcher,
    poll_interval: float = 5.0,
    batch_size: int = 50,
    in_flight: int = 25,
) -> None:
    sem = asyncio.Semaphore(in_flight)

    async def _process_row(session: AsyncSession, queue_row, audit_row) -> None:
        async with sem:
            now = datetime.now(timezone.utc)
            active = {
                name for name, st in queue_row.sink_state.items()
                if st.get("status") == "pending"
                and datetime.fromisoformat(st["next_attempt_at"]) <= now
            }
            if not active:
                return
            entry = SanitizedAuditEntry.from_audit(audit_row, queue_row.queue_id)
            results = await dispatcher.dispatch(entry, active)
            await _update_queue_row(session, queue_row.queue_id, results, now)

    while True:
        try:
            async with session_factory() as session, session.begin():
                rows = (await session.execute(
                    select(SinkQueue, AuditLog)
                    .join(AuditLog, SinkQueue.audit_id == AuditLog.audit_id)
                    .where(SinkQueue.delivered_at.is_(None))
                    .where(SinkQueue.failed_at.is_(None))
                    .where(SinkQueue.next_attempt_at <= func.now())
                    .order_by(SinkQueue.next_attempt_at)
                    .limit(batch_size)
                    .with_for_update(skip_locked=True, of=SinkQueue)
                )).all()

                if rows:
                    await asyncio.gather(
                        *[_process_row(session, q, a) for q, a in rows]
                    )
                # session.begin() context commits here, releasing locks AFTER updates land
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("sink_worker batch failed; will retry after poll_interval")

        if len(rows) < batch_size:
            await asyncio.sleep(poll_interval)
```

Five correctness properties, each addressing a P1/P2:

1. **Lock held across dispatch + update** (P1-1 fix). The `session.begin()` block contains SELECT, dispatch, and UPDATE. SKIP LOCKED row locks survive every network call.
2. **`_update_queue_row` is defined and runs inside the transaction** (P1-2 fix).
3. **Dispatcher receives only the `active` subset** — already-delivered sinks are not re-called (P1-3 fix).
4. **`SanitizedAuditEntry.from_audit()` replaces `from_orm`** (P1-4 + P1-5 fix). The PII boundary is structural; Pydantic v2 syntax.
5. **Per-row concurrency via Semaphore** (P2-4 fix). Throughput is bounded by `in_flight`, not by `batch_size × avg_sink_latency`. Adaptive sleep skips the poll wait when the batch was full (P3-13 fix).

Explicit join condition (P2-18 fix). `for_update(of=SinkQueue)` locks only the outbox row, not the joined audit row.

### Built-in Sinks

**OTLP** (`governance/app/sinks/otlp.py`) — covers Datadog, Grafana, Honeycomb, and any OTel-compatible backend. Emits each `SanitizedAuditEntry` as an OpenTelemetry `LogRecord` with audit fields as attributes. Field set is the `SanitizedAuditEntry` allowlist only — no implicit field flow.

**Splunk HEC** (`governance/app/sinks/splunk.py`) — covers Splunk and Elastic with HEC-compatible endpoints. Posts JSON via `httpx.AsyncClient`. Includes `queue_id` as the HEC `event.id` for downstream deduplication (P3-1 fix).

Both sinks:
- Instantiate `httpx.AsyncClient` once in `__init__`, reuse across `send()` calls, close in `close()` (P2-12 fix).
- Hard-code `verify=True`; no config knob to disable TLS verification (P2-15 fix).
- Catch `httpx.HTTPStatusError`, log only `status_code` and `sink.name`, re-raise via `raise SinkError(...) from None` to strip auth-leaking traceback chains (P2-8 fix).
- Let `respond.raise_for_status()` (inside the catch above) drive the worker retry path on non-2xx responses.

### Worker Lifecycle

```python
# governance/app/main.py (ASGI lifespan)
from contextlib import asynccontextmanager
from fastapi import FastAPI

@asynccontextmanager
async def lifespan(app: FastAPI):
    dispatcher = build_dispatcher_from_config(load_config())
    worker_task = asyncio.create_task(
        sink_worker(app.state.session_factory, dispatcher),
        name="sink_worker",
    )
    try:
        yield
    finally:
        worker_task.cancel()
        try:
            await asyncio.wait_for(worker_task, timeout=30.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        await dispatcher.close()   # flushes httpx clients, closes connections
```

Startup: worker is created by the ASGI lifespan. One per process; with N Fly machines, N workers coordinate via SKIP LOCKED.

Shutdown (SIGTERM): the lifespan cancels the worker. The worker's `CancelledError` handler exits the loop; any in-flight transaction rolls back, releasing locks for other workers. `dispatcher.close()` then drains every sink's connection pool. Bounded by a 30s deadline so a stuck sink cannot prevent shutdown.

The worker's own internal logs go to stdout/stderr only — never through a sink (P3-7 fix). This prevents the feedback loop where a failing OTLP sink logs its failure to the OTLP sink.

---

## Configuration

`governance.yaml` gains a top-level `sinks:` block:

```yaml
sinks:
  worker:
    poll_interval_seconds: 5
    batch_size: 50
    in_flight: 25
    per_sink_timeout_seconds: 10

  otlp:
    enabled: true
    endpoint: "${OTLP_ENDPOINT}"
    headers:                      # only allowlisted header names accepted (see below)
      x-honeycomb-team: "${OTLP_API_KEY}"

  splunk:
    enabled: false
    url: "${SPLUNK_HEC_URL}"
    token: "${SPLUNK_HEC_TOKEN}"
    index: "llm_audit"
```

**Validation rules** (enforced by the factory at boot):
- Any `enabled: true` sink with an unresolvable env var fails boot fast (P2-9 fix). No silent skip.
- `headers:` accepts only names matching `^x-[a-z0-9-]+$` or the literal `authorization` (P2-14 fix). Any other key — `Host`, `Forwarded`, undocumented routing headers — rejects at load with a clear error.
- Secrets are env var references only; resolved at load time; never logged. The `/health` and `/metrics` endpoints never echo header values.

Secrets are stored as Fly secrets in production. Adding a new sink requires: implement `Sink` protocol, add a config block, register in the factory — three files touched.

---

## Error Handling

| Condition | Behaviour |
|---|---|
| Sink raises on `send()` | Dispatcher catches, returns `SinkResult(error=e)`. `_update_queue_row` increments `sink_state[sink].retry_count`, sets `next_attempt_at = now() + min(2^retry_count seconds, 3600)` |
| Per-sink `retry_count >= 10` | `sink_state[sink].status = "dead"` — never retried. Row stays in queue; other sinks continue. |
| All sinks terminal, at least one `"dead"` | Row gets `failed_at = now()` — visible to dead-letter health check. No silent discard. |
| All sinks `"delivered"` | Row gets `delivered_at = now()`. Eligible for archival. |
| Sink disabled in config between insert and dispatch | A boot-time reconciliation step marks affected `sink_state` entries as `"disabled"` (terminal, ignored). P2-2 fix. |
| Worker crash / restart | Transaction rolls back; locks release; row picked up by next batch. |
| Slow sink | Bounded by `per_sink_timeout` (default 10s). Treated as failure → retry path. |

`/health` (public) returns `{"degraded": true/false}` only — boolean derived from dead-letter count and oldest-undelivered age (P2-16 fix). Detailed counters live on `/metrics` (authenticated).

`/metrics` exposes:
- `sink_queue_dead_letter_total` (count of `failed_at IS NOT NULL` rows)
- `sink_queue_oldest_pending_seconds` (age of oldest `delivered_at IS NULL AND failed_at IS NULL` row)
- `sink_dispatch_latency_seconds{sink}` histogram
- `sink_send_errors_total{sink, error_type}` counter

---

## Operations

### Deployment ordering (P2-20)

`002_add_sink_queue` migration **must** run before any process with the modified `write_audit()` starts. The new `write_audit()` references the `sink_queue` table; without the table it crashes every request. Deploy order:

1. Apply migration (idempotent, fast — single `CREATE TABLE`).
2. Roll machines to the new code.

For safety, the new `write_audit()` includes a `to_regclass('sink_queue')` check on first invocation per process; if absent, it raises a clear `MigrationNotAppliedError` instead of a confusing FK error.

### Audit retention and the FK (P2-3)

The FK is intentionally `ON DELETE RESTRICT` (the Postgres default). GDPR purges of `audit_log` rows go through a dedicated job that waits for the corresponding `sink_queue` row to reach `delivered_at IS NOT NULL OR failed_at IS NOT NULL` before deleting. This guarantees an audit row is never deleted while delivery is still in flight, and never silently dropped from sinks. The job is out of scope for this spec but is a prerequisite for the audit-retention spec.

### Sink lifecycle (config changes)

Adding a sink:
- New `sink_state` entries are seeded for new rows only. Existing in-flight rows are unaffected (they finish on their original sink set). Backfill to new sinks is out of scope — a future operator CLI will replay from `audit_log`.

Removing a sink:
- A one-shot reconciliation script runs at config-change time:
  ```sql
  UPDATE sink_queue
     SET sink_state = jsonb_set(sink_state, '{removed_sink, status}', '"disabled"')
   WHERE sink_state ? 'removed_sink'
     AND sink_state->'removed_sink'->>'status' = 'pending';
  ```
  This marks rows so the terminal-state check (`all sinks terminal`) can fire. Without this step, rows seeded with the now-removed sink would never reach `delivered_at`.

### Archival (P2-13)

A nightly job deletes `WHERE delivered_at < now() - interval '7 days'`. Dead-letter rows (`failed_at IS NOT NULL`) are retained indefinitely until operators inspect and clear them. Operator inspection tooling is out of scope — `psql` is the v1 interface.

---

## Toolchain

| Concern | Tool | Config |
|---|---|---|
| Package management | `uv` | `pyproject.toml` |
| Type checking | `ty` | `pyproject.toml [tool.ty]` |
| Lint + format | `ruff` | `pyproject.toml [tool.ruff]` |
| Migrations | Alembic | `alembic.ini` |
| ORM | SQLAlchemy 2.x async | — |
| Models | Pydantic v2 | — |
| HTTP | `httpx` (pinned `>=0.27`) | — |
| OTel exporter | `opentelemetry-exporter-otlp-proto-http` (pinned `>=1.27`) — HTTP+protobuf transport, avoids `grpcio` binary surface (P3-2) | — |

CI gates: `ruff check`, `ruff format --check`, `ty check` — all must pass before tests run.

---

## Testing

- **Unit:** `FakeSink` records calls, no network required. Verify:
  - `SinkDispatcher` concurrent dispatch and independent failure isolation
  - `dispatch(entry, active)` only calls sinks in `active`
  - `CancelledError` propagates through `dispatch()` rather than being recorded as a result
  - `_update_queue_row` per-sink jsonb_set semantics and row-level terminal-state transitions
- **PII enforcement:** Property test — `SanitizedAuditEntry.from_audit(any AuditLog)` never produces a payload containing any string from `audit_log.pii_findings[*].matched_text`. Run against both OTLP and Splunk serializers.
- **Worker:** pytest against a real Postgres test DB:
  - Inject sink failures; verify per-sink `retry_count`, backoff, `dead` transition, row-level `failed_at`
  - `SKIP LOCKED` multi-worker safety: start two worker tasks against the same DB; assert no row is double-dispatched (verified via FakeSink call count == 1 per row)
  - Cancellation: cancel mid-batch; assert rows are released and re-picked up cleanly
- **Sink integration:** `respx` for httpx mocking. Verify OTLP attribute mapping, Splunk HEC payload shape, `queue_id` echoed as `event.id`, TLS verify enforced.
- **End-to-end:** One test covers audit write → outbox row created → worker dispatches → sink receives → `delivered_at` set.
- **Performance acceptance** (P3-12): the worker sustains ≥ 500 rows/sec on a Fly `shared-cpu-2x@2048MB` machine against two healthy sinks with p99 send latency ≤ 50ms. Failing this gates a perf-review before merge.

---

## Files Added / Modified

```
governance/
  app/
    sinks/
      __init__.py
      base.py           # Sink Protocol
      entry.py          # SanitizedAuditEntry — the PII boundary
      dispatcher.py     # SinkDispatcher, SinkResult
      worker.py         # sink_worker loop + _update_queue_row
      otlp.py           # OTLPSink
      splunk.py         # SplunkHECSink
      factory.py        # build sinks + dispatcher from governance.yaml config
      reconcile.py      # one-shot script to disable removed sinks in flight
  alembic/versions/
    002_add_sink_queue.py
governance.yaml         # add sinks: block
pyproject.toml          # add httpx, opentelemetry-exporter-otlp-proto-http; pin pydantic v2
```

`write_audit()` in `governance/app/audit.py` is modified to also insert a `sink_queue` row in the same SQLAlchemy session, before commit. The insert seeds `sink_state` from the live enabled-sinks list at the moment of write.

---

## Deferred (not P1, addressed in follow-on specs)

- **P3-5** Batch sinks (`send_batch`) — current `send()` protocol is record-at-a-time; sinks that benefit from batching (Kafka, S3) will need a protocol extension. Out of scope for v1.
- **P3-6** LISTEN/NOTIFY for sub-second fan-out latency — current 5s poll floor is acceptable for SIEM use cases. Revisit if real-time dashboards demand it.
- **P3-8** Per-project sink routing — config is global. Multi-tenant routing is a separate spec.
- **P3-10/11** Dead-letter inspection UI and new-sink backfill CLI — `psql` is the v1 interface.
- **P2-13 archival as a partitioned table** — v1 uses time-bounded DELETE; if write volume crosses 5M rows/day we revisit with `pg_partman`.

---

## Changelog

- **rev 2 (2026-05-26):** Revised after multi-agent review. Resolved all 5 P1 findings (lock scope, `_update_queue_row` definition, per-sink dispatch filtering, `SanitizedAuditEntry` PII boundary, Pydantic v2). Adopted per-sink retry model (P2-1 recommendation): `retry_count`, `next_attempt_at`, terminal status moved into `sink_state` JSONB per sink. Added worker lifecycle section (ASGI lifespan startup, bounded-deadline shutdown, `dispatcher.close()`). Added Operations section covering deployment ordering, FK retention policy, sink config-change reconciliation, archival. Fixed partial-index predicate, per-sink timeouts, `CancelledError` propagation, allowlisted YAML headers, TLS verify enforcement, secret-leak hardening, `/health` boolean degradation. Open P2/P3 items either resolved inline or explicitly deferred.
- **rev 1 (2026-05-26):** Initial spec. Review notes captured in `2026-05-26-pluggable-sink-interface-design.review.md`.
