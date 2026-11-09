# Agent sessions

Project-owned sessions use
`docling://projects/{project_id}/sessions/{session_id}`. Start one and launch
an agent with `dc session start --project materials --agent codex`. The CLI
installs the agent's MCP connection and `dc` skill, starts the project session,
waits for the agent, then closes the session when it exits. Use
`dc session list --project materials` to see the agent and state. The `--no-launch` flag creates a project session for an agent already running.

When an agent is already running, its installed `dc` skill uses MCP
`project_session_start`, `session_append`, and `project_session_close`. The
launcher passes `DOCLING_CONTEXT_PROJECT` and the full
`DOCLING_CONTEXT_SESSION` URI to its child. The child should append useful
outcomes; an agent conversation is not automatically copied into DCLX. The
project's session count appears in `dc overview`.

Closing a project session queues compilation of shared memory proposals; run
`dc memory worker --once` to process the queue. Project abstract/overview
jobs and ingestion jobs are separate queues.

The Python `SessionStore` still supports user-scoped private sessions. The
human-facing `dc session start` path now requires a project and agent.

`SessionStore` appends agent turns, tool calls, tool results, and feedback to a
revisioned DCLX package at
`docling://users/{tenant}/{user}/sessions/{session_id}`. Each event is a DocLang
`<text>` node. The `context/session.json` archive part records its kind, turn ID,
idempotency key, timestamp, and exact XPath. A raw attachment lives in a typed
`attachments/` archive part; metadata stores its size and SHA-256 hash.
Sessions have `open` and `closed` states. `start` creates an empty package;
`close` prevents further appends and queues the final revision for compilation.

```python
from docling_context import LocalContextStore, Principal, SessionStore

principal = Principal("acme", "alice")
with LocalContextStore("./context-data") as store:
    sessions = SessionStore(store)
    sessions.start(principal, "conversation-1")
    event = sessions.append(
        principal, "conversation-1", key="turn-1", kind="turn",
        text="I prefer short answers", turn_id="turn-1",
    )
    print(event.xpath, event.revision_id)
    print(sessions.replay(principal, "conversation-1"))
    sessions.close(principal, "conversation-1")
```

Repeated appends with the same key return the existing event. The writer checks
the prior revision before replacing the package, so concurrent writes must retry
after a revision conflict. Sessions contain at most 1,000 events, each at most
20,000 characters. An attachment is limited to 4 MiB, and the session has a
32 MiB total attachment limit. The DCLX package is
portable and can be replayed after restarting the store.

`trim(principal, id, keep_last=n)` creates a revision with the last `n` events.
Older immutable revisions remain addressable. `delete` removes the logical
session; `purge` removes its revision history, attachments, compilation jobs,
and recall ledger. A source deletion also moves memories with no surviving
citations back to review.
`list` filters current sessions by status and update time; the matching CLI
command is `dc session list`. See the [CLI guide](cli.md) for start, append,
replay, close, retention, and job commands.

Session URIs and attachments require the owning tenant and user. This version
does not expose sharing of private sessions.
