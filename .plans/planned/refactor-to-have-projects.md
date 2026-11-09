# Shared resources and projects

## Agreed scope

Use a fresh local store. SQLite is the single catalog for resource identity,
revisions, project membership, typed relationships, jobs, sessions, search
indexes, and cited structured knowledge. DCLX packages are content-addressed
files beside the SQLite database. No PostgreSQL server or migration is needed.

```text
docling://
├── resources/
│   ├── library/{doc_id}
│   ├── memories/{memory_id}
│   ├── skills/{skill_id}
│   ├── concepts/{concept_id}
│   └── knowledge/{knowledge_id}
├── projects/{project_id}/
│   ├── .description.dclx
│   ├── .abstract.dclx
│   ├── .overview.dclx
│   └── sessions/{session_id}
└── user/{user_id}/
```

A project is a topic of work. Its membership links to subsets of flat shared
resources without copying packages. Source bytes are hashed before conversion;
a repeat import reuses the existing library document. `add-resource --project`
links the imported document. `dc overview` lists every project with counts of
linked documents, memories, skills, concepts, knowledge, and sessions.

## Implementation areas

1. Use `LocalContextStore` and one SQLite catalog for project and resource
   tables. Keep tenant-scoped foreign keys and unique constraints. Store raw
   source hashes separately from DCLX package hashes and immutable revisions.
2. Route `add-resource` to the shared library; replace `--collection` with
   `--project`. Preserve exact URI, revision, and XPath citations. Support
   project-filtered lexical and vector retrieval.
3. Store reviewed memories in the flat memory list, compile project sessions
   into linked memory proposals, and validate DCLX skill, concept, and
   knowledge descriptors. Store typed knowledge facts with source citations.
4. Refresh project abstract and overview DCLX documents after project changes.
   Expose project and resource links, overview, and knowledge facts in CLI,
   local SDK, HTTP, and MCP.
5. Retire old collection-specific public commands. Keep private user sessions
   available until a project is
   selected; new project sessions live under the project URI.
6. Rewrite `README.md` and `docs/` around SQLite, a five-minute quick start,
   and tested installer recipes for Codex, Claude Code, Hermes, and Pi.

## Acceptance checks

- Duplicate imports reuse a document ID, including concurrent imports.
- A resource can link to multiple projects; unlinking preserves it.
- Overview counts are correct for empty and populated projects.
- Project search returns only linked resources and citations remain readable.
- Tenant boundaries and memory owner review checks hold.
- Cited knowledge facts validate field types and source revisions.
- Project sessions compile into shared, project-linked memory proposals.
- CLI, SDK, MCP, docs, and lockfile agree on SQLite; no PostgreSQL setup is
  required.
- Run tests, lint, type checking, and smoke-test the quick start.

## Current implementation

The SQLite catalog, shared library deduplication, project membership and
search, flat memories, typed descriptors, knowledge facts, project context jobs,
CLI overview, and service/MCP project operations are implemented. Old
collection summary commands are removed from the CLI; internal compatibility
code and private user sessions remain. The documentation describes the SQLite
store and project workflow.
