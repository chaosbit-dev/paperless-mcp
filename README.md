# paperless-mcp

[![License: MIT](https://img.shields.io/badge/license-MIT-10B981)](LICENSE)
[![CI](https://img.shields.io/github/actions/workflow/status/chaosbit-dev/paperless-mcp/build-push.yml?branch=main&label=CI)](https://github.com/chaosbit-dev/paperless-mcp/actions/workflows/build-push.yml)
[![Version](https://img.shields.io/badge/dynamic/toml?url=https%3A%2F%2Fraw.githubusercontent.com%2Fchaosbit-dev%2Fpaperless-mcp%2Fmain%2Fpyproject.toml&query=%24.project.version&label=version&prefix=v)](pyproject.toml)
[![Python](https://img.shields.io/python/required-version-toml?tomlFilePath=https%3A%2F%2Fraw.githubusercontent.com%2Fchaosbit-dev%2Fpaperless-mcp%2Fmain%2Fpyproject.toml)](pyproject.toml)
[![Coverage](https://img.shields.io/endpoint?url=https%3A%2F%2Fchaosbit-dev.github.io%2Fpaperless-mcp%2Fcoverage.json)](.github/workflows/coverage.yml)

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

The design and the EARS specs are kept in a separate, private specification repository,
under the segment `document-access` (prefix `DACC`). Code and tests here cite those specs as
`@spec DACC-…`, so a citation is a pointer to the requirement a line implements even when the
spec itself isn't public.

## Configuration

Environment variables, all prefixed `PAPERLESS_MCP_`:

| Variable | Meaning |
|---|---|
| `PAPERLESS_URL` | Base URL the server calls Paperless on. |
| `PAPERLESS_UI_URL` | Base URL used to build the document links it returns. |
| `ACCESS_TEAM_DOMAIN` | Cloudflare Access team domain, e.g. `team.cloudflareaccess.com`. |
| `ACCESS_AUDIENCE` | The Access application's audience tag. |
| `IDENTITIES` | JSON list of `{"email", "token_env", "mode"}`. `token_env` names the environment variable holding that caller's Paperless token; `mode` is `read`. |
| `EXCLUDED_DOCUMENT_TYPES` | JSON list of document type names left out of every result. Default `["School Record"]`. |
| `ALLOWED_HOSTS` | JSON list of `Host` values the MCP endpoint answers to. |
| `HOST`, `PORT` | Listen address. Default `0.0.0.0:8000`. |

The server refuses to start, and exits non-zero, when the identity map is unusable, when an
identity holds more than view permissions in Paperless, or when the `private` tag or an
excluded document type does not exist. It re-checks permissions hourly and exits if they
have grown.

Each tool call writes one JSON audit line to stdout: who called, which Paperless user it
acted as, the tool, the document ids returned. Never a value, a title or a search string.
A call refused before a tool runs (an unknown tool, arguments that do not fit the schema) writes
the same line with the error class.

## Image

`build-push.yml` publishes `ghcr.io/chaosbit-dev/paperless-mcp` (a public package) on every
push to `main`, and only after `ci.yml` passes on that commit. It holds no stored secret: it
pushes with the run's own `GITHUB_TOKEN`, signs the image keyless with the run's GitHub OIDC
identity, and attests an SBOM and build provenance. A pull request only builds the image. The
`cosign verify` command is at the top of the workflow.

## Development

```sh
uv sync
uv run ruff check .
uv run mypy src
uv run pytest -q
```

## License

[MIT](LICENSE)
