# Step 1: Core contracts and local DocLang store

## Goal and dependency

Build an inspectable, local context tree whose canonical document payloads are DocLang documents. This is the foundation for Steps 2–6. No application document, summary, session, memory, or skill is stored as Markdown.

## Reference and boundary

- Use the DocLang checkout at `.references/doclang`, branch `feat/add-cpp-backend` (reviewed at `12a037a`), as an external dependency. Its public Python exports include `DoclangDocument` and `DoclangXDocument` (`DocLangXDocument` is the underlying class name).
- Treat the referenced project's checkout and the overview report as concept references only. Implement original interfaces, models, algorithms, documentation, and tests. Do not import or copy code, identifiers, prompts, or fixtures from that checkout.
- Pin a released DocLang version or an exact commit with the required native API. Record the dependency and its license in the project metadata. Add a compatibility smoke test before relying on a new version.

## Contracts to settle first

1. Define `docling://` URIs with explicit `resources/{collection}/...` and `users/{tenant}/{user}/memories|sessions|skills/...` namespaces. Normalize Unicode and path separators; reject `.`/`..`, empty segments, encoded separators, and namespace escapes. URI case policy must be deterministic.
2. Separate logical URI from immutable `document_id`, `revision_id`, package hash, and node address. A node address is `(document_id, revision_id, xpath)`; a changed source revision never silently rebinds an old citation.
3. Define `Principal`, `TenantScope`, `DocumentRecord`, `TreeEntry`, `PackageRef`, and `ChangeEvent` with versioned serializable fields. Every read or query accepts a principal and applies the same tenant rule.
4. Define interfaces for package storage, metadata/tree lookup, event publication, and transactions. The local implementation uses a filesystem package store plus SQLite; Step 6 supplies cloud implementations without changing SDK operations.
5. Define the read API: list children, walk tree with depth/limit, fetch package metadata, fetch DocLang document, and fetch bounded XPath content with page and bounding box where available. Define write/update/delete with expected revision for optimistic concurrency.

## Local layout and transaction design

- Store immutable `.dclx` blobs under a hash-derived path. SQLite stores URI-to-revision mapping, parent relationships, tenant ownership, package hash, source metadata, and an append-only change/outbox table. SQLite is the source of truth for the local namespace; package bytes are the source of truth for document content.
- Write a package to a temporary path, validate it, flush and atomically rename it, then commit the SQLite row and outbox event in one database transaction. On startup, reconcile orphaned blobs and missing packages; never return a row whose package is missing or fails hash verification.
- Read DCLX through `DoclangXDocument.read`; use `DoclangDocument.at`, `iterate_items`, `page_number`, and `bounding_box` only after input validation. Keep native objects inside a narrow adapter so future DocLang API changes have one integration point.
- Keep sidecar handling in the package adapter. The current native API exposes summary, TOC, and concepts as DocLang sidecars. It does not expose a general Python method for adding arbitrary archive parts. Any extra-part writer must preserve unknown parts, use a documented versioned manifest, and pass a read/write/read round-trip test. Do not assume the placeholder OPC files written by this checkout prove full OPC compliance.
- Define explicit limits for package size, ZIP expansion, XML depth, tree page size, and XPath result size. Validate paths inside an archive before extraction or repackaging.

## Work sequence

1. Create a Python package and dependency pin; add one smoke test for the DocLang native extension.
2. Implement URI parsing and tenant authorization as pure functions.
3. Implement package validation and local immutable blob storage.
4. Add SQLite schema, migrations, transaction/outbox handling, and tree operations.
5. Implement the storage interfaces with a small in-memory fake for later contract tests.

## Acceptance checks

- Create, read, replace, list, and delete DCLX documents across two tenants; cross-tenant reads and scoped searches fail closed.
- Restart the process and recover the same tree and package hashes.
- Simulate a failed package write and a failed SQLite commit; neither exposes a partial revision.
- Preserve source XML and unrelated archive members through a package round trip; verify citations include the exact revision and XPath.

## Exit artifact

A local library that can store and navigate DocLang packages with stable addresses and a backend-neutral contract. No converter, model, vector index, or cloud account is needed.
