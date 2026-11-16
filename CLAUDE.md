# paperless-mcp

MCP server over Paperless-ngx. Read-only today; see README.md.

## Dev loop

```sh
uv run ruff check .
uv run ruff format .
uv run mypy src
uv run pytest -q
```

## Rules

- Intent lives in a private spec repository, segment `document-access` (`DACC`). Change the
  spec first, then the test, then the code. Tests and code carry `@spec DACC-…`. Without
  access to the spec repo, describe the intended behaviour in the PR instead.
- Tests run against `tests/fake_paperless.py`. It rejects any query parameter it does not
  know, because real Paperless silently ignores unknown parameters and returns everything.
  Add a parameter to the fake only after confirming Paperless honours it.
- Never request `content` from Paperless, and never log a field value, title, search string
  or token.
- HTTP is `httpx2`, the library the MCP SDK itself uses.
- Start the server through the `paperless-mcp` console script, never `python -m`.
- `transport_security` must be passed explicitly to `streamable_http_app`; leaving it out
  disables DNS-rebinding protection.
