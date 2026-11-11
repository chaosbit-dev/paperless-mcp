FROM python:3.13-slim AS builder

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock README.md ./
COPY src/ src/

RUN uv sync --locked --no-dev --no-editable

FROM python:3.13-slim

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH"
# The audit log is stdout; a buffered line is a lost line when the pod is killed.
ENV PYTHONUNBUFFERED=1

USER 65532:65532

EXPOSE 8000

# The console script, never `python -m paperless_mcp.server`: that loads the module twice.
CMD ["paperless-mcp"]
