# Step 4: Sessions and durable memory

## Goal and dependency

Capture agent activity as DocLang and turn explicitly selected, supported observations into reviewable long-term memory. Depends on the storage, jobs, and retrieval contracts from Steps 1–3.

## Data model

- A session is a revisioned DocLang document in a DCLX package under `docling://users/{tenant}/{user}/sessions/{session_id}`. Represent turns, tool calls, tool results, and feedback with a documented DocLang vocabulary/profile. Keep attachment references and raw large tool payloads as typed archive parts with size and retention limits.
- A memory is its own DCLX package under `.../memories/{kind}/{id}`; supported kinds begin with preference, fact, entity, concept, and procedure. The DocLang body carries the readable claim and explanation. Typed metadata records confidence, status, author/generator, valid time, and source citations `(session/document revision, xpath)`.
- Links between memories and source nodes live in an indexed edge table and, where portable, a versioned package part. The index is rebuilt from package metadata; a package alone must still expose its provenance.
- Define statuses `proposed`, `accepted`, `rejected`, `superseded`, and `deleted`. A model may propose a memory but does not silently turn an uncertain observation into a durable fact. User corrections override generated claims and remain traceable.

## Compilation and recall

1. Append session events using idempotency keys and preserve turn order. Publish a session update through the outbox.
2. A compiler reads a bounded session revision, extracts candidate claims, checks for equivalent or contradictory existing memories, and emits proposed DCLX memory packages with source XPaths. Use deterministic rules where possible; optional local models enrich extraction.
3. Acceptance commits a new memory revision and refreshes retrieval indexes. Rejection records the decision so a retry does not propose the same claim indefinitely.
4. Recall searches accepted memories within the user/tenant scope, records delivered `(memory_id, revision_id)` in a recall ledger for the session, and suppresses redundant repeats while allowing a newer corrected revision.
5. Deleting a session or document evaluates dependent memories. Mark unsupported claims for review or withdrawal, rebuild links, and ensure retrieval does not serve deleted content. Keep explicit retention and purge operations.

## Conflict and privacy rules

- Store provenance and validity separately from semantic similarity. Similar text is not proof of the same claim; contradictory dates or subjects must be reviewed.
- Scope private memory to the owning tenant and user by default. Shared memory requires an explicit sharing record and the same authorization checks as documents.
- Keep secrets and raw tool arguments out of long-term memory by default. Add configurable redaction before compilation and a way to remove a claim and its index entries.
- The session writer and memory compiler must work with no cloud services. If models are unavailable, session logging and manual memory creation still work.

## Work sequence

1. Define and validate the session and memory DocLang profiles.
2. Implement append, replay, and retention for session packages.
3. Add compilation jobs, provenance links, and review transitions.
4. Add memory search and recall ledger integration.
5. Add correction, supersession, and deletion cascades.

## Acceptance checks

- Replay a session after restart with the same ordered turns and citations.
- Compile twice and get no duplicate accepted memory; reject a proposal and verify retries respect the rejection.
- Correct a preference and verify recall returns the accepted newer revision with its provenance.
- Delete a source and verify unsupported claims are flagged or withdrawn and all index entries are updated.
- Attempt cross-user and cross-tenant reads through each memory operation and verify denial.

## Exit artifact

Portable DocLang session and memory packages with reviewable provenance, correction, and bounded recall.
