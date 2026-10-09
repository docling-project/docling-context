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
│   ├── .abstract.dclx         L0 relevance check
│   ├── .overview.dclx         L1 structure and key points
│   └── sessions/{session_id}
└── user/{user_id}/
```

Each resource type is a flat list. Project membership and typed relationships
between resources live in SQLite. `dc overview` shows every project and counts
its linked documents, memories, skills, concepts, knowledge descriptors, and
sessions. Project context documents do not count as linked resources.

## 5 minutes to fun

Install Python 3.12+ and `uv`, then run from this repository:

```bash
uv sync --extra conversion
uv run dc init
uv run dc project create materials --title "Materials research"
uv run dc add-resource ./paper.pdf --project materials
uv run dc overview
uv run dc search "material properties" --project materials
```

`add-resource` prints the canonical library URI and revision. It checks the
source binary hash first and reuses the existing library document on a repeat
import. Search results include URI, revision ID, and XPath. Read a result with
`uv run dc outline URI` and `uv run dc show URI XPATH --revision REVISION_ID`.
Add `--json` to commands when an agent or script needs structured output.

If you have no PDF, use a native DocLang source without the conversion extra:

```bash
printf '<doclang><text>Basalt is an igneous rock.</text></doclang>' > basalt.dclg
uv run dc add-resource ./basalt.dclg --project materials
uv run dc search basalt --project materials
```

The default store is `~/.local/share/docling-context`. `dc init` creates its
SQLite database and package directory. Use `--store PATH` or set
`DOCLING_CONTEXT_STORE` to select another location, and use the same setting
for the CLI, workers, and MCP server. No PostgreSQL setup is required.

## Connect an agent

The bundled installer configures a local MCP server for Codex, Claude Code,
Hermes, or Pi. For example:

```bash
uv run docling-context integrate codex --scope project
uv run docling-context doctor codex --scope project
```

See [agent installation](agents.md) for all four recipes, supported scopes,
write tools, and remote deployment.

## Guides

- [CLI and project workflows](cli.md)
- [Storage, revisions, and jobs](storage.md)
- [Scoped retrieval and citations](retrieval.md)
- [Conversion settings](conversion.md)
- [Agent sessions](sessions.md)
- [Durable memory](memory.md)
- [Agent installation and MCP](agents.md)
