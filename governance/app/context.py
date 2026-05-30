from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PipelineContext:
    # Inputs (set by caller before running pipeline)
    text: str
    tenant_id: str
    user_id: str
    model_id: str
    routing_method: str
    phase: str = "request"
    roles: list[str] = field(default_factory=list)

    # Populated by pii_stage
    pii_findings: list[dict] = field(default_factory=list)
    data_classification: str = "none"
    redacted_text: str = ""

    # Populated by harm_opa_stage
    harm_score: float = 0.0
    decision: str = "allow"
    violations: list[str] = field(default_factory=list)

    # Populated after audit write
    audit_id: str | None = None
