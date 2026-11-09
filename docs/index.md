# docling-context

`docling-context` turns source files into citable DCLX packages. Resources are
shared; a project represents a topic of work and links only the resources it
needs. One library document, memory, skill, or concept can support several
projects without being copied. SQLite records resource identity, links, jobs,
sessions, knowledge facts, and search indexes in one local store.

```text
docling://
├── resources/
│   ├── library/{doc_id}       original sources converted to DCLX
│   ├── memories/{memory_id}   session outcomes and conclusions
│   ├── skills/{skill_id}      reusable methods
│   ├── concepts/{concept_id}  entity and relationship definitions
│   └── knowledge/{knowledge_id} structured knowledge descriptors
├── projects/{project_id}/
│   ├── .description.dclx      optional short description
│   ├── .abstract.dclx         title, description, and linked counts
│   ├── .overview.dclx         linked resource inventory
│   └── sessions/{session_id}
└── user/{user_id}/
```

Each resource type is a flat list. Project membership and typed relationships
between resources live in SQLite. `dc overview` shows every project and counts
its linked documents, memories, skills, concepts, knowledge descriptors, and
sessions. Each project starts with the shared `doclang-authoring` skill linked,
so a new project's skill count is one. Project context documents do not count
as linked resources.

## 5 minutes to fun

Install Python 3.12+ and `uv`, then run from this repository:

```bash
uv sync --extra conversion
uv run dc init
uv run dc project create materials --title "Materials research"
uv run dc library add ./paper.pdf --project materials
uv run dc overview
uv run dc library search "material properties" --project materials
```

You can import a document directly from an HTTP(S) URL, including PDF URLs
without a `.pdf` suffix:

```bash
uv run dc library add https://arxiv.org/pdf/2608.23083 --project materials
```

`library add` prints the canonical library URI and revision. It checks the
source binary hash and reuses the existing library document on a repeat import,
whether the source is a local file or a URL. Search results include URI,
revision ID, and XPath. Read a result with
`uv run dc library outline URI` and `uv run dc library show URI XPATH --revision REVISION_ID`.
Add `--json` to commands when an agent or script needs structured output.

If you have no PDF, use a native DocLang source without the conversion extra:

```bash
printf '<doclang><text>Basalt is an igneous rock.</text></doclang>' > basalt.dclg
uv run dc library add ./basalt.dclg --project materials
uv run dc library search basalt --project materials
```

The default store is `~/.local/share/docling-context`. `dc init` creates its
SQLite database and package directory. Use `--store PATH` or set
`DOCLING_CONTEXT_STORE` to select another location, and use the same setting
for the CLI, workers, and MCP server. No PostgreSQL setup is required.

## Connect an agent

Start an agent inside a project session. The agent runs in your current
terminal; when it exits, you return to your shell and the session closes:

```bash
uv run dc session start --project materials --agent codex
```

Alternatively, install Docling Context's MCP tools and agent skill, then start
the agent yourself. For Codex, invoke the skill with `$dc`; in Claude Code,
Hermes, and Pi, use `/dc`:

```bash
uv run dc agent install codex --scope project
codex
```

Both paths support Codex, Claude Code, Hermes, and Pi. See
[agent installation](agents.md) for all four recipes, scope and command details,
and remote deployment.

## Guides

- [CLI and project workflows](cli.md)
- [Storage, revisions, and jobs](storage.md)
- [Scoped retrieval and citations](retrieval.md)
- [Conversion settings](conversion.md)
- [Agent sessions](sessions.md)
- [Durable memory](memory.md)
- [Agent installation and MCP](agents.md)
