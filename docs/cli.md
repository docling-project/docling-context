# `dc` CLI

Install the package to expose `dc`. For PDF input, install the `conversion`
extra. The command uses a local store at `~/.local/share/docling-context` by
default; pass `--store PATH` to choose another directory. `--tenant` and
`--user` select the principal; their defaults are `default` and `local`.
Use `uv run dc` or activate the project environment; a system calculator also
uses the name `dc` on some machines.

```bash
uv run --extra conversion dc --store ./context-data add-resource ./paper.pdf \
  --collection papers --no-ocr --tables --no-charts
uv run dc --store ./context-data task status JOB_ID
uv run dc --store ./context-data worker --once
uv run dc --store ./context-data status
uv run dc --store ./context-data index status
uv run dc --store ./context-data index lex status
uv run dc --store ./context-data index lex rebuild
uv run --extra vectors dc --store ./context-data index vector status
uv run --extra vectors dc --store ./context-data index vector rebuild
uv run --extra vectors dc --store ./context-data search "invoice" --mode hybrid
uv run dc --store ./context-data ls docling://resources/
uv run dc --store ./context-data tree docling://resources/papers -L 2
uv run dc --store ./context-data find "invoice" --uri docling://resources/papers
uv run dc --store ./context-data grep "total" --uri docling://resources/papers
uv run dc --store ./context-data search "invoice" --uri docling://resources/papers
```

`add-resource` accepts one document or a folder of documents. With the
`conversion` extra installed, it accepts Docling's supported formats, including
PDF, DOCX, PPTX, XLSX, images, HTML, and plain text. Native DCLG and DCLX files
also work without that extra. Folder imports scan the top level by default.
Add `-r` or `--recursive` to include subfolders; those paths are retained in
the URI. Use repeatable `--from FORMAT` options to select Docling format names,
such as `--from pdf --from docx --from image` (default: all). Filtering uses
filename extensions; Docling then detects the actual input format. Files with
the same stem and different extensions receive distinct URIs. A single file
can use `--uri`; otherwise `--collection` defaults to `documents`. Use `--config`
for a [JSON conversion config](conversion.md), with command flags overriding it.
Use `--force` to create a new revision for unchanged input.
Table reconstruction always uses accurate mode; there is no CLI mode switch.
Page and picture images are on by default, and there is no default page limit.
`--no-page-images`, `--no-picture-images`, and `--max-pages N` override these
defaults. OCR uses RapidOCR with English PP-OCRv6 `tiny` models when enabled.

Ingestion is synchronous. Each new resource revision queues a collection-summary
job; `add-resource` prints the revision and its `task_id`. `task status` reports
that job. Run `worker --once` for one due job, or `worker` continuously. A
collection summary may remain stale until its latest job completes.

Every `status` command prints a table by default. Pass `--json` for structured
output, for example `dc status --json`, `dc task status JOB_ID --json`, or
`dc index vector status --json`.

`find` and `grep` are case-insensitive substring scans of current documents.
They scan at most 1,000 documents, 2,000 nodes per document, and 50,000 nodes
total, returning at most 100 hits. They accept
an optional URI subtree scope and enforce tenant and user access. They do not
yet provide embedding search or relevance ranking.

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
