from __future__ import annotations

import asyncio
import sys

from . import pii as pii_module
from .context import PipelineContext
from .harm import harm_scan
from .opa import OPAError
from .opa import check as opa_check

HARM_THRESHOLD = 0.8


async def pii_stage(ctx: PipelineContext, opa_url: str) -> None:
    result = await pii_module.run(ctx.text)
    ctx.pii_findings = result.findings
    ctx.data_classification = result.data_classification
    ctx.redacted_text = result.redacted_text


async def harm_opa_stage(ctx: PipelineContext, opa_url: str) -> None:
    text_to_check = ctx.redacted_text if ctx.redacted_text is not None else ctx.text
    opa_input = {
        "phase": ctx.phase,
        "request": {
            "model": ctx.model_id,
            "provider": ctx.routing_method,
            "data_classification": ctx.data_classification,
            "pii_findings": ctx.pii_findings,
        },
        "user": {
            "user_id": ctx.user_id,
            "tenant_id": ctx.tenant_id,
            "roles": ctx.roles,
        },
    }

    harm_result, opa_result = await asyncio.gather(
        asyncio.to_thread(harm_scan, text_to_check),
        opa_check(opa_url, opa_input),
        return_exceptions=True,
    )

    if isinstance(harm_result, BaseException):
        print(f"[pipeline] harm_scan error (fail-open): {harm_result}", file=sys.stderr)
    else:
        ctx.harm_score = harm_result.score
        if harm_result.blocked or harm_result.score >= HARM_THRESHOLD:
            ctx.decision = "block"
            ctx.violations.append(f"harm:{harm_result.reason}")

    if isinstance(opa_result, OPAError):
        # OPA unavailable: fail-closed
        print(f"[pipeline] OPA error (fail-closed): {opa_result}", file=sys.stderr)
        ctx.decision = "block"
        ctx.violations.append("opa:unavailable")
    elif isinstance(opa_result, BaseException):
        print(f"[pipeline] unexpected OPA exception (fail-closed): {opa_result}", file=sys.stderr)
        ctx.decision = "block"
        ctx.violations.append("opa:error")
    else:
        if not opa_result.allowed:
            ctx.decision = "block"
            ctx.violations.extend(opa_result.violations)


async def run(ctx: PipelineContext, opa_url: str) -> PipelineContext:
    await pii_stage(ctx, opa_url)       # PII must complete first
    await harm_opa_stage(ctx, opa_url)  # harm + OPA run concurrently inside
    return ctx
