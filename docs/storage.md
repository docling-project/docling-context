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
contains DocLang content (L2), a summary (L0), and a TOC (L1). The source
binary SHA-256 is distinct from the DCLX package hash. `add-resource` checks
the source hash within the tenant before conversion and reuses the library URI
when it exists. `--force` writes a new revision. Revision provenance records
the original filename, origin, converter, options, and warnings.

`dc resource add TYPE FILE` imports a DCLX or DCLG resource. Skills, concepts,
and knowledge descriptors require type-specific JSON metadata; inspect
`dc resource add --help` for the shape. `dc resource link SOURCE_URI TARGET_URI
--relation NAME` records a typed relationship; `dc resource links URI` lists
both incoming and outgoing links. An optional source revision and XPath make a
relationship citable. `dc knowledge add-fact` stores a typed fact in SQLite
with an exact source citation.

## Projects

`dc project create ID --title TITLE` creates a project. `project link` and
`project unlink` change membership; unlinking does not delete a resource.
`project resources` lists a project's links; `resource projects URI` lists
projects linked to one resource. `set-description`, `set-abstract`, and
`set-overview` attach project-owned DCLX documents. Project creation,
membership changes, and description changes queue abstract and overview
refresh jobs. Run `dc project worker` to process one, `dc project jobs ID` to
inspect them, or `dc project refresh ID` to queue another refresh.

`dc overview` includes projects with no links and counts each linked resource
once. `dc search QUERY --project ID` restricts retrieval to linked resources.
Results cite canonical URI, immutable revision ID, and XPath.

Project sessions live at `docling://projects/{project_id}/sessions/{id}`.
`dc project session-start`, `session-append`, `session-close`, and
`session-list` manage them. Closing a project session queues memory compilation;
run `dc memory worker --once` to produce proposed shared memories linked back
to the project. The sessions column in `dc overview` counts project sessions.

This is a fresh-start layout. There is no migration tool for older stores.
