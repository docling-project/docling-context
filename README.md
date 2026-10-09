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
uv run dc add-resource ./paper.pdf --project research
uv run dc overview
uv run dc search "key finding" --project research
```

`dc init` creates the local store; no database server is needed. Set
`DOCLING_CONTEXT_STORE` or pass `--store PATH` to choose its location. A repeated
import of the same source bytes reuses its library URI. Results carry a URI,
revision ID, and XPath for exact citation. Use `--json` for machine-readable
output; the default CLI output is tabular.

To connect an agent, run `uv run docling-context integrate codex --scope project`
and `uv run docling-context doctor codex --scope project`. The same installer
supports Claude Code, Hermes, and Pi; see the [agent guide](docs/agents.md).

## Development

Run `uv sync --all-extras` and `uv run pytest -q tests`. New stores use the
SQLite project catalog. There is no migration command for PostgreSQL or older
store layouts.
