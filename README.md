# docling-context

`docling-context` converts source documents to DCLX and stores them in a shared
library. Projects link to any subset of library documents, memories, skills,
concepts, and structured knowledge. One source can serve several projects.
SQLite stores the catalog, project links, sessions, jobs, and search indexes;
DCLX packages live beside it in the local store.

See the [overview and five-minute quick start](docs/index.md),
[CLI guide](docs/cli.md), [storage guide](docs/storage.md), and
[agent installation guide](docs/agents.md).

## Quick start

With Python 3.12+ and `uv`:

```bash
uv sync --extra conversion
uv run dc init
uv run dc project create research --title "Research"
uv run dc library add ./paper.pdf --project research
uv run dc overview
uv run dc library search "key finding" --project research
```

`dc init` creates the local store; no database server is needed. Set
`DOCLING_CONTEXT_STORE` or pass `--store PATH` to choose its location. A repeated
import of the same source bytes reuses its library URI. Results carry a URI,
revision ID, and XPath for exact citation. Use `--json` for machine-readable
output; the default CLI output is tabular.

To launch an agent in a project session, run
`uv run dc session start --project research --agent codex`. To use Docling
Context from an agent you start yourself, run `uv run dc agent install codex`
and invoke its `dc` skill. Both flows support Claude Code, Hermes, and Pi; see
the [agent guide](docs/agents.md).

## Development

Run `uv sync --all-extras` and `uv run pytest -q tests`. New stores use the
SQLite project catalog. There is no migration command for PostgreSQL or older
store layouts.
