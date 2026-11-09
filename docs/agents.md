# Work with an agent

Install `docling-context` into a Python environment visible to the agent. The
package exposes `dc` for document work and `docling-context` for MCP serving.
The CLI, workers, and MCP server must use the same local SQLite store. These
examples use the default store at `~/.local/share/docling-context`.

```bash
uv sync --extra conversion
uv run dc init
uv run dc project create papers --title "Paper review"
uv run dc library add ./paper.pdf --project papers
```

There are two ways to work with an agent:

| Route | Start with | Session handling |
| --- | --- | --- |
| Launch the agent from `dc` | `dc session start --project papers --agent AGENT` | `dc` creates and closes the project session around the agent process. |
| Start the agent yourself | `dc agent install AGENT`, then invoke its `dc` skill | The skill instructs the agent to create, record, and close a project session through MCP. |

## Launch an agent from `dc`

```bash
uv run dc session start --project papers --agent codex
```

Replace `codex` with `claude`, `hermes`, or `pi`. The agent executable must be on
`PATH`. The command checks the project, installs the agent's managed MCP
connection and `dc` skill, starts a session under the project, and runs the
agent in your terminal. When the agent exits, you return to the original shell
and the session closes, even if the agent was interrupted. The launcher passes
the project ID and session URI to the agent and asks it to record useful
outcomes. MCP searches without an explicit project default to the launched
project.

Only events actually sent through `session_append` are stored; the launcher
does not capture the full conversation. Inspect the session and process any
queued memory compilation job with:

```bash
uv run dc session list --project papers
uv run dc session show docling://projects/papers/sessions/SESSION_ID
uv run dc session jobs docling://projects/papers/sessions/SESSION_ID
uv run dc memory worker --once
uv run dc memory proposals
```

The `SESSION_ID` comes from `session start` or `session list`. Closing a session
queues compilation; it does not itself create an accepted memory. A worker may
propose memories, which need review before acceptance.

Every project links the shared `doclang-authoring` resource skill. When an
agent writes DCLG or DCLX, it should read that skill first and validate the
result with DocLang's bundled XSD and Schematron rules. The installed `dc`
agent skill includes this instruction. Reinstall it with `dc agent install`
to update an existing agent installation.

The launch installs user-scoped agent configuration so it works from any
directory. The scope controls where the agent loads its configuration; it is
distinct from the Docling Context project ID. Agent sessions are tagged with
the selected agent in SQLite.

## Install for an agent you start yourself

Run the matching command from the environment where `docling-context` is
installed. The installer writes one owned MCP entry and one `dc` skill. Write
tools are enabled because the skill records project sessions. It preserves
unrelated agent configuration and refuses to overwrite an unmanaged `dc` skill.
The default scope is user; use project scope to keep the integration with a
working directory. Project scope is relative to the current directory (or
`--project-root PATH`); it does not choose a Docling Context project. Hermes
currently supports user scope for MCP.

```bash
uv run dc agent install codex --scope project
uv run dc agent status codex --scope project

uv run dc agent install claude --scope project
uv run dc agent status claude --scope project

uv run dc agent install hermes
uv run dc agent status hermes

uv run dc agent install pi --scope project
uv run dc agent status pi --scope project
```

Restart the agent after installation. Invoke the skill and name the Docling
Context project in your request:

| Agent | Example request inside the agent |
| --- | --- |
| Codex | `$dc Review the papers project and record useful conclusions.` |
| Claude Code | `/dc Review the papers project and record useful conclusions.` |
| Hermes Agent | `/dc Review the papers project and record useful conclusions.` |
| Pi | `/dc Review the papers project and record useful conclusions.` |

Codex also exposes the skill through `/skills`; the [official Codex skill guide](https://learn.chatgpt.com/docs/build-skills)
documents this syntax. The installed skill instructs the agent to select a
project, start a project session through MCP, search its linked resources,
record useful outcomes, and close the session. Verify what was recorded with
`dc session list --project papers` and `dc session replay SESSION_URI`.
`dc session start --project papers --agent codex` launches Codex in the current terminal;
`--no-launch` creates only the project session record.

If an agent is already running and you want to supply a session yourself, run
`dc session start --project papers --agent codex --no-launch`. This creates an
open session but does not install or launch the agent. Give the returned full
URI to the agent, then run `dc session close SESSION_URI` when finished. The
skill leaves launcher-provided sessions open for the launcher to close.

`dc agent status AGENT` checks that the managed skill and MCP configuration are
present; it does not open an MCP connection. Run `docling-context doctor AGENT`
with the same `--scope` to check the connection. `dc agent remove AGENT`
removes the owned skill and MCP entry. The lower-level `docling-context
integrate`, `doctor`, and `remove` commands remain available for MCP-only or
remote configurations; those commands do not install the skill.

## Harnesses

| Agent | User MCP config | Project MCP config | User skill | Project skill |
| --- | --- | --- | --- | --- |
| Codex | `~/.codex/config.toml` | `.codex/config.toml` | `~/.agents/skills/dc/SKILL.md` | `.agents/skills/dc/SKILL.md` |
| Claude Code | `~/.claude.json` | `.mcp.json` | `~/.claude/skills/dc/SKILL.md` | `.claude/skills/dc/SKILL.md` |
| Hermes Agent | `~/.hermes/config.yaml` | Unsupported | `~/.hermes/skills/dc/SKILL.md` | Unsupported |
| Pi | `~/.pi/agent/mcp.json` | `.pi/mcp.json` | `~/.pi/agent/skills/dc/SKILL.md` | `.pi/agent/skills/dc/SKILL.md` |

The installer preserves unrelated configuration and only removes entries and
skill files it owns. Project configuration is reviewable in Git. Codex and Pi
may require project trust before loading project MCP entries.
Configuration paths and transports follow the current
[Codex](https://developers.openai.com/codex/mcp),
[Claude Code](https://code.claude.com/docs/en/mcp),
[Hermes](https://hermes-agent.nousresearch.com/docs/reference/mcp-config-reference/),
and [Pi](https://pi.dev/docs/latest/mcp) MCP guides.

All four agents support local stdio and remote HTTP MCP connections through
the lower-level integration commands. Those commands manage the connection,
not the `dc` skill:

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

Read tools are on by default. `dc agent install` enables local write tools for
the skill. With the lower-level commands, local write tools require
`--allow-writes` on `integrate` or `docling-context mcp`; write tools include `ingest`,
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
server and worker restarts. Larger documents can be imported with
`dc library add`, which converts synchronously and indexes the result. Use
`dc library jobs status JOB_ID` to inspect either job type; add `--json` for a record.
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
