# Build context is the repo root (see docker-compose.yml), so this stage can
# reach both this directory and policies/mcp/authz.rego.
FROM python:3.12-slim AS strip
WORKDIR /build
COPY opa-sidecar/strip_entitlements.py .
COPY policies/mcp/authz.rego .
RUN python strip_entitlements.py authz.rego authz.rego

# Full OPA server process, not WASM/in-process (docs/auth-architecture.md,
# "colocated sidecar, not in-process/WASM").
FROM openpolicyagent/opa:0.68.0-debug
COPY --from=strip /build/authz.rego /policies/mcp/authz.rego
COPY opa-sidecar/sidecar-config.yaml /config/sidecar-config.yaml

CMD ["run", "--server", "-a", "127.0.0.1:8181", "-c", "/config/sidecar-config.yaml", "/policies/mcp"]
