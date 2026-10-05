# Step 2: Ingestion and hierarchical summaries

## Goal and dependency

Turn source files and existing DocLang artifacts into revisioned DCLX packages, with L0 and L1 context in DocLang sidecars and the full L2 document intact. Depends on the package and outbox contracts from Step 1.

## Input and package contract

- Support `.dclg`/`.dclg.xml` and `.dclx` first. Add local Docling conversion for selected formats behind a `Converter` interface; add a `docling-serve` adapter only after local conversion works. Record converter version, options, source hash, and origin URI in provenance.
- The canonical result is one DCLX package per logical document revision. If input is DCLG, wrap its validated XML in DCLX. If input is DCLX, preserve its recognized sidecars and opaque assets. Never flatten a source document to Markdown.
- L0 is a short DocLang summary using `set_document_summary`; L1 uses the native DocLang TOC sidecar via `set_toc`. The full `document.xml` is L2. Concepts may use `set_concepts`. For future sidecar types, use a context-specific package manifest and DocLang or typed structured parts, with explicit schema version and source revision.
- Collection-level summaries are separate DocLang documents stored as DCLX packages under reserved collection URIs. Their provenance lists child revision IDs. Rebuild them when children change. This avoids pretending a directory is a sidecar of one child package.

## Pipeline

1. Fingerprint source bytes and conversion settings. Repeated ingestion with an identical fingerprint is idempotent unless `force` is requested.
2. Convert or load, validate DocLang, preserve source geometry/assets, and capture conversion warnings.
3. Walk heading and semantic nodes to build an outline. Use deterministic extraction first; optional local LLM/VLM providers enrich summaries. Model output must be validated as DocLang and tied to the exact revision.
4. Create L0/L1 sidecars. On model failure, generate bounded extractive summaries and structural outlines, mark their generation method, and continue ingestion.
5. Package, validate, commit the new revision, and emit `DocumentCommitted`. Schedule collection-summary and indexing jobs through the local outbox.

## Job and update semantics

- Jobs have durable IDs, input revision, status (`queued`, `running`, `complete`, `failed`), attempt count, and retry time in SQLite. Workers claim leases so a crash can be retried safely.
- Derived sidecars are immutable per revision. Enrichment creates a new package revision and emits an update; consumers never combine a sidecar from one revision with XML from another.
- If a source document is replaced or deleted, mark dependent collection summaries stale, then rebuild them. Expose stale status to retrieval until the new derived revision is committed.
- Limit converter resources and archive sizes. Keep source path/URL access behind explicit allow rules; no automatic network fetching is required for fully local operation.

## Work sequence

1. Implement the DCLG/DCLX import path and package round trips.
2. Implement heading-tree extraction, TOC generation, and deterministic L0 fallback.
3. Add local Docling conversion and optional model enrichment.
4. Add durable jobs, retries, and collection-summary invalidation.
5. Add remote conversion adapter with the same output contract.

## Acceptance checks

- Ingest one native DCLX, one DCLG XML file, and one converted PDF. Reopen every result with `DoclangXDocument`, validate L0/L1 as DocLang, and resolve sample XPaths and geometry.
- Repeat ingestion and verify no duplicate revision or jobs; change source bytes and verify a new revision.
- Kill a worker after conversion and resume without duplicate visible state.
- Fail the model provider and verify local ingestion and structural retrieval still work.

## Exit artifact

An `add-resource` library operation and background worker that yield portable DCLX packages with traceable, revision-consistent context tiers.
