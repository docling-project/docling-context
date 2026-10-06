# Storage, revisions, and jobs

`LocalContextStore` stores metadata in SQLite and DCLX blobs under SHA-256
paths. A logical URI points to the latest revision; old revisions remain
addressable by document ID, revision ID, and XPath. Every write also inserts
an outbox event in the same SQLite transaction. Blob reads verify their hash.

Resource URIs have the form `docling://resources/{collection}/{path}`. User
URIs have the form
`docling://users/{tenant}/{user}/{memories|sessions|skills}/{path}`. Tenant
ownership is checked on reads and writes. Local file ingestion requires an
explicit `allowed_roots` entry; bytes can be ingested without filesystem access.

Each ingested document is a DCLX package containing `document.xml` (L2), a
DocLang summary (L0), and a DocLang TOC (L1). Source assets and sidecars other
than the generated summary and TOC are kept from DCLX input. `context/manifest.json` records the
summary method and exact source revision. Source hashes, origin, converter
version/options, and warnings are in the revision's provenance.

Collection summaries are separate DCLX documents at
`docling://resources/{collection}/_context/summary`. Writes and deletes mark
the collection stale and enqueue a durable job. `CollectionWorker` claims jobs
with leases, retries failures, and can resume after a crash without adding a
duplicate summary revision. Its `status` method exposes staleness and the
summary revision ID.

The local and in-memory stores implement the same basic `ContextStore`
contract, leaving room for another storage backend later.
