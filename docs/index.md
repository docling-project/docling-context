# Capabilities

`docling-context` stores document revisions as DocLang DCLX packages and exposes
them through local Python and `dc` command-line APIs.

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
| Search | `dc find` and `grep` provide bounded lexical scans of current documents |

Search currently scans stored documents and has no semantic ranking or vector
index. The indexed retrieval work is planned separately.

- [Conversion settings](conversion.md)
- [CLI commands](cli.md)
- [Storage and jobs](storage.md)
- [Single PDF and folder examples](../examples/adding_resources/README.md)
