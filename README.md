# paperless-mcp

An MCP server for [Paperless-ngx](https://docs.paperless-ngx.com/). It lets a Claude session
find documents and read their extracted custom fields — amounts, dates, providers — without
returning document text.

- Each caller acts as their own Paperless user. The server can see only what that user can.
- Five read tools: `list_schema`, `search_documents`, `get_document`, `summarize_field`,
  `run_saved_view`.
- Documents tagged `private`, and documents with no type, are returned as metadata only.
  Configured document types are left out entirely.
- Served over streamable HTTP at `/mcp`, behind Cloudflare Access; the Access JWT is
  re-validated in-process.

## Design and specs

The design and the EARS specs live in the `homelab-batch` repository, segment
`document-access` (prefix `DACC`):

- `docs/intent/document-access/document-access-design.md`
- `docs/intent/document-access/document-access-specs.md`

Code and tests here cite those specs as `@spec DACC-…`.

## Development

```sh
uv sync
uv run ruff check .
uv run mypy src
uv run pytest -q
```
