# `dc` CLI

Install the package to expose `dc`. For PDF input, install the `conversion`
extra. The command uses a local store at `~/.local/share/docling-context` by
default; pass `--store PATH` to choose another directory. `--tenant` and
`--user` select the principal; their defaults are `default` and `local`.
Use `uv run dc` or activate the project environment; a system calculator also
uses the name `dc` on some machines.

```bash
uv run --extra conversion dc --store ./context-data add-resource ./paper.pdf \
  --collection papers --no-ocr --tables --no-charts
uv run dc --store ./context-data task status JOB_ID
uv run dc --store ./context-data worker --once
uv run dc --store ./context-data status
uv run dc --store ./context-data ls docling://resources/
uv run dc --store ./context-data tree docling://resources/papers -L 2
uv run dc --store ./context-data find "invoice" --uri docling://resources/papers
uv run dc --store ./context-data grep "total" --uri docling://resources/papers
```

`add-resource` accepts one document or a folder of documents. With the
`conversion` extra installed, it accepts Docling's supported formats, including
PDF, DOCX, PPTX, XLSX, images, HTML, and plain text. Native DCLG and DCLX files
also work without that extra. Folder imports scan the top level by default.
Add `-r` or `--recursive` to include subfolders; those paths are retained in
the URI. Use repeatable `--from FORMAT` options to select Docling format names,
such as `--from pdf --from docx --from image` (default: all). Filtering uses
filename extensions; Docling then detects the actual input format. Files with
the same stem and different extensions receive distinct URIs. A single file
can use `--uri`; otherwise `--collection` defaults to `documents`. Use `--config`
for a [JSON conversion config](conversion.md), with command flags overriding it.
Use `--force` to create a new revision for unchanged input.
Table reconstruction always uses accurate mode; there is no CLI mode switch.
Page and picture images are on by default, and there is no default page limit.
`--no-page-images`, `--no-picture-images`, and `--max-pages N` override these
defaults. OCR uses RapidOCR with English PP-OCRv6 `tiny` models when enabled.

Ingestion is synchronous. Each new resource revision queues a collection-summary
job; `add-resource` prints the revision and its `task_id`. `task status` reports
that job. Run `worker --once` for one due job, or `worker` continuously. A
collection summary may remain stale until its latest job completes.

`find` and `grep` are case-insensitive substring scans of current documents.
They scan at most 1,000 documents, 2,000 nodes per document, and 50,000 nodes
total, returning at most 100 hits. They accept
an optional URI subtree scope and enforce tenant and user access. They do not
yet provide embedding search or relevance ranking.
