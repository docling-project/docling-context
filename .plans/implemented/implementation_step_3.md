# Step 3: Scoped retrieval and context assembly

## Goal and dependency

Find relevant DocLang nodes within authorized URI and XPath scopes, then assemble bounded context with exact citations. Depends on Steps 1–2. The system must answer lexical queries with no embedding model installed.

## Index records and interfaces

- A retrieval unit is a source document or semantic node with `tenant_id`, `uri`, `document_id`, `revision_id`, `xpath`, `parent_xpath`, `tier`, page, bounding box, text for indexing, and an internal stable `uint64` vector ID. SQLite maps the vector ID to the full address and stores lifecycle state.
- Use SQLite tables for tree/path filters and FTS5 for lexical search. Keep a `VectorIndex` interface (`upsert`, `remove`, `search(query, allowed_ids, k)`, `checkpoint`, `rebuild`) so the retrieval planner is independent of index implementation.
- Store the embedding model ID, dimensions, normalization rule, input hash, and index generation with each record. Never mix vectors from different model/dimension generations; rebuild a new generation and switch it atomically.
- Index L0 collection/document summaries, L1 outlines/section descriptions, and selected L2 semantic units. Preserve DocLang structure: split at headings and safe semantic boundaries; tables, formulas, and captions remain addressable units. Keep source XML untouched.

## Local vector store choice

- Evaluate [turbovec `IdMapIndex`](https://github.com/ryancodrai/turbovec) as the initial local implementation because it exposes stable external IDs, `remove`, `search(..., allowlist=...)`, and `write`/`load`/`sync` persistence. Use `IdMapIndex`, not positional IDs that can move after deletion. Its Python API expects contiguous `float32` arrays; document and validate dimensional requirements.
- Resolve tenant, URI-prefix, tier, and XPath-prefix constraints in SQLite first. Supply the resulting vector IDs as turbovec's allowlist, then combine vector results with FTS results. Recheck every returned ID against current revision and principal before opening a package.
- Treat turbovec's index file as a rebuildable projection, not the canonical document store. Record an index generation and committed outbox offset in SQLite. On restart, load a compatible snapshot and replay later events; if missing or inconsistent, rebuild from canonical DCLX packages and metadata.
- Benchmark filtered recall, search latency, index size, restart time, update/delete behavior, and embedding dimensions on representative local corpora. If a platform or dimension is unsupported, retain the FTS path and switch the vector adapter rather than changing public APIs. Repository benchmark claims are hypotheses for this workload, not acceptance results.

## Retrieval algorithm

1. Authenticate and normalize tenant, URI subtree, optional document, and optional XPath scope.
2. Read permitted candidates from SQLite, then run FTS and optional allowlist vector search only within those IDs.
3. Merge and rerank by relevance, tier, freshness, and structural proximity; collapse overlapping parent/child hits.
4. Fetch cited nodes from the exact DCLX revision. Return L0/L1 first when they satisfy the requested budget; load L2 only for selected nodes. Respect per-result and total token limits.
5. Return score components, URI, revision, XPath, page/bbox when present, tier, and truncation status. No citation may point to a stale or missing revision.

## Work sequence

1. Build deterministic node extraction and SQLite FTS records.
2. Implement scoped lexical search and cited XPath reads.
3. Add embedding provider interface and turbovec adapter with checkpoint/rebuild logic.
4. Add hybrid ranking and context assembly.
5. Measure quality and latency against a flat-search baseline with a fixed corpus and query set.

## Acceptance checks

- Scoped results never include another tenant, sibling subtree, or excluded XPath branch, including when an index contains stale IDs.
- Delete and replace a document, restart, and verify no old vector result is returned. Corrupt or remove the vector snapshot and verify a correct rebuild.
- Return the same cited DocLang content before and after index rebuild; enforce requested budgets and deterministic truncation.
- Report measured recall and latency for lexical-only, vector-only, and hybrid modes; set performance targets from the baseline rather than promising a fixed subsecond result in advance.

## Exit artifact

A local retrieval engine with exact DocLang citations, optional embeddings, scoped hybrid search, and a replaceable vector backend.
