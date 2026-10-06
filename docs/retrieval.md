# Scoped retrieval

`dc search` uses the local SQLite index and works without an embedding model:

```bash
uv run dc search "glacier" --uri docling://resources/articles \
  --xpath '/doclang[1]/text[1]' -k 5 --max-tokens 1000
```

Ingestion indexes each document automatically. Run `dc index status` to inspect
both indexes, or `dc index lex rebuild` to regenerate lexical records from stored DCLX
packages. Rebuilding the SQLite index does not generate embeddings. Run
`uv run --extra vectors dc index vector rebuild` to generate them with the default
local model, then `dc search` uses hybrid mode automatically. Use
`dc search --mode lexical` to request lexical search explicitly.
On Apple Silicon, vector rebuilding selects MLX for supported BGE models when
installed; other models use FastEmbed's ONNX runtime. The backend is part of the
stored model ID, so changing backends regenerates vectors. Both rebuild
commands report progress on interactive terminals.

By default, the command prints a table with total, lexical, vector, tier,
freshness, and structural proximity scores, document ID, XPath, and a compact
text excerpt. The excerpt shows the entire text through 128 characters, or
the first and last 64 characters separated by ` ... `. Use `--json` for the
complete citation records, including URI, revision, page, bounding box, full
bounded text, and truncation status. A footer reports how many nodes are shown
out of all indexed lexical matches in scope, before result and token limits.
`--document` restricts a query to one URI;
`--tier` can be repeated. `--max-tokens` and
`--per-result-tokens` limit the returned text using whitespace-delimited tokens.
Multiword lexical searches first look for nodes containing every query word;
if none match, they use any-word matching. The footer counts matches for the
selected lexical query.
The `@summary:` and `@toc:` XPath prefixes denote sidecars within the cited
DCLX revision. The default summary is the first 600 characters of extracted
heading and text content, so it can include publisher boilerplate. The TOC is
made from headings and may duplicate a heading hit. Source XML is never
rewritten for indexing. Matching summary and outline hits are assembled before
body nodes, with relevance ordering within each tier.

The Python API supports lexical, vector, and hybrid modes:

```python
from docling_context import LocalContextStore, Principal, Retriever, SearchScope

with LocalContextStore("./context-data") as store:
    result = Retriever(store).search(
        Principal("default", "local"),
        "glacier",
        scope=SearchScope(uri="docling://resources/articles"),
        mode="lexical",
    )
```

Install `docling-context[vectors]` and supply an embedding provider for vector
and hybrid queries. The provider implements `model_id`, `dimensions`, and
`embed(list[str]) -> list[list[float]]`. The `TurbovecIndex` adapter uses
stable `uint64` IDs from SQLite, contiguous `float32` arrays, L2 normalization,
and `IdMapIndex` with a SQL-derived allowlist. Dimensions must be a multiple of
eight, from 8 to 16,384. One model and dimension generation is active per
store. Replacing the provider rebuilds the vector generation.

```python
retriever = Retriever(store, embedder=my_embedding_provider)
result = retriever.search(principal, "glacier", mode="hybrid")
```

The built-in local adapter supports direct text, image, and combined inputs:

```python
from docling_context import FastEmbedProvider, embed_text, embed_image, embed_text_image

provider = FastEmbedProvider("Qdrant/clip-ViT-B-32")
text_vector = embed_text(provider, "a geological cross section")
image_vector = embed_image(provider, image_bytes)
combined_vector = embed_text_image(provider, "a geological cross section", image_bytes)
```

The default `BAAI/bge-small-en-v1.5` model is text-only. With a text-only
provider, image input returns `None`, and a combined input uses the text only.
The API can search with an image via
`retriever.search(principal, "", image=image_bytes, mode="vector")` when the
selected provider supports images. A CLIP model can compare text queries with
image vectors in the same space. Picture assets referenced by source `<src>`
nodes are separate vector units with the picture XPath and an `asset_path` in
the search result. Image-only picture results have empty `text`; the CLI table
shows their asset path. Picture nodes with text use combined text and image
embeddings when the model supports both. Text-only models skip image-only
pictures and use the text from pictures that contain text. Existing stores
must run `dc index lex rebuild` before picture units are available.
For a picture hit, `store.get_document(principal, hit.uri).get_part_bytes(hit.asset_path)`
reads the cited image from its DCLX package.

SQLite stores vector metadata and embedding bytes. The turbovec snapshot is a
rebuildable projection. A missing or corrupt snapshot triggers a full rebuild;
compatible snapshots replay later outbox changes for only the affected URIs.
Changing the embedding model also requires a full rebuild. Search checks each
candidate against the current document revision and reads cited text from DCLX,
so stale index records are excluded.
The retriever enforces tenant, URI subtree, optional document, XPath subtree,
and tier constraints before searching. Parent and child hits are collapsed.
Source nodes retain their XML `parent_xpath`; `section_xpath` records the
preceding heading and contributes to structural proximity scoring.

SQLite builds with FTS5 use FTS5; builds without it use FTS3. The latter is
common in some Python distributions on macOS. Both remain indexed and support
the same lexical API. `find` and `grep` retain their existing substring-scan
behavior; `search` uses the retrieval index.

Run the fixed 60-document benchmark with `uv run --extra vectors
python examples/retrieval/benchmark.py`. It reports recall at five and median
and p95 query latency for a flat package scan, lexical, vector, and hybrid
search. The bundled hash embedder is a deterministic fixture for comparison,
not a semantic embedding model. Measure with a representative corpus and model
before setting production latency or recall targets. For a labeled corpus in
an existing store, create a JSON list such as:

```json
[
  {
    "query": "Koonap formation in the Karoo Basin",
    "relevant_uris": ["docling://resources/documents/example-paper"]
  }
]
```

Run `uv run --extra vectors python examples/retrieval/benchmark_corpus.py
--store ~/.local/share/docling-context --queries queries.json`. The command
reports recall at five and median and p95 latency for a flat package scan,
lexical retrieval, and, when a vector generation exists, vector and hybrid
retrieval. Supply several labeled queries and relevant URIs from your corpus
before using the results to set performance targets. Opening the store can
reconcile its index or vector snapshot.

On a local macOS Python 3.12 environment using SQLite FTS3, the fixture
benchmark measured the following warm-query results. Each mode searched 60
documents with 12 queries, repeated five times; recall is against the known
topic document at rank five. These figures are for the fixture only.

| Mode | Recall@5 | Median query (ms) | p95 query (ms) |
| --- | ---: | ---: | ---: |
| Flat package scan | 1.00 | 8.060 | 9.437 |
| Indexed lexical | 1.00 | 1.337 | 1.403 |
| Vector | 1.00 | 1.717 | 2.045 |
| Hybrid | 1.00 | 1.803 | 1.895 |

The filtered vector query returned the target at rank one for all 12 queries,
with a 0.349 ms median. The turbovec snapshot occupied 114,964 bytes.
Replacing one document and replaying its change took 19.323 ms; deleting
one and replaying its change took 15.121 ms. Restart and first vector query took
7.706 ms. These results only characterize this small fixture and host.

On a separate 71-document PDF corpus, five title-derived queries with one
known relevant document each produced these one-pass measurements on the same
macOS host. The corpus used the default BGE text model and SQLite FTS3; each
mode returned a relevant document in the top five for all five queries.

| Mode | Recall@5 | Median query (ms) | p95 query (ms) |
| --- | ---: | ---: | ---: |
| Flat package scan | 1.00 | 4808.376 | 4811.038 |
| Indexed lexical | 1.00 | 100.376 | 271.041 |
| Vector | 1.00 | 382.161 | 407.565 |
| Hybrid | 1.00 | 438.324 | 845.513 |

For this corpus, an initial regression target is p95 under one second for each
indexed mode. These five queries are drawn from document titles, so their
recall does not establish quality on paraphrased or cross-document questions.
Use a larger labeled query set and repeated runs before setting a production
target.
