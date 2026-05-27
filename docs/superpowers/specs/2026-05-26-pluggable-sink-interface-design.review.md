# Code Review — Pluggable Sink Interface Design Spec

**Spec:** `2026-05-26-pluggable-sink-interface-design.md`
**Review date:** 2026-05-26
**Verdict:** **Not ready for implementation.** Five P1 defects must be resolved.

Six specialized agents reviewed the spec in parallel: architecture, security, performance, data integrity, spec-flow completeness, and Python code quality. Findings below are consolidated and de-duplicated. Where multiple agents reached the same finding independently, that is noted — high consensus = high confidence.

---

## 🔴 P1 — Blocks implementation

### P1-1 — SKIP LOCKED lock released before dispatch (5/6 agents)

**Architecture · Performance · Data Integrity · Spec Flow · Python**

```python
async with session_factory() as session:
    rows = await session.execute(... .with_for_update(skip_locked=True))
# <-- session.__aexit__ runs HERE; transaction commits; ALL row locks released

for queue_row, audit_row in rows:                          # outside the transaction
    results = await dispatcher.dispatch(entry)
    await _update_queue_row(session, queue_row, ...)       # closed session → InvalidRequestError
```

Two compounding failures in one block:

1. **Lock released before dispatch.** The spec's central correctness claim — *"`SKIP LOCKED` makes the worker safe to run on multiple Fly.io machines without double-delivery"* — is false as written. Once `async with` exits, any other worker can immediately re-acquire the same rows.
2. **Dead session used after exit.** `_update_queue_row(session, ...)` is called on a session whose transaction has already ended. SQLAlchemy 2.x raises `InvalidRequestError`. Rows are claimed and then never updated — silent data loss.

**Fix:** Move the entire `for` loop *and* the `_update_queue_row` calls inside the `async with session_factory()` block. The transaction must wrap SELECT → dispatch → UPDATE. Row locks intentionally held during network I/O — this is the correct outbox pattern.

---

### P1-2 — `_update_queue_row` is undefined

**Spec Flow · Python**

Line 143 calls `_update_queue_row(session, queue_row, results, max_retries)`. This function is referenced but not specified anywhere in the spec. It is the load-bearing piece that implements:

- per-sink state writes into `sink_state` JSONB
- retry-count increment
- exponential backoff calculation for `next_attempt_at`
- transition to `delivered_at` (all sinks done) or `failed_at` (max retries hit)
- the "partial delivery / skip already-delivered sinks" logic

Without its specification, the spec describes none of the behavior the error-handling table promises.

**Fix:** Add an `_update_queue_row` implementation to the spec — at minimum the SQL it issues (`jsonb_set(sink_state, ...)`, not full-column replacement; see P1-5) and the per-sink vs. per-row decision for `retry_count`/`failed_at` (see P2-1).

---

### P1-3 — Dispatcher unconditionally re-delivers already-delivered sinks

**Data Integrity**

The error-handling table promises: *"Delivered sinks skipped on next attempt; only failed sinks retried."*

`SinkDispatcher.dispatch()` does not honor this:

```python
async def dispatch(self, entry: AuditEntry) -> dict[str, Exception | None]:
    results = await asyncio.gather(*[s.send(entry) for s in self._sinks], ...)
```

The dispatcher receives no `sink_state` and cannot know which sinks have already delivered. On every retry attempt, every sink fires again. This silently converts **at-least-once per row** into **at-least-once-per-attempt per sink** — duplicates for the sink that succeeded on attempt 1 every time the row is retried for a sibling sink.

**Fix:** Change the dispatcher signature to accept the set of already-delivered sink names: `dispatch(entry, skip: set[str])`. Filter `self._sinks` before the `gather`. Document this in the protocol.

---

### P1-4 — PII leak guarantee is implicit, not structural (OTLP)

**Security**

The PII constraint is global: *"`pii_findings` matched text never leaves the system via any sink."*

- Splunk section honors it explicitly: *"`pii_findings` excluded from payload."*
- OTLP section says only: *"audit fields as attributes."*

The OTLP author and every future sink author must independently remember the rule. There is no shared enforcement point. The pattern guarantees the constraint *will* be violated by omission eventually.

Compounding the risk, `AuditEntry.from_orm(audit_row)` reflects every column on `audit_log`. Any new column added later — raw prompt snapshot, token buffer, debug field — automatically flows into every sink unless the implementer remembers to exclude it. Allowlist absent.

**Fix:** Add a `SanitizedAuditEntry` type (or `AuditEntry.to_sink_payload()` method) that strips `pii_findings` and any other sensitive fields. The `Sink` protocol accepts only the sanitized type. Define `AuditEntry` with an explicit field allowlist and `model_config = ConfigDict(extra="forbid")`. Both Splunk and OTLP integration tests must assert the exclusion.

---

### P1-5 — `from_orm` is Pydantic v1 syntax — runtime crash on v2

**Python**

```python
entry = AuditEntry.from_orm(audit_row)
```

Pydantic v2 removed `from_orm`. The correct call is:

```python
entry = AuditEntry.model_validate(audit_row, from_attributes=True)
```

The toolchain (SQLAlchemy 2.x + modern uv/ty stack) almost certainly uses Pydantic v2. This is a `AttributeError` on the first row processed.

**Fix:** Update spec to `model_validate(..., from_attributes=True)`. Pin Pydantic v2 in `pyproject.toml`.

---

## 🟡 P2 — Should fix before implementation

### P2-1 — `retry_count` / `failed_at` are per-row, not per-sink

**Architecture · Data Integrity**

The schema has single columns: `retry_count`, `next_attempt_at`, `failed_at`. But the error-handling table promises partial-delivery semantics ("only failed sinks retried"). With per-row counters, a fast-failing OTLP exhausts the row's `retry_count` and trips `failed_at`, killing delivery for a still-viable Splunk that's only on retry 3.

**Fix:** Move `retry_count` / `next_attempt_at` / `dead` state *inside* `sink_state` JSONB as per-sink fields. A row is `delivered` when all sinks are `delivered`; `failed_at` is set only when all sinks are terminal (delivered or dead).

---

### P2-2 — Sink lifecycle change strands rows forever

**Architecture · Data Integrity · Spec Flow**

`sink_state` is seeded at row creation from the current enabled-sinks list. If a sink is disabled between insert and dispatch, the row's `sink_state` still contains the now-disappeared key. The "all delivered" completion check never fires. Row sits in the queue indefinitely — not failed, not delivered, invisible to dead-letter health metrics.

**Fix:** Completion check must treat keys absent from the active sink list as `disabled` (terminal). Add a migration step when removing a sink to mark in-flight `pending` rows for that sink as `disabled`.

---

### P2-3 — Foreign key has no `ON DELETE` clause — GDPR purge blocked

**Security · Data Integrity**

```sql
audit_id UUID NOT NULL REFERENCES audit_log(audit_id)
```

Default is `RESTRICT`. A GDPR right-to-erasure delete of an `audit_log` row fails as long as `sink_queue` rows reference it. `ON DELETE CASCADE` would silently drop undelivered rows (violates "no silent discard"). `ON DELETE SET NULL` requires a nullable `audit_id` and a worker rule.

**Fix:** Specify the policy. Recommended: keep `RESTRICT`, and gate audit purges through a job that waits for `delivered_at IS NOT NULL OR failed_at IS NOT NULL` before deleting from `audit_log`. Document this contract.

---

### P2-4 — Sequential entry dispatch creates throughput cliff

**Architecture · Performance**

The worker processes 50 entries sequentially. At 500ms p99 per sink call, batch wall time is ~25s — 5× the `poll_interval`. The worker perpetually falls behind under sustained load.

**Fix:** After fixing P1-1 (lock scope), gather across entries too — `asyncio.gather(*[process_row(r) for r in rows])`. Bound with a `Semaphore` to limit in-flight count. Drops worst-case batch time to ~500ms.

---

### P2-5 — No per-sink timeout; slow sink holds the batch

**Architecture**

`asyncio.gather` per entry waits for the slowest sink. A 30s OTLP timeout holds the row lock (after P1-1 fix) for the full 30s. With batch_size=50, one bad sink starves 50 rows × 30s.

**Fix:** Make timeout part of the `Sink` protocol contract. Either require each `send()` to honor an `asyncio.wait_for`, or have `SinkDispatcher` wrap each call.

---

### P2-6 — `return_exceptions=True` silently swallows `CancelledError`

**Python**

```python
asyncio.gather(*coros, return_exceptions=True)
```

`return_exceptions=True` catches `BaseException`, including `CancelledError`. Shutdown signals get recorded as sink failures and the worker keeps spinning instead of cancelling.

**Fix:**

```python
results = await asyncio.gather(*coros, return_exceptions=True)
for r in results:
    if isinstance(r, BaseException) and not isinstance(r, Exception):
        raise r
```

---

### P2-7 — Worker lifecycle / shutdown unspecified

**Spec Flow**

- Who creates the `sink_worker` task? ASGI lifespan? Separate process entrypoint?
- `Sink.close()` is in the protocol — who calls it on SIGTERM?
- Does graceful shutdown await in-flight dispatches before calling `close()`?

Without this, httpx connections leak and OTLP buffered spans are dropped on every Fly machine restart.

**Fix:** Specify lifecycle. Tie startup to ASGI lifespan; on shutdown, cancel the worker task, await in-flight dispatches up to a deadline, then call `close()` on every sink.

---

### P2-8 — `raise_for_status()` exception traces can leak auth tokens

**Security**

`httpx.HTTPStatusError` includes the request URL and, in some configurations, request headers — where `Authorization: Splunk ${SPLUNK_HEC_TOKEN}` and `x-honeycomb-team: ${OTLP_API_KEY}` live. A default `logger.exception(e)` writes them to logs.

**Fix:** Each sink catches `httpx.HTTPStatusError`, logs only `status_code` + `sink.name`. Re-raise via `raise NewException(...) from None` to strip the chained traceback.

---

### P2-9 — Config validation behavior unspecified

**Spec Flow**

`enabled: true` with a missing `${OTLP_ENDPOINT}` is undefined. Silent skip violates the at-least-once contract; lazy fail violates fast-feedback.

**Fix:** Fail fast at startup if any required field is unresolvable for an enabled sink. State this explicitly.

---

### P2-10 — Partial index predicate mismatch

**Performance**

```sql
CREATE INDEX ON sink_queue (next_attempt_at) WHERE delivered_at IS NULL;
```

Worker queries also filter on `failed_at IS NULL`. The partial index returns dead-letter rows that the executor must then discard. Degrades as the dead-letter backlog grows.

**Fix:** `WHERE delivered_at IS NULL AND failed_at IS NULL`.

---

### P2-11 — Health-check queries lack supporting indexes

**Performance**

`COUNT(*) WHERE failed_at IS NOT NULL` and `MIN(created_at) WHERE delivered_at IS NULL AND failed_at IS NULL` become sequential scans on a growing table.

**Fix:** Add `CREATE INDEX ON sink_queue (failed_at) WHERE failed_at IS NOT NULL` and include `created_at` in the corrected partial index.

---

### P2-12 — httpx.AsyncClient lifecycle not defined

**Performance**

If sinks instantiate a new `AsyncClient` per `send()`, each call eats a TCP+TLS handshake (~20–200ms) and leaks file descriptors.

**Fix:** Instantiate once in `__init__`, close in `close()`. Document in the spec.

---

### P2-13 — No table archival / unbounded queue growth

**Architecture · Performance**

Delivered rows accumulate forever. At 1M audits/day, ~7M rows/week. Index maintenance and vacuum cost grow without bound.

**Fix:** Specify a nightly purge of `WHERE delivered_at < now() - interval '7 days'` (or weekly partitioning by `created_at`). Make it part of the spec, not an afterthought.

---

### P2-14 — Header injection via freeform YAML

**Security**

`headers:` is an unconstrained map. A compromised config or operator typo can inject `Authorization:` for a different destination, `Host:` overrides, or exfiltration headers.

**Fix:** Allowlist permitted header names (vendor `x-*` prefixes and `authorization` only). Reject routing-affecting headers.

---

### P2-15 — TLS verification not specified

**Security**

`httpx.AsyncClient()` defaults to `verify=True`, but the factory could quietly accept `verify=False`. Spec is silent.

**Fix:** Mandate `verify=True` non-configurably. Add a CI test asserting `client.verify is not False` for every sink.

---

### P2-16 — `/health` exposes dead-letter counts unauthenticated

**Security**

Raw counts on an unauthenticated endpoint give attackers an operational side-channel.

**Fix:** Public `/health` returns a boolean (`degraded: true/false`). Move raw metrics to an authenticated `/metrics` endpoint.

---

### P2-17 — JSONB updates must use `jsonb_set`, not column replacement

**Data Integrity**

A naive `UPDATE sink_queue SET sink_state = $1` (Python-merged dict) produces lost updates. Required pattern: `UPDATE ... SET sink_state = jsonb_set(sink_state, '{otlp}', '"delivered"')`.

**Fix:** Spec must show the SQL pattern in `_update_queue_row`.

---

### P2-18 — `session_factory` untyped; `.join(AuditLog)` lacks ON

**Python**

```python
async def sink_worker(session_factory, ...) -> None:
```

`ty` can't check ORM calls. And `select(SinkQueue, AuditLog).join(AuditLog)` relies on FK inference — breaks the moment a second FK exists.

**Fix:** Annotate `session_factory: async_sessionmaker[AsyncSession]`. Make joins explicit: `.join(AuditLog, SinkQueue.audit_id == AuditLog.audit_id)`.

---

### P2-19 — `Exception | None` return type is too broad; `name: str` should be `ClassVar`

**Python**

`dict[str, Exception | None]` loses information. `name: str` permits per-instance variation that doesn't match how sinks are actually used.

**Fix:** Introduce `SinkResult` dataclass. Use `name: ClassVar[str]` in the protocol.

---

### P2-20 — Migration / code deployment ordering not specified

**Data Integrity · Spec Flow**

Deploying the modified `write_audit()` before the `002_add_sink_queue` migration runs causes every audit write to fail with `relation "sink_queue" does not exist` — blocking all requests (violates the "never block a request" constraint).

**Fix:** Spec must call out the migration→code order. Optionally add a feature flag in `write_audit()` for the deploy window.

---

## 🔵 P3 — Nice to have

| # | Finding | Source |
|---|---------|--------|
| P3-1 | Splunk HEC has no idempotency key — at-least-once → duplicate events. Include `queue_id` as `event.id` | Security |
| P3-2 | `opentelemetry-exporter-otlp` unpinned; prefer `*-proto-http` variant to avoid `grpcio` binary surface | Security |
| P3-3 | Sink names treated as stable IDs but not enforced — renames silently corrupt `sink_state` reconciliation | Data Integrity |
| P3-4 | Audit row immutability not enforced — concurrent writes between SELECT and dispatch are undefined | Data Integrity |
| P3-5 | `send()` is record-at-a-time; future batch sinks (Kafka) must buffer internally | Architecture |
| P3-6 | 5s poll latency floor; LISTEN/NOTIFY hybrid reduces to <100ms if real-time needed | Performance |
| P3-7 | Worker logs to OTLP sink → feedback loop risk; mandate stdout-only for worker internal logs | Spec Flow |
| P3-8 | Multi-tenant per-project sink routing out of scope — state explicitly | Spec Flow |
| P3-9 | `AuditEntry` schema versioning story absent — downstream sink payloads change silently when columns added | Spec Flow |
| P3-10 | Dead-letter remediation UX absent — no inspect/retry/purge interface for operators | Spec Flow |
| P3-11 | Replay/backfill story for newly-added sinks absent | Spec Flow |
| P3-12 | Performance acceptance criteria (rows/sec target) missing from Testing section | Spec Flow |
| P3-13 | Adaptive sleep — don't sleep `poll_interval` after a full batch | Python |

---

## Cross-cutting themes

1. **The worker is the weakest part of the spec.** Five of six agents independently flagged the lock-release bug. The combination of P1-1, P1-2, P1-3, P1-5 means the worker as written cannot run correctly even once.

2. **PII safety relies on implementer memory.** P1-4 and P2-15 share a root cause: the design hands a fully-populated domain object to sink authors and trusts them to remember the rules. A `SanitizedAuditEntry` boundary type eliminates both classes of bug.

3. **Per-sink semantics are claimed but not modeled.** The error-handling table promises per-sink retry / skip-delivered, but the schema (single `retry_count`/`failed_at` columns) and dispatcher (calls every sink) don't support it. P1-3 and P2-1 are two faces of the same gap.

4. **Operational concerns are undersized.** Health, archival, dead-letter remediation, lifecycle, and deployment ordering are mentioned in passing or not at all. Each will surface during the first production incident.

---

## Recommended path forward

1. **Resolve all five P1s in a spec revision.** None require redesign — they're correctness gaps in the worker code and protocol shape.
2. **Decide the per-sink retry model** (P2-1) — it determines the schema. Recommended: move retry state into `sink_state` JSONB.
3. **Add the `SanitizedAuditEntry` boundary type** (P1-4) before defining any concrete sink.
4. **Add a worker lifecycle section** covering startup, shutdown, and `close()` call ordering (P2-7).
5. **Re-circulate the revised spec** before approving for implementation.
