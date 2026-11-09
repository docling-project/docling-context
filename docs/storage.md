# Storage and project catalog

The local store contains `context.sqlite3` and content-addressed DCLX packages.
SQLite holds projects, resource identities and revisions, project membership,
typed resource links, project sessions, cited knowledge facts, jobs, and search
indexes. `dc init` creates the store at `~/.local/share/docling-context` by
default. Use `DOCLING_CONTEXT_STORE` or `dc --store PATH` to select another
location. Back up the SQLite database and package directory together.

## Shared resources

Library documents live at `docling://resources/library/{doc_id}`. The other
flat lists are `memories`, `skills`, `concepts`, and `knowledge`. A package
contains DocLang content (L2), a summary (L0), and a validated DocLang outline
(L1). The summary and outline are native DCLG companion documents at
`annotations/summary.dclg` and `annotations/toc.dclg`.
The source binary SHA-256 is distinct from the DCLX package hash. `library add` checks
the source hash within the tenant before conversion and reuses the library URI
when it exists. `--force` writes a new revision. Revision provenance records
the original filename, origin, converter, options, and warnings.

Use `dc library add --source SOURCE` for original documents, `dc skill create` for methods,
`dc concept create` for definitions, `dc memory create` for conclusions, and
`dc knowledge create` for a dataset of cited facts. A concept has a mandatory
schema in its DCLX descriptor. Each knowledge dataset pins the concept URI and
revision, so later concept edits cannot silently change fact validation.
`dc knowledge add-fact` checks entity type, property, value type, and unit
against that pinned schema before storing an exact source citation. Resource
links between datasets and concepts are recorded in SQLite.

## Projects

`dc project create --project ID --title TITLE` creates a project and links the shared
`docling://resources/skills/doclang-authoring` skill. The skill explains how
to write and validate DCLG XML and DCLX packages. It is required for every
project, including projects already present when a store is opened. `project
link` and `project unlink` change membership; unlinking does not delete a
resource.
`project resources` lists a project's links; `concept projects ID` or `skill projects ID` lists
projects linked to a typed resource. `project context set` attaches
project-owned DCLX documents. Project creation,
membership changes, and description changes queue abstract and overview
refresh jobs. Run `dc project context worker` to process one, `dc project context jobs --project ID` to
inspect them, or `dc project context refresh --project ID` to queue another refresh. The generated
abstract and overview contain the title, optional description, resource counts,
and URI inventory; they do not summarize source document contents.

`dc overview` includes projects with no links and counts each linked resource
once. `dc library search --query QUERY --project ID` restricts retrieval to linked resources.
Results cite canonical URI, immutable revision ID, and XPath.

Project sessions live at `docling://projects/{project_id}/sessions/{id}`.
`dc session start --project ID --agent codex` launches an agent and records its
session under that project. `dc session list --project ID` shows the agent and
state. Use `dc session start --no-launch` to create a session without launching an agent. Closing a
project session queues memory compilation;
run `dc memory worker --once` to produce proposed shared memories linked back
to the project. The sessions column in `dc overview` counts project sessions.

This is a fresh-start layout. There is no migration tool for older stores.
