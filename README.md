# docling-context

A local context tree that stores documents as immutable DocLang `.dclx` packages.
SQLite tracks logical URIs, revisions, tenant ownership, and an append-only change
outbox. Package bytes live under SHA-256 paths. The API is designed so another
storage backend can implement the same contracts later.

## Development setup

The project resolves DocLang from commit
`92d299026f2f985328b52424483c711e19b2f306` (Apache-2.0) through uv's Git
source configuration. No local DocLang checkout is required for development.
The source configuration is uv-specific; installers that ignore it resolve the
published `doclang` package, which may not yet have the required native API.

```bash
uv sync
uv run pytest -q tests/test_step1.py
```

## Local usage

```python
from doclang import DocLangXDocument
from docling_context import LocalContextStore, NodeAddress, Principal

document = DocLangXDocument()
assert document.read_xml('<doclang version="0.7"><text>Hello</text></doclang>')
package = document.write_bytes()
principal = Principal(tenant_id="acme", user_id="alice")

with LocalContextStore("./context-data") as store:
    record = store.put(principal, "docling://resources/manuals/hello", package)
    citation = NodeAddress(record.document_id, record.revision_id, "/doclang[1]/text[1]")
    print(store.read_node(principal, citation).text)
```

Document URIs use `docling://resources/{collection}/{path}` or
`docling://users/{tenant}/{user}/{memories|sessions|skills}/{path}`. Namespace
names are lowercase; path segments keep their case and normalize to Unicode NFC.
Encoded separators, traversal segments, and ambiguous escapes are rejected.
User paths require a matching principal. Resource rows are scoped by tenant in
storage, so the same resource URI can exist independently in two tenants.

`put` creates a document when `expected_revision` is omitted. To replace one,
pass its current revision ID. `delete` also requires the current revision ID.
Each revision keeps its own package hash and remains addressable by
`NodeAddress(document_id, revision_id, xpath)` after replacement or deletion.
Tree and event queries require a principal and have bounded page sizes.

The package adapter validates the native DCLX report and applies limits to the
compressed package, archive entries, XML node count and depth, and node reads.
Startup reconciliation marks missing or corrupt packages unavailable and removes
orphaned blobs. Reads verify SHA-256 before returning content. A blob can remain
after a failed SQLite transaction until the next reconciliation; no logical
revision or outbox event is exposed.
