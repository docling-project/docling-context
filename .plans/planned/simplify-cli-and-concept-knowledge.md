# Simplify the CLI and ground knowledge in concepts

## Agreed behavior

- Use one canonical command for each operation; remove duplicate CLI routes.
- The top level is `init`, `overview`, `project`, `library`, `session`, `memory`,
  `knowledge`, `concept`, `skill`, `index`, and `agent`. The two extra typed groups
  keep concepts and skills out of the source-document library.
- `library` owns document import, listing, tree navigation, exact node reading,
  outlines, lexical lookup (`find` and `grep`), indexed search, and ingest jobs.
- Projects manage titles, shared resource links, and project context. Group context
  refresh, show, set, jobs, and worker under `project context`.
- Remove `project session-*`. `session start --project ID --agent NAME` launches
  the agent in the calling terminal, closes the session on exit, and returns to
  the caller. `--no-launch` explicitly creates a session without launching.
- Session IDs returned by the CLI work in subsequent commands; reject ambiguous
  IDs instead of choosing a project silently.
- Merge store status and pending-work counts into overview. Bare `dc` shows the
  overview and examples for creating a project, importing documents, and launching
  an agent. Keep the default SQLite store and global `--store` override.
- Memories are reviewed DCLX conclusions, syntheses, and session outcomes, with
  citations. Support project-scoped create/list/search and DCLX authoring, retain
  review, correction, recall, and compilation commands.
- Concepts are DCLX definitions with a mandatory machine-readable schema for
  entity types, typed properties, optional units, and relationship types.
- `knowledge create ID --concept CONCEPT_ID --project PROJECT` creates a DCLX
  dataset descriptor and stores cited facts in SQLite. It never takes an
  independent `--schema`. Pin the concept URI and immutable revision and copy the
  validated schema into the dataset catalog entry. Validate entity types, property
  names, value types, and units when adding facts. Later concept edits do not
  mutate existing datasets.
- Concepts can be reused by multiple datasets and projects. Dataset-to-concept,
  resource-to-resource, and project-to-resource relationships remain globally
  queryable. Source citations retain URI, revision, and XPath.
- No CLI compatibility aliases or automatic data migration. Existing documents
  and projects remain readable; unbound old datasets require explicit recreation
  before accepting new facts.

## Implementation steps

1. Refactor argparse command groups and dispatch while reusing existing import,
   search, session launcher, job, and inspection implementations.
2. Add typed concept/skill creation and listing, ID-to-URI resolution, concept
   schema validation, and concept-bound knowledge creation/show/list/filtering.
3. Resolve project session IDs and add project-scoped memory workflows without
   bypassing review or authorization rules.
4. Update CLI/index/agent/storage/conversion/retrieval/session documentation,
   README examples, generated `/dc` skill, and command references in source.
   Keep MCP tool names stable: recording a session through MCP is separate from
   launching an external interactive agent through the CLI.
5. Update existing CLI tests to the canonical routes. Test help, bare invocation,
   agent launch/return, resource scoping, memory project links, mandatory concept
   schemas, pinned revisions, fact rejection, and exact citations. Run the full
   suite and required lint/type checks.

## Schema contract

Concept metadata lives in `context/concept.json` inside DCLX. Its `schema` has
nonempty `entity_types` and `properties`; properties map names to definitions
such as `{"type":"number","unit":"%"}`. Supported value types are `text`,
`integer`, `number`, `boolean`, and `json`. Optional `relationship_types` map names
to source/target entity types. Definitions and documentation live in the DCLG
document body. DCLG input requires descriptor metadata with the mandatory schema;
DCLX input may carry the descriptor directly.

## Verification and completion

- Implemented the canonical CLI groups and removed duplicate top-level and
  project-session routes. Bare `dc` shows the overview and starting examples.
- Added document, skill, and concept relationship commands; project-scoped
  memory authoring and lookup; and authored DCLG/DCLX memory preservation.
- Knowledge datasets now require a concept, pin its URI and revision, and
  validate typed facts and units against the copied schema.
- Updated the CLI, agent, storage, session, retrieval, conversion, and index
  documentation, plus the generated agent skill.
- Verified with Ruff, mypy, and the full pytest suite (119 tests passed).
