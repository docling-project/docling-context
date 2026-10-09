# Durable memory

Reviewed memories and compiled project session outcomes are DCLX packages in
the flat `docling://resources/memories/{memory_id}` list. A memory may link to
several projects. `dc resource add memories FILE [--project ID]` accepts a
DCLX package with a valid memory profile.

`MemoryService` stores each claim as a DCLX package under
`docling://resources/memories/{memory_id}`. Supported kinds are
`preference`, `fact`, `entity`, `concept`, and `procedure`. The DocLang body
contains the readable claim and explanation. `context/memory.json` stores
confidence, status, author, optional validity dates, source citations, conflict
references, and review history. A citation names a source URI, document ID,
revision ID, and DocLang XPath. SQLite indexes these links and can reconstruct
them from the packages.

```python
from docling_context import (
    LocalContextStore, MemoryCompiler, MemoryService, Principal, SessionStore,
)

principal = Principal("acme", "alice")
with LocalContextStore("./context-data") as store:
    sessions = SessionStore(store)
    sessions.append(
        principal, "conversation-1", key="feedback-1", kind="feedback",
        text="Remember: I prefer short answers",
    )
    compiler = MemoryCompiler(store)
    while compiler.run_once() is not None:
        pass
    memories = MemoryService(store)
    for proposal in memories.list(principal, status="proposed"):
        print(proposal.claim, proposal.citations)
        memories.accept(principal, proposal.uri)
```

The compiler accepts only explicit `Remember:` or `Remember that` statements
and `I prefer` turns or feedback. It ignores tool calls and tool results. It
redacts common credential patterns before writing a proposal; callers can pass
a custom `redactor` to `MemoryCompiler`. Compilation runs locally and requires
no model or cloud service. Jobs have leases and retries. Exact normalized
claim matches are deduplicated, including rejected proposals, so retries do not
undo a review decision. Similar or conflicting claims are left for review.

`create` makes a proposed memory by default. A direct user-authored call may
pass `accepted=True`. Compiler proposals require `accept` before they enter
search. `reject`, `correct`, and `delete` write new package revisions. A
correction is user authored, accepted, and records the revision it supersedes.
`purge` physically removes a deleted claim and its revisions. `search` returns
accepted memories within the tenant. `recall(principal, session_id, query)`
records delivered memory and revision IDs; it suppresses repeats in that
session, while a corrected revision can be delivered again.
`list` includes all non-purged statuses by default and can filter by status,
kind, or update time. The `dc memory` CLI exposes listing, review, compilation,
search, recall, and purge; see the [CLI guide](cli.md).

Deleting a cited session or document checks dependent memories. A claim with
no surviving cited source becomes `proposed` and leaves the retrieval index.
Reads are tenant scoped. Review and purge require the memory's owner.
Closing a project session queues compilation; the worker links each resulting
memory to that project.
