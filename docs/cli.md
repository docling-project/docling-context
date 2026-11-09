# `dc` command guide

`dc` stores source documents, reusable conclusions, methods, concepts, and cited facts in one local SQLite store. Projects link selected resources without copying them. The default store is `~/.local/share/docling-context`; pass `--store PATH` before a command to use another one. Run `dc` without arguments for a project overview and three starting commands. The examples below use `uv run dc`.

| Resource | Purpose |
| --- | --- |
| Library document | An imported source PDF, office document, image, URL, DCLG, or DCLX in `resources/library/`. |
| Memory | A reviewed conclusion or session outcome stored as DCLX in `resources/memories/`. |
| Skill | A reusable method stored as DCLX in `resources/skills/`. |
| Concept | A DCLX definition with a mandatory schema for entity types, properties, units, and relationships. |
| Knowledge dataset | A DCLX descriptor plus cited SQLite facts that follow a pinned concept revision. |
| Project | A topic, its resource links, context documents, and agent sessions. |

## Five minutes to start

```sh
uv sync --all-extras
uv run dc init
uv run dc project create --project materials --title 'Materials Research'
uv run dc library add --source ./paper.pdf --project materials
uv run dc library search --query 'porous materials' --project materials
uv run dc session start --project materials --agent codex
```

The top level has one route for each kind of work: `init`, `overview`, `project`, `library`, `session`, `memory`, `concept`, `skill`, `knowledge`, `index`, and `agent`. Use `dc COMMAND --help` to inspect options. Agent and script callers can add `--json` to individual commands.

To see every command and nested subcommand at once, run `uv run python scripts/dump_cli_tree.py`. Add `--show-options` to include their arguments. The script reads the CLI parser, so the tree follows command changes.

## `init` and `overview`

`init` creates the SQLite catalog and package store. `overview` lists every project with its linked resource counts and also reports store and job counts. Bare `dc` displays the same overview with starting examples.

```sh
uv run dc init
uv run dc overview
uv run dc overview --json
```

## `project`

Projects define topics and membership. Every project links the shared `doclang-authoring` skill. Project context consists of an optional description and generated abstract and overview documents.

| Subcommand | Use |
| --- | --- |
| `create ID --title TITLE` | Create a project. |
| `show ID` | Read its title and context URIs. |
| `resources ID` | List its linked library documents, memories, skills, concepts, and knowledge datasets. |
| `link ID URI` / `unlink ID URI` | Change membership. |
| `context set ID --description FILE` | Set a DCLG or DCLX description; `--abstract` and `--overview` also work. |
| `context show ID` | Read its context documents. |
| `context refresh ID` | Queue regeneration of the abstract and overview inventory. |
| `context jobs ID` / `context worker` | Inspect or process regeneration jobs. |

```sh
uv run dc project create --project oilgas --title 'Oil and Gas'
uv run dc project link --project oilgas --uri docling://resources/library/DOC_ID
uv run dc project resources --project oilgas
uv run dc project context set --project oilgas --description ./description.dclx
uv run dc project context worker
```

The generated abstract and overview describe the project's title, description, counts, and linked URIs. They do not summarize the contents of its source documents.

## `library`

The library owns original source documents and their discovery. Imported content is deduplicated by binary hash. `--project` links the imported document to a project. A repeat import reuses the library ID; conversion options can still produce a new revision.

| Subcommand | Use |
| --- | --- |
| `add SOURCE` | Import a local file, folder, or HTTP(S) URL. Use `-r` for nested folders. |
| `list [--project ID]` | List source documents. |
| `projects ID` | List projects linked to a document. |
| `links ID` / `link ID TARGET_URI --relation NAME` | Inspect or create document relationships. |
| `tree [URI]` | Walk a URI subtree. |
| `outline ID` | Read a document's table of contents. |
| `show ID XPATH` | Read one cited node. Full URIs also work. |
| `find TERM` / `grep TERM` | Bounded substring lookup. `find` ranks by occurrence count; `grep` keeps scan order. |
| `search QUERY` | Indexed lexical, vector, or hybrid search with exact citations. |
| `jobs status JOB_ID` | Inspect an asynchronous ingestion job created by an agent or API. |

```sh
uv run dc library add --source ./papers -r --project oilgas
uv run dc library add --source 'https://example.org/paper.pdf' --project oilgas
uv run dc library list --project oilgas
uv run dc library search --query 'Khurmala diagenesis' --project oilgas --mode hybrid
uv run dc library outline --uri DOC_ID
uv run dc library show --uri DOC_ID --xpath '/doclang[1]/text[1]'
```

`library add` converts synchronously. Its terminal progress bar counts PDF pages and advances after each document is reused or converted and committed. Other source types count as one unit. The bar reaches 100% after the whole import finishes; restart the same command after interruption. Conversion settings are in [conversion.md](conversion.md). `--json` keeps import output as JSON lines and hides the progress bar.

Search covers all indexed nodes in the selected project by default. `--tier 0`, `--tier 1`, and `--tier 2` narrow it to summary, outline, and source nodes. `-k` controls the returned hit count. Search warns when an index is stale and gives its rebuild command. See [retrieval.md](retrieval.md).

## `session`

One session command family handles both interactive and manual sessions. `start` installs the agent's managed skill and MCP connection, starts a project session, launches the agent in the current terminal, and returns after the agent exits. `--no-launch` creates a session record for an agent already running.

| Subcommand | Use |
| --- | --- |
| `start --project ID --agent NAME` | Launch Codex, Claude, Hermes, or Pi. |
| `list [--project ID]` | List sessions and their agent and state. |
| `show ID` / `replay ID` | Inspect session metadata or events. |
| `append ID TEXT --kind KIND --key KEY` | Save an event with a stable key. |
| `close ID` | Close the session and queue memory compilation. |
| `jobs ID` | Inspect memory compilation jobs. |
| `trim`, `attachment`, `delete`, `purge` | Manage events and lifecycle. |

```sh
uv run dc session start --project oilgas --agent claude
uv run dc session start --project oilgas --agent codex --no-launch
uv run dc session list --project oilgas
uv run dc session append --id SESSION_ID --text 'Reviewed section 2' --kind turn --key review-1
uv run dc session close --id SESSION_ID
```

A returned session ID works directly if it uniquely identifies one of your project sessions. Use the full session URI when IDs are ambiguous. Agents record useful outcomes through MCP; the launcher does not automatically copy the agent's complete conversation. See [sessions.md](sessions.md) and [agents.md](agents.md).

## `memory`

A memory is a durable conclusion or synthesis. Session compilation can propose one; an accepted memory enters recall and search. Direct creation can use a claim or an authored DCLG/DCLX document. With `--from`, the first `<text>` node is the concise claim and the original document body is retained across review.

| Subcommand | Use |
| --- | --- |
| `create [KIND] CLAIM [--project ID]` | Create a proposed memory; `--accepted` accepts a user-authored claim immediately. |
| `create --from FILE [--project ID]` | Create from DCLG/DCLX. |
| `list [--project ID]` / `show ID` | Inspect memories and review state. |
| `proposals`, `accept`, `reject`, `correct` | Review or correct a memory. |
| `search QUERY [--project ID]` / `recall QUERY [--project ID]` | Find accepted conclusions; recall suppresses repeats per session. |
| `worker --once` / `jobs SESSION_ID` | Process or inspect compilation. |
| `delete` / `purge` | Remove a memory or its history. |

```sh
uv run dc memory create --from ./diagenesis-summary.dclx --project oilgas
uv run dc memory list --project oilgas
uv run dc memory search --query 'Khurmala' --project oilgas
uv run dc memory accept --uri MEMORY_ID
uv run dc memory worker --once
```

See [memory.md](memory.md) for citations and review behavior.

## `concept`

A concept defines what facts mean. Create one from a DCLG or DCLX document and a mandatory schema descriptor. Its `schema` defines nonempty `entity_types`, typed `properties`, optional units, and optional `relationship_types` with source and target entity types.

```json
{
  "name": "Formation properties",
  "summary": "Observed properties of geological formations",
  "schema": {
    "entity_types": ["formation"],
    "properties": {"porosity": {"type": "number", "unit": "%"}},
    "relationship_types": {"overlies": {"source": "formation", "target": "formation"}}
  }
}
```

```sh
uv run dc concept create --id formation-properties --from ./formation-properties.dclg \
  --metadata ./formation-properties.json --project oilgas
uv run dc concept show --id formation-properties
uv run dc concept list --project oilgas
uv run dc concept projects --id formation-properties
```

`create` also accepts a DCLX that already contains `context/concept.json`. Both sources are validated before storage. `links` and `link` inspect or create relationships with other resources.

## `skill`

Skills are shared, named DCLX methods. Create one from DCLG or DCLX, optionally supplying descriptor JSON with a name and summary. Every project automatically links `doclang-authoring`.

```sh
uv run dc skill create --id sampling-method --from ./sampling-method.dclx --project oilgas
uv run dc skill show --id sampling-method
uv run dc skill list --project oilgas
```

`projects`, `links`, and `link` inspect memberships and resource relationships.

## `knowledge`

A knowledge dataset contains individually cited facts in SQLite and a DCLX descriptor in `resources/knowledge/`. Creation requires a concept. The dataset pins that concept's URI and immutable revision; a later concept edit does not change the dataset's schema. One concept can define several datasets in different projects.

| Subcommand | Use |
| --- | --- |
| `create ID --concept CONCEPT_ID [--project ID]` | Create a dataset using a validated concept. |
| `show ID` / `list [--project ID]` | Inspect the pinned concept and datasets. |
| `add-fact ID ENTITY_TYPE ENTITY_ID PROPERTY JSON_VALUE` | Validate and store one fact with an exact citation. Supply `--unit` when the property defines one. |
| `facts ID [--entity ENTITY_ID] [--property NAME]` | List or filter cited facts. |

```sh
uv run dc knowledge create --id khurmala-properties --concept formation-properties --project oilgas
uv run dc knowledge add-fact --uri khurmala-properties --entity-type formation --entity-id khurmala --property porosity --value 12.5 \
  --unit '%' --source-uri docling://resources/library/DOC_ID \
  --source-revision REVISION_ID --source-xpath '/doclang[1]/table[1]'
uv run dc knowledge facts --uri khurmala-properties --entity khurmala
```

Facts must match the pinned entity type, property, value type, and unit. Every source citation identifies a document URI, immutable revision, and XPath. Existing datasets created without a concept remain readable but cannot receive new facts; create a concept-backed dataset to continue writing.

## `index`

Inspect and rebuild search storage. Search does not repair stale indexes during a query.

```sh
uv run dc index status
uv run dc index lex rebuild
uv run dc index vector rebuild
```

`index lex status` and `index vector status` show the two indexes separately. Install the vector extra before building embeddings: `uv sync --extra vectors`.

## `agent`

Install or inspect the `/dc` skill and MCP connection in an agent. These commands do not launch an agent; `session start` does.

```sh
uv run dc agent install --agent codex
uv run dc agent status --agent codex
uv run dc agent install --agent claude --scope project
uv run dc agent install --agent hermes
uv run dc agent install --agent pi --scope project
uv run dc agent remove --agent codex
```

See [agents.md](agents.md) for each agent's setup and the MCP workflow.
