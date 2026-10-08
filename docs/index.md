# Capabilities

`docling-context` stores document revisions as DocLang DCLX packages and exposes
them through Python, `dc`, MCP, and authenticated HTTP APIs.

| Capability | Current behavior |
| --- | --- |
| Storage | SQLite metadata and outbox; immutable, SHA-256-addressed DCLX packages |
| Namespaces | Tenant-scoped resources and user-scoped memories, sessions, and skills |
| Ingestion | Native DCLG and DCLX, plus Docling formats through optional local conversion |
| Context tiers | L0 document summary, L1 heading TOC, L2 source document in one package |
| Collection summaries | Durable jobs with retries, leases, and visible stale status |
| Conversion | Typed PDF and image settings for OCR, tables, charts, enrichment, images, and limits |
| Remote conversion | Opt-in Docling Serve adapter; explicit endpoint and bounded response |
| Inspection | `dc status`, `ls`, `tree`, and task status |
| Search | `dc search` provides scoped lexical, vector, and hybrid retrieval, including picture assets with an image-capable model |
| Sessions | `dc session` records, lists, replays, closes, and purges revisioned event packages |
| Durable memory | `dc memory` lists and reviews DCLX claims, runs compilation jobs, and supports scoped recall |
| Agent harnesses | One-command MCP integration for Codex, Claude Code, Hermes, and Pi |
| Public service | Versioned Python and authenticated HTTP operations for inspection, search, ingestion, jobs, sessions, and memory |

`find` and `grep` retain their bounded substring scans. Indexed retrieval
verifies citations against the current DCLX revision before returning text.

- [Conversion settings](conversion.md)
- [Scoped retrieval](retrieval.md)
- [Retrieval benchmark](../examples/retrieval/benchmark_corpus.py)
- [CLI commands](cli.md)
- [Storage and jobs](storage.md)
- [Agent sessions](sessions.md)
- [Durable memory](memory.md)
- [Agent harness installation and MCP](agents.md)
- [Single PDF and folder examples](../examples/adding_resources/README.md)
