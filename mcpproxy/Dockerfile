FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /workspace
ENV PYTHONPATH=/workspace:/workspace/mcpproxy

COPY pyproject.toml uv.lock ./mcpproxy/
RUN cd mcpproxy && uv sync --frozen --no-dev

COPY . ./mcpproxy

WORKDIR /workspace/mcpproxy
CMD ["uv", "run", "--project", "/workspace/mcpproxy", "uvicorn", "mcpproxy.app.main:app", "--host", "0.0.0.0", "--port", "8000"]
