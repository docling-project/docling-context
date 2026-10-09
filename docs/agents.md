# Agent harness installation

Install `docling-context` into a Python environment visible to the harness. The
package exposes `dc` for document work and `docling-context` for MCP serving and
harness integration. Use the same local SQLite store for the CLI, workers, and
MCP server.

```bash
uv sync --extra conversion
export DOCLING_CONTEXT_STORE=./context-data
uv run dc init
uv run dc project create papers --title "Paper review"
uv run dc add-resource ./paper.pdf --project papers
uv run docling-context integrate codex --scope project
uv run docling-context doctor codex --scope project
```

`integrate` launches a temporary MCP server and checks handshake, tool discovery,
backend listing, and one immutable citation when indexed content exists. Then it
adds a named `docling-context` entry to the harness configuration. Restart the
harness to load it. `doctor` repeats the checks; `remove` deletes only an entry
still owned by this installer. Existing entries with the same name are reported
as conflicts. Repeating `integrate` is safe, and changing options upgrades only
the owned entry. The installer writes a small ownership marker next to the
config; keep it alongside the config for future upgrades and removal.

## Install in each harness

Run one of these pairs from the environment where `docling-context` is
installed. The installer writes one named MCP entry and `doctor` verifies the
connection. Use `--scope user` for a personal installation; Hermes currently
supports only user scope. Add `--allow-writes` to `integrate` when the agent
should ingest documents or write session and memory records.

```bash
uv run docling-context integrate codex --scope project
uv run docling-context doctor codex --scope project

uv run docling-context integrate claude --scope project
uv run docling-context doctor claude --scope project

uv run docling-context integrate hermes --scope user
uv run docling-context doctor hermes --scope user

uv run docling-context integrate pi --scope project
uv run docling-context doctor pi --scope project
```

Restart the harness after installation. Ask it: “Use docling-context overview
to list my projects, then search the papers project for the main findings and
show the cited nodes.” The MCP `overview` and `search(project_id=...)` tools
use the SQLite project catalog. The installer carries the selected `--store`
path into the local MCP command.

## Harnesses

| Harness | User config | Project config | Local stdio | Remote HTTP |
| --- | --- | --- | --- | --- |
| Codex | `~/.codex/config.toml` | `.codex/config.toml` | Yes | Yes |
| Claude Code | `~/.claude.json` | `.mcp.json` | Yes | Yes |
| Hermes Agent | `~/.hermes/config.yaml` | Unsupported by this installer | Yes | Yes |
| Pi | `~/.pi/agent/mcp.json` | `.pi/mcp.json` | Yes | Yes |

The config tests check preservation, repeat installs, upgrades, and removal. The
MCP handshake is tested with the Python MCP client; harness binaries are not
required for the test suite. Project configuration is reviewable in Git.
Codex and Pi may require project trust before loading project MCP entries.
Configuration paths and transports follow the current
[Codex](https://developers.openai.com/codex/mcp),
[Claude Code](https://code.claude.com/docs/en/mcp),
[Hermes](https://hermes-agent.nousresearch.com/docs/reference/mcp-config-reference/),
and [Pi](https://pi.dev/docs/latest/mcp) MCP guides.

```bash
uv run docling-context integrate claude --scope user
uv run docling-context integrate hermes --scope user
uv run docling-context integrate pi --scope project
uv run docling-context remove pi --scope project
```

The local entry uses the absolute Python executable of the current environment.
Keep that environment installed and accessible after restarting the harness.
Use `--store PATH`, `--tenant ID`, and `--user ID` on `integrate` and `doctor`
to select a local store and principal. Those IDs select local scope; they are
not remote authentication credentials. If the chosen store has no indexed
document, `doctor` reports that cited retrieval could not be exercised.

## Using the tools

Ask the agent to call `search` with a narrow URI scope, inspect relevant
documents with `outline`, and call `show` using the returned document URI,
immutable `revision_id`, and XPath. Keep those three values with quoted text
for a reproducible citation. `show` accepts `format="xml"` for bounded DocLang
XML. `search(include_context=True)` also assembles the bounded cited hits into
one context string. `@toc:` and `@summary:` search paths refer to package
sidecars; `show` accepts those paths, and `outline` exposes the TOC links.
With a configured image-capable vector model, `search` accepts an optional
base64 image query (up to 2.5 MB) alongside query text. A text-only model
silently skips the image contribution.

Read tools are on by default. Local write tools require `--allow-writes` on
`integrate` or `docling-context mcp`; write tools include `ingest`,
`project_create`, `project_link`, `project_session_start`,
`project_session_close`, `resource_link`, `knowledge_add_fact`,
`session_append`, and `memory_review`. Memory capture remains opt-in. Each
operation checks the principal's tenant and user scope. The current local
principal comes from the server command, not from tool arguments.

MCP `ingest` accepts a filename and base64 source bytes up to 2.5 MB, with an
optional `project_id`, and returns a durable job ID. The worker checks the
source hash, stores the DCLX in the flat library, and links it to the project
when requested. Run `docling-context ingest-worker` alongside the agent, or
`docling-context ingest-worker --once` for one job. `job_status` reports queued,
running, completed, or failed state and the resulting revision. Jobs survive
server and worker restarts. Larger documents can be imported with `dc
add-resource`, which converts synchronously and indexes the result. Use
`dc jobs status JOB_ID` to inspect either job type; add `--json` for a record.
Queued Docling conversions use a five-minute document timeout.

## Remote service

Run the HTTP service behind a TLS terminator and provide a bearer token in an
environment variable. The server binds that token to the configured tenant and
user; callers cannot substitute a principal in requests. The server refuses to
start without the token.

```bash
export DOCLING_CONTEXT_TOKEN='your-deployment-token'
uv run docling-context mcp --transport http --host 127.0.0.1 --port 8765 \
  --store ./context-data --tenant team --user agent
uv run docling-context integrate codex --mode remote \
  --url https://context.example.com/mcp
```

`integrate` and `doctor` need the token in their own environment to verify the
remote MCP endpoint. Generated config contains only the variable name or
`${VARIABLE}` reference, never the token value. Remote write tools are enabled
only on the server with `--allow-writes`. Restrict the deployment token and use
separate server instances or authentication mappings for distinct principals.

The same version 1 operations are available to Python through
`RemoteContextService` at `/v1/{operation}` and locally through
`ContextService`. For example:

```python
from docling_context import ContextService, LocalContextStore, Principal, SearchRequest

with LocalContextStore("./context-data") as store:
    service = ContextService(store, Principal("team", "agent"))
    hits = service.search(SearchRequest("Koonap", project_id="papers"))
    for hit in hits.data["hits"]:
        print(hit["uri"], hit["revision_id"], hit["xpath"])
```

For a deployment, use the same `SearchRequest` with
`RemoteContextService("https://context.example.com/mcp", token)`. The token
stays in the client process and is sent only as a bearer header.

The HTTP API requires `schema_version: 1`, returns an operation ID, and uses
stable error codes such as `invalid_uri`, `unauthorized_scope`,
`missing_revision`, `stale_citation`, `package_failure`, and
`budget_exceeded`. Content-bearing responses mark truncation.
