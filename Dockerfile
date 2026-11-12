# @spec DACC-BUILD-002
# Images are pinned by digest: a tag can be moved to different content, a digest cannot.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS builder

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock README.md ./
COPY src/ src/

RUN uv sync --locked --no-dev --no-editable

FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH"
# The audit log is stdout; a buffered line is a lost line when the pod is killed.
ENV PYTHONUNBUFFERED=1

USER 65532:65532

EXPOSE 8000

# The console script, never `python -m paperless_mcp.server`: that loads the module twice.
CMD ["paperless-mcp"]
