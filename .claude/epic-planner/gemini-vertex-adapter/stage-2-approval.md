# Research Approval

**Decision:** Approved
**Date:** 2026-08-07
**Notes:** Coordinator approved all three recommendations as-is:

1. Two adapter modules (existing API-key module + new SA/Vertex module) sharing one common body-translation file.
2. Layered path selection (distinct model-catalog entry + tenant default + role-gated header override), fixing the `provider: google` vs `gemini` label bug as part of this work.
3. Add `google-auth` as the new dependency (via `uv add`); do not add the full `google-genai` SDK; keep the hand-written adapter style consistent with the other providers.

Instructed to proceed to PRD and SPEC, explicitly noting the two still-open UNCERTAIN items (exact `alt=sse` streaming response framing; exact 403 `reason` string for a missing `roles/aiplatform.user` grant) as flagged risks/unknowns to verify during implementation, not blockers to planning. Then continue through normal approval gates to epic + task creation.
