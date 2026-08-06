# LLM Governance Gateway

A split-control-plane LLM proxy. The proxy owns client-facing compatibility, auth, and provider dispatch; the governance service owns safety, policy, audit, and data-handling decisions. This context also covers the usage/observability dashboard being planned on top of the proxy.

## Language

**Usage Event**:
One *authenticated* proxied request, tagged with a status (allowed, blocked, errored). A request that fails authentication (401) never becomes a Usage Event — there's no real API key/tenant to attach it to yet, and failed-auth attempts are a security-log concern, not a usage/cost one. Status is one of three buckets: **allowed** (reached a provider and returned a response), **blocked** (a governance policy decision — PII/harm/policy violation), or **errored** (everything else that didn't complete: rate limit, governance-unavailable, bad model, protocol translation error, or a provider-side failure). Cost is computed only for allowed Usage Events that reached a provider and returned token counts; blocked/errored Usage Events always carry 0 tokens and $0 cost, since every known failure point short of "allowed" happens before or during the provider call without a usable response.
_Avoid_: Request (too generic — doesn't imply it's the unit the dashboard counts/aggregates, and wrongly implies auth failures are included)

**Usage Log**:
A new table, owned and written by the proxy service, holding one row per Usage Event: tenant, API key, model, status, prompt/completion tokens, cost, latency. Separate from `audit_log` — Usage Log identifies the real API key; `audit_log` deliberately stores only a pseudonym. Proxy writes it because token/cost data only exists after the provider responds, which governance never sees.
_Avoid_: Audit log (that's a distinct, governance-owned, pseudonymized compliance record — see below)

**Audit Log** (existing):
Governance-owned, append-only, pseudonymized compliance record of PII/harm/policy decisions per request. Does not carry token counts or cost, and identifies the caller only by an HMAC pseudonym, not the real API key. Not the same thing as the Usage Log.
_Avoid_: Usage log, usage data

**Pricing Table**:
A versioned table of $-per-token input/output rates per model, each with an `effective_from` date. The proxy resolves the active rate at request time and writes the computed cost onto the Usage Log row, so historical cost figures don't change retroactively when a rate changes.
_Avoid_: pricing.yaml, rate card (unless referring informally to the same concept)

**Usage Visibility**:
The access rule for who can see which Usage Log rows. A caller with the `admin` role sees every Usage Log row for their own `tenant_id` (all API keys, all users). A caller without `admin` sees only Usage Log rows for their own API key. No caller sees Usage Log rows outside their own `tenant_id` — there is no cross-tenant view. This reuses the existing `admin`/`tier1`/`tier2` role vocabulary (`config/users.yaml`); it does not use the `PLATFORM`/`TENANT`/`SELF` scope referenced in `audit_log`'s RLS policy, which no code path currently sets to a real value.
_Avoid_: PLATFORM/TENANT/SELF scope (unused today — see note above)

## Relationships

- A **Usage Event** produces at most one **Usage Log** row (proxy-owned) and at most one **Audit Log** row (governance-owned) — two independent records of the same request, for two different purposes (cost/observability vs. compliance).
- A **Usage Log** row is scoped to exactly one **API Key**, one **Tenant**, and one **Model**.
- A **Usage Log** row's cost is computed from the **Pricing Table** row active (`effective_from`) at the moment the request was made, not the rate active when a report is later queried.
- A **Usage Log** row's model is the canonical resolved `model_id` (e.g. `gpt-4o`), never a client-facing alias (e.g. `gpt4`) — `config/models.yaml`'s `alias_of` resolution happens before the row is written, so the **Pricing Table** only needs one rate per real model.

## Example dialogue

> **Dev:** "Can the usage dashboard just read from `audit_log`? It already has `model_id` and `tenant_id` per request."
> **Domain expert:** "No — `audit_log` doesn't have tokens or cost, and its `user_id` is a pseudonym, not the real API key. We need a separate **Usage Log** that the proxy writes after the provider responds, since that's the only place the numbers exist."

## Flagged ambiguities

- "usage" was ambiguous between "traffic volume" (all requests) and "billable spend" (allowed requests only) — resolved: a **Usage Event** is any request; cost is computed only over the allowed subset.
