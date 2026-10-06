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

Schema version 3 adds `retrieval_units`, an FTS table, and `vector_state`.
Schema version 4 adds `vector_members`, which records the stable IDs in each
snapshot so later outbox events can remove replaced or deleted nodes.
Schema version 5 records picture asset paths on retrieval units. Existing
stores can run `dc index lex rebuild` to add picture units from saved DCLX
packages, then `dc index vector rebuild` to embed them with an image-capable
model.
Schema version 6 records each source node's active heading in `section_xpath`.
Rebuild the lexical index after upgrading an existing store to populate that
field for previously indexed nodes.
Document writes replace their retrieval records in the same SQLite transaction
as the revision and outbox event; deletes remove those records. Existing stores
are backfilled from their DCLX packages on upgrade. The FTS table is a
rebuildable lexical projection. Optional vector snapshots are held in
`vectors-{generation}.tvim` files and can be reconstructed from SQLite
embedding bytes. The package and current revision remain authoritative for
all citations.

The local and in-memory stores implement the same basic `ContextStore`
contract, leaving room for another storage backend later.
