# `dc` CLI

Install the package to expose `dc`. For PDF input, install the `conversion`
extra. Run `dc init` to create the local SQLite catalog and package directory.
The store defaults to `~/.local/share/docling-context`;
pass `--store PATH` to choose another directory. `--tenant` and `--user`
select the principal, defaulting to `default` and `local`. Use `uv run dc` or
activate the environment; some systems also have a calculator named `dc`.

```bash
export DOCLING_CONTEXT_STORE=./context-data
uv run dc init
uv run dc project create papers --title "Paper review"
uv run --extra conversion dc --store ./context-data add-resource ./paper.pdf \
  --project papers --no-ocr --tables --no-charts
uv run dc overview
uv run dc project resources papers
uv run dc resource projects docling://resources/library/DOC_ID
uv run dc search "invoice" --project papers
uv run dc project link papers docling://resources/library/DOC_ID
uv run dc project unlink papers docling://resources/library/DOC_ID
uv run dc resource add skills ./method.dclx --project papers
uv run dc resource link docling://resources/library/DOC_ID \
  docling://resources/skills/SKILL_ID --relation uses-method
uv run dc resource links docling://resources/library/DOC_ID
uv run dc project set-description papers ./description.dclx
uv run dc project worker
uv run dc project jobs papers
uv run dc project session-start papers --id review-1
uv run dc project session-append papers review-1 "Read section 2" --kind turn --key turn-1
uv run dc project session-close papers review-1
uv run dc --store ./context-data status
uv run dc --store ./context-data index status
uv run dc --store ./context-data index lex status
uv run dc --store ./context-data index lex rebuild
uv run --extra vectors dc --store ./context-data index vector status
uv run --extra vectors dc --store ./context-data index vector rebuild
uv run --extra vectors dc --store ./context-data search "invoice" --project papers --mode hybrid
uv run dc --store ./context-data ls docling://resources/
uv run dc --store ./context-data tree docling://resources/library -L 2
uv run dc --store ./context-data outline docling://resources/library/DOC_ID
uv run dc --store ./context-data show docling://resources/library/DOC_ID \
  '/doclang[1]/text[1]' --format xml
uv run dc --store ./context-data session start --id chat-1
uv run dc --store ./context-data session list --status open
uv run dc --store ./context-data memory list --status proposed
uv run docling-context integrate codex --scope project --store ./context-data
uv run docling-context doctor codex --scope project --store ./context-data
```

`add-resource` accepts one document or a folder of documents. With the
`conversion` extra installed, it accepts Docling's supported formats, including
PDF, DOCX, PPTX, XLSX, images, HTML, and plain text. Native DCLG and DCLX files
also work without that extra. Folder imports scan the top level by default.
Add `-r` or `--recursive` to include subfolders; those paths are retained in
source provenance. Use repeatable `--from FORMAT` options to select Docling format names,
such as `--from pdf --from docx --from image` (default: all). Filtering uses
filename extensions; Docling then detects the actual input format. Every new
source receives a flat library URI; an identical source binary reuses its URI.
Use `--project ID` to link the import to a project. Use `--config`
for a [JSON conversion config](conversion.md), with command flags overriding it.
Use `--force` to create a new revision for unchanged input.
Table reconstruction always uses accurate mode; there is no CLI mode switch.
Page and picture images are on by default, and there is no default page limit.
`--no-page-images`, `--no-picture-images`, and `--max-pages N` override these
defaults. OCR uses RapidOCR with English PP-OCRv6 `tiny` models when enabled.

Ingestion is synchronous. `add-resource` prints its library URI, revision,
source hash, and optional project ID. `dc overview` lists projects and counts their
distinct linked resources; `--json`, `--limit`, and `--offset` are supported.
`dc project set-abstract` and `set-overview` accept native DCLX or DCLG files.
Project changes queue automatic abstract and overview refresh jobs; run
`dc project worker` to process them.
Project session commands store DCLX event packages under the project path and
make the session count visible in `dc overview`. Closing one queues memory
compilation; `dc memory worker --once` creates proposed shared memories.

Skills, concepts, and knowledge are flat DCLX resources. `resource add` adds a
typed metadata part to the package. Supply `--metadata FILE` to define it; for
example, a knowledge descriptor can use:

```json
{"name":"Materials","summary":"Measured material properties","fields":{"density":"number","phase":"text"}}
```

After `dc resource add knowledge ./materials.dclg --metadata ./materials.json
--project papers`, use `dc knowledge add-fact --help` to see the required
entity, property, JSON value, and exact source citation arguments. `dc knowledge
facts KNOWLEDGE_URI` lists stored facts. Concept metadata can define
`entity_types`, `relationship_types`, and `properties`; skill metadata holds
its name and summary. Use `dc resource link` for relationships between these
resources and library documents.

`outline URI` reads the TOC sidecar and `show URI XPATH` reads one cited node.
Use `--revision ID` for an immutable revision, `--format xml` for DocLang XML,
and `--max-chars N` to bound output. Both commands show labeled tables by
default; `--raw` prints only the XML or node content. Use `--json` for the full
record, including URI, document ID, revision ID, XPath, and truncation marker.

`docling-context` exposes the existing `dc` commands and adds `mcp`,
`integrate`, `doctor`, `remove`, and `ingest-worker`. See the
[agent harness guide](agents.md) for scopes, remote tokens, and verification.

CLI commands print labeled tables by default. `dc tree` also has `--raw` for an
indented URI view. Pass `--json` for structured output, for example
`dc status --json`, `dc add-resource FILE --json`, or
`dc project show ID --json`. Import, project-resource, resource-link, tree,
`ls`, `find`, and `grep` commands emit one JSON object per row; other list
commands emit a JSON array.
The main `status` also shows session, memory, and memory compilation job counts
for the selected user. The session and memory lists support `--limit` and `--offset` pages;
the default page size is 100 and the maximum is 1,000.

## Sessions and memory

Start a session before recording events. `start --id` is idempotent for an open
session; omit `--id` to generate one. Session commands use the selected tenant
and user. `append` needs a stable `--key`, so a retried harness hook cannot
duplicate an event. Use `--stdin` for text that should stay out of process
arguments. An optional binary `--attachment` is stored as a typed DCLX part.

```bash
uv run dc session start --id chat-1 --json
printf 'I prefer concise replies' | uv run dc session append chat-1 \
  --kind feedback --key feedback-1 --stdin --json
uv run dc session replay chat-1
uv run dc session show chat-1
uv run dc session list --status open --since 2026-10-01
uv run dc session close chat-1
uv run dc memory worker --once
uv run dc memory proposals
```

Set `DOCLING_CONTEXT_SESSION=chat-1` to omit the ID from `session append`, or
use `--text` when sending a short event without stdin. `session close` prevents
new events and queues compilation of the final revision. `session jobs ID` and
`memory jobs ID` show compilation outcomes. `memory worker` polls for jobs;
`--once` handles one due job and exits. Queued jobs for older session revisions
become `superseded` when a newer revision is queued.

`session list` filters by `--status open|closed` and `--since`/`--until` ISO
dates or timestamps. `session trim ID --keep-last N` creates a shorter current
revision. Use `session attachment ID KEY --output FILE` to save one attached
event payload.

```bash
uv run dc memory list --kind preference --status proposed
uv run dc memory show MEMORY_URI --json
uv run dc memory accept MEMORY_URI
uv run dc memory search 'concise replies'
uv run dc memory recall 'concise replies' --session chat-1
uv run dc memory correct MEMORY_URI 'I prefer brief answers'
uv run dc memory reject MEMORY_URI --reason 'Incorrect inference'
uv run dc memory delete MEMORY_URI
uv run dc memory purge MEMORY_URI
```

`memory list` includes every non-purged status by default, including rejected
and deleted claims. Filter it by `--status`, `--kind`, `--since`, or `--until`.
`memory proposals` is a shortcut for proposed claims. `memory create KIND CLAIM`
creates a user-authored proposal; `--accepted` accepts it immediately. Optional
source citations require all four flags: `--source-uri`,
`--source-document-id`, `--source-revision-id`, and `--source-xpath`.
`memory search` and `memory recall` return accepted claims only. Recall records
which memory revision was delivered to a session and suppresses repeats.

### Delete and purge

`delete` withdraws an item from normal use while retaining its history. `purge`
removes that item's stored revisions and content. Both commands are scoped to
the selected tenant and user.

| Command | Immediate effect | What remains |
| --- | --- | --- |
| `dc session delete ID` | Removes the current session from `session list`, `show`, and `replay`. | Historical revisions and their attachments remain stored; compilation jobs and the session's recall ledger remain. |
| `dc session purge ID` | Deletes the session if necessary, then removes its revision records, attachments, compilation jobs, and recall ledger. | Derived memories are separate packages. The change outbox retains operation metadata. |
| `dc memory delete URI` | Writes a new revision with status `deleted`. The claim leaves memory search and recall. | The current claim and review history remain visible through `memory show URI` and `memory list --status deleted`; older revisions remain stored. |
| `dc memory purge URI` | Removes a previously deleted claim's current package, revisions, search index entries, provenance links, and recall records. | The source session or document and change outbox metadata remain. |

`memory purge` requires `memory delete` first. `session purge` can be called on
an active or previously deleted session. Removing a source session causes
dependent accepted memories with no other surviving citation to return to
`proposed` for review; it does not purge those memory packages. Purging a memory
does not erase its wording from a cited session or document. If that source is
later compiled again, it can produce a new proposal.

Package blobs are content addressed and are removed by purge only when no
remaining revision references them. The outbox retains earlier put/delete
identifiers and operation metadata; purge does not clear it. Purge removes the
item's stored content and revisions, but does not erase every related record.

`find` and `grep` are case-insensitive substring scans of current documents.
They scan at most 1,000 documents, 2,000 nodes per document, and 50,000 nodes
total, returning at most 100 hits. They accept
an optional URI subtree scope and enforce tenant and user access. They do not
yet provide embedding search or relevance ranking. When scoped to memory
packages, they skip claims that are not accepted.

`search` uses the SQLite retrieval index and returns exact DCLX citations.
New or changed documents are indexed during ingestion. `index status` reports
both lexical and vector coverage. `index lex status` and `index vector status`
show each index separately. `index lex rebuild` regenerates the SQLite index
from stored DCLX packages without rerunning conversion or OCR. It invalidates
an existing vector snapshot; a configured embedding provider rebuilds it on its
next use. Status and lexical rebuild honor the selected tenant and user scope.

The CLI defaults to lexical search until a vector model has been built, then
uses hybrid search. `--mode lexical|vector|hybrid` selects a mode explicitly.
Install the `vectors` extra and run `dc index vector rebuild` to generate
embeddings with the local default `BAAI/bge-small-en-v1.5` model.
`--model MODEL` selects another supported FastEmbed text model;
`Qdrant/clip-ViT-B-32` selects paired
text and image encoders. The first build downloads model weights unless
`--offline` is passed. On Apple Silicon, `--backend auto` selects MLX for
supported BGE models and uses ONNX for other models. Use `--backend mlx` or
`--backend onnx` to choose explicitly. MLX uses separate model weights and
cannot be combined with `--offline`; `--backend auto --offline` uses ONNX.
The selected backend is saved with the vector generation, so subsequent
searches use the same encoder. Both lexical and vector rebuilds display a
progress bar on interactive terminals; progress goes to stderr.
After a vector build, document writes and deletes replay from the outbox on
the next vector search. Missing or corrupt snapshots trigger a full rebuild.
`dc index vector status` reports embedding coverage and the
stored model. `search --image PATH --mode vector` accepts an image query with
CLIP; text and image may be supplied together. A text-only model skips the
image input. Vector builds currently require all documents in the store to be
accessible to the selected principal because the snapshot is store-wide.
With an image-capable model, referenced picture assets are indexed alongside
text nodes. Search results include the picture XPath and asset path; image-only
results show an image marker in the table. After upgrading an existing store,
run `dc index lex rebuild` and `dc index vector rebuild` to add picture assets.

Use `--uri`, `--document`, and `--xpath` to restrict scope; repeat `--tier`
to select context tiers. `-k`, `--max-tokens`, and `--per-result-tokens`
control output size. It prints a compact table by default; pass `--json` for
full citation records. See [scoped retrieval](retrieval.md) for the Python
vector and hybrid API.
