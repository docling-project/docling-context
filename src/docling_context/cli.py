"""The local dc command for ingestion, inspection, and basic lexical lookup."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict, replace
from datetime import UTC, date, datetime, time
from pathlib import Path

from tabulate import tabulate  # type: ignore[import-untyped]
from tqdm import tqdm  # type: ignore[import-untyped]

from .catalog import CATALOG_SCHEMA_VERSION, Catalog, new_document_uri
from .compiler import MemoryCompiler
from .conversion_config import PdfConversionConfig
from .converters import LocalDoclingConverter
from .durable_memory import MemoryRecord, MemoryService, SourceCitation
from .embeddings import (
    CLIP_MODEL,
    DEFAULT_MODEL,
    local_embedding_provider,
    stored_embedding_provider,
)
from .ingestion import Ingestor
from .local import LocalContextStore
from .models import Principal
from .package import MAX_PACKAGE_BYTES, bounded_nodes
from .profiles import (
    DESCRIPTOR_NAMES,
    MAX_ATTACHMENT_BYTES,
    MEMORY_KINDS,
    MEMORY_STATUSES,
    profile_kind,
    validate_memory,
)
from .project_context import ProjectContextWorker
from .retrieval import RetrievalHit, Retriever, SearchScope
from .sessions import SessionInfo, SessionStore
from .uri import authorize_uri, parse_uri
from .vectors import TurbovecIndex


def _json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _status(
    values: Mapping[str, object],
    *,
    json_output: bool,
    fields: tuple[str, ...] | None = None,
) -> None:
    selected = fields or tuple(values)
    if json_output:
        _json({field: values[field] for field in selected})
        return
    rows = []
    for field in selected:
        value = values[field]
        if isinstance(value, bool):
            display = "yes" if value else "no"
        elif value is None:
            display = "—"
        else:
            display = str(value)
        rows.append((field.replace("_", " ").capitalize(), display))
    print(
        tabulate(
            rows,
            headers=("Field", "Value"),
            tablefmt="rounded_grid",
            disable_numparse=True,
        )
    )


def _table_or_json(
    values: Sequence[Mapping[str, object]],
    *,
    headers: tuple[str, ...],
    json_output: bool,
) -> None:
    if json_output:
        _json(values)
    elif values:
        print(
            tabulate(
                [[row.get(key, "") for key in headers] for row in values],
                headers=tuple(key.replace("_", " ").title() for key in headers),
                tablefmt="rounded_grid",
                disable_numparse=True,
                maxcolwidths=tuple(
                    72 if key in {"claim", "text"} else None for key in headers
                ),
            )
        )
    else:
        print("No records.")


def _rows_or_json_lines(
    values: Sequence[Mapping[str, object]],
    *,
    headers: tuple[str, ...],
    json_output: bool,
) -> None:
    if json_output:
        for value in values:
            _json(value)
    else:
        _table_or_json(values, headers=headers, json_output=False)


def _session_rows(items: tuple[SessionInfo, ...]) -> list[dict[str, object]]:
    return [asdict(item) for item in items]


def _memory_rows(items: tuple[MemoryRecord, ...]) -> list[dict[str, object]]:
    return [asdict(item) for item in items]


def _session_id(value: str | None) -> str:
    session_id = value or os.environ.get("DOCLING_CONTEXT_SESSION")
    if not session_id:
        raise ValueError("session ID required; pass it or set DOCLING_CONTEXT_SESSION")
    return session_id


def _timestamp_filter(value: str | None, *, upper: bool = False) -> str | None:
    if value is None:
        return None
    try:
        if len(value) == 10:
            moment = datetime.combine(
                date.fromisoformat(value),
                time.max if upper else time.min,
                tzinfo=UTC,
            )
        else:
            moment = datetime.fromisoformat(value)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=UTC)
    except ValueError as exc:
        raise ValueError("date filter needs an ISO date or timestamp") from exc
    return moment.astimezone(UTC).isoformat()


def _input_text(args: argparse.Namespace, *, name: str = "text") -> str:
    value = getattr(args, name, None)
    option = getattr(args, "text_option", None)
    if option is not None:
        if value is not None:
            raise ValueError("use either positional text or --text")
        value = option
    if args.stdin:
        if value is not None:
            raise ValueError("use either text argument or --stdin")
        value = sys.stdin.read(20_001)
    if value is None:
        raise ValueError("text argument or --stdin is required")
    return value


def _advance_progress(bar: tqdm, completed: int, total: int) -> None:
    if bar.total != total:
        bar.total = total
        bar.refresh()
    bar.update(completed - bar.n)


def _search_excerpt(text: str) -> str:
    compact = " ".join(text.split())
    if len(compact) <= 128:
        return compact
    return f"{compact[:64]} ... {compact[-64:]}"


def _search_table(hits: tuple[RetrievalHit, ...]) -> str:
    if not hits:
        return "No matches."
    rows = [
        (
            f"{hit.score:.3f}",
            f"{hit.lexical_score:.3f}",
            f"{hit.vector_score:.3f}",
            f"{hit.tier_score:.3f}",
            f"{hit.freshness_score:.3f}",
            f"{hit.proximity_score:.3f}",
            hit.document_id,
            hit.xpath,
            _search_excerpt(hit.text) if hit.text else f"[image: {hit.asset_path}]",
        )
        for hit in hits
    ]
    return tabulate(
        rows,
        headers=(
            "Score",
            "Lex",
            "Vec",
            "Tier",
            "Fresh",
            "Near",
            "Document ID",
            "XPath",
            "Excerpt",
        ),
        tablefmt="rounded_grid",
        maxcolwidths=(None, None, None, None, None, None, None, 48, 72),
        disable_numparse=True,
    )


def _prefix(value: str) -> str:
    return parse_uri(value.rstrip("/"), prefix=True).value


def _format_extensions() -> dict[str, tuple[str, ...]]:
    try:
        from docling.datamodel.base_models import FormatToExtensions
    except ImportError:
        return {
            "pdf": ("pdf",),
            "xml_doclang": ("dclg", "dclg.xml"),
            "dclx": ("dclx",),
        }
    return {
        format.value: tuple(extension.lower() for extension in extensions)
        for format, extensions in FormatToExtensions.items()
    }


def _formats_for(path: Path, extensions: dict[str, tuple[str, ...]]) -> set[str]:
    name = path.name.lower()
    if name.endswith((".dclg", ".dclg.xml")):
        return {"xml_doclang"}
    if name.endswith(".dclx"):
        return {"dclx"}
    return {
        format
        for format, suffixes in extensions.items()
        if any(name.endswith(f".{suffix}") for suffix in suffixes)
    }


def _config(args: argparse.Namespace) -> PdfConversionConfig:
    config = (
        PdfConversionConfig.from_json_file(args.config)
        if args.config is not None
        else PdfConversionConfig()
    )
    changes = {
        key: value
        for key, value in {
            "do_ocr": args.ocr,
            "do_table_structure": args.tables,
            "do_chart_extraction": args.charts,
            "generate_page_images": args.page_images,
            "generate_picture_images": args.picture_images,
            "max_pages": args.max_pages,
            "document_timeout": args.timeout,
        }.items()
        if value is not None
    }
    return replace(config, **changes)


def _add_resource(
    args: argparse.Namespace, store: LocalContextStore, principal: Principal
) -> None:
    source = args.source.resolve(strict=True)
    extensions = _format_extensions()
    requested = {
        "xml_doclang" if value == "dclg" else value for value in args.from_formats or ()
    }
    unknown = requested - extensions.keys()
    if unknown:
        raise ValueError(f"unknown source format: {', '.join(sorted(unknown))}")

    def selected(path: Path) -> bool:
        formats = _formats_for(path, extensions)
        return bool(formats and (not requested or formats & requested))

    if source.is_dir():
        candidates = source.rglob("*") if args.recursive else source.iterdir()
        paths = sorted(path for path in candidates if path.is_file() and selected(path))
        if not paths:
            raise ValueError(
                "folder contains no documents matching the selected formats"
            )
        base = source
    else:
        if args.recursive:
            raise ValueError("--recursive requires a folder source")
        if not source.is_file():
            raise ValueError("source must be a file or folder")
        if not selected(source):
            raise ValueError("source does not match a supported selected format")
        paths = [source]
        base = source.parent
    ingestor = Ingestor(
        store,
        allowed_roots=(base,),
        converter=LocalDoclingConverter(_config(args)),
    )
    with Catalog(store) as catalog:
        if args.project:
            catalog.get_project(principal, args.project)
        imported: list[dict[str, object]] = []
        for path in paths:
            if path.stat().st_size > MAX_PACKAGE_BYTES:
                raise ValueError(f"source file is too large: {path}")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            with store.transaction():
                uri = catalog.find_source(principal, digest) or new_document_uri()
                record = ingestor.add_resource(principal, uri, path, force=args.force)
                catalog.register_resource(principal, record)
                if args.project:
                    catalog.link(principal, args.project, record.uri)
            imported.append(
                {
                    "uri": record.uri,
                    "revision_id": record.revision_id,
                    "source_sha256": digest,
                    "project_id": args.project,
                }
            )
        _rows_or_json_lines(
            imported,
            headers=("uri", "revision_id", "source_sha256", "project_id"),
            json_output=args.json,
        )


def _scan(
    store: LocalContextStore,
    principal: Principal,
    term: str,
    prefix: str,
    *,
    document_limit: int = 1_000,
    hit_limit: int = 100,
) -> list[dict[str, object]]:
    address = parse_uri(prefix, prefix=True)
    authorize_uri(principal, address)
    base = address.value
    descendant = base + "/"
    rows = store.db.execute(
        """SELECT uri FROM documents WHERE tenant_id=? AND available=1
           AND (uri=? OR substr(uri,1,?)=?) ORDER BY uri LIMIT ?""",
        (principal.tenant_id, base, len(descendant), descendant, document_limit),
    ).fetchall()
    needle = term.casefold()
    if not needle:
        raise ValueError("search term cannot be empty")
    hits: list[dict[str, object]] = []
    remaining_nodes = 50_000
    for row in rows:
        if remaining_nodes <= 0:
            break
        record = store.get_record(principal, row["uri"])
        document = store.get_document(principal, record.uri)
        if (
            profile_kind(record.uri) == "memories"
            and validate_memory(document)["status"] != "accepted"
        ):
            continue
        nodes = bounded_nodes(
            document, limit=min(2_000, remaining_nodes), text_bytes=2_048
        )
        remaining_nodes -= len(nodes)
        for node in nodes:
            content = str(node["text"])
            if needle in content.casefold():
                hits.append(
                    {
                        "uri": record.uri,
                        "revision_id": record.revision_id,
                        "xpath": node["xpath"],
                        "text": content[:500],
                        "page": node["page"],
                    }
                )
                if len(hits) >= hit_limit:
                    return hits
    return hits


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dc", description="Local DocLang context tools"
    )
    parser.add_argument(
        "--store",
        type=Path,
        default=Path(
            os.environ.get("DOCLING_CONTEXT_STORE", "~/.local/share/docling-context")
        ).expanduser(),
        help="Local DCLX and index directory (default: ~/.local/share/docling-context).",
    )
    parser.add_argument(
        "--tenant",
        default=os.environ.get("DOCLING_CONTEXT_TENANT", "default"),
        help="Tenant scope (default: default).",
    )
    parser.add_argument(
        "--user",
        default=os.environ.get("DOCLING_CONTEXT_USER", "local"),
        help="User scope (default: local).",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Initialize the local SQLite store")
    init.add_argument(
        "--json", action="store_true", help="Print JSON instead of a table"
    )
    overview = commands.add_parser(
        "overview", help="List projects and linked resource counts"
    )
    overview.add_argument("--json", action="store_true")
    overview.add_argument("--limit", type=int, default=100)
    overview.add_argument("--offset", type=int, default=0)
    project = commands.add_parser(
        "project", help="Create and inspect projects and links"
    )
    project_commands = project.add_subparsers(dest="project_command", required=True)
    project_create = project_commands.add_parser("create", help="Create a project")
    project_create.add_argument("project_id")
    project_create.add_argument("--title", required=True)
    project_show = project_commands.add_parser("show", help="Show project details")
    project_show.add_argument("project_id")
    for name in ("link", "unlink"):
        command = project_commands.add_parser(
            name, help=f"{'Link' if name == 'link' else 'Unlink'} a shared resource"
        )
        command.add_argument("project_id")
        command.add_argument("uri")
    project_list = project_commands.add_parser(
        "resources", help="List linked resources"
    )
    project_list.add_argument("project_id")
    project_refresh = project_commands.add_parser(
        "refresh", help="Queue abstract and overview regeneration"
    )
    project_refresh.add_argument("project_id")
    project_jobs = project_commands.add_parser(
        "jobs", help="Show abstract and overview generation jobs"
    )
    project_jobs.add_argument("project_id")
    project_commands.add_parser("worker", help="Process one queued project context job")
    for name in ("description", "abstract", "overview"):
        command = project_commands.add_parser(
            f"set-{name}", help=f"Set the project {name} document"
        )
        command.add_argument("project_id")
        command.add_argument("source", type=Path, help="DCLX or DCLG source file")
    project_session_start = project_commands.add_parser(
        "session-start", help="Start a project session"
    )
    project_session_start.add_argument("project_id")
    project_session_start.add_argument(
        "--id", help="Session ID (default: generated UUID)"
    )
    project_session_list = project_commands.add_parser(
        "session-list", help="List project sessions"
    )
    project_session_list.add_argument("project_id")
    project_session_append = project_commands.add_parser(
        "session-append", help="Append a project session event"
    )
    project_session_append.add_argument("project_id")
    project_session_append.add_argument("session_id")
    project_session_append.add_argument("text")
    project_session_append.add_argument(
        "--kind",
        choices=("turn", "tool_call", "tool_result", "feedback"),
        required=True,
    )
    project_session_append.add_argument("--key", required=True)
    for name in ("session-show", "session-close"):
        command = project_commands.add_parser(
            name,
            help=(
                "Show a project session"
                if name == "session-show"
                else "Close a project session"
            ),
        )
        command.add_argument("project_id")
        command.add_argument("session_id")
    for command in project_commands.choices.values():
        command.add_argument(
            "--json", action="store_true", help="Print JSON instead of a table"
        )
    resource = commands.add_parser("resource", help="Create a shared DCLX resource")
    resource_commands = resource.add_subparsers(dest="resource_command", required=True)
    resource_add = resource_commands.add_parser(
        "add", help="Import a shared DCLX resource"
    )
    resource_add.add_argument(
        "type", choices=("memories", "skills", "concepts", "knowledge")
    )
    resource_add.add_argument("source", type=Path, help="DCLX or DCLG source file")
    resource_add.add_argument(
        "--metadata",
        type=Path,
        help="JSON metadata for a skill, concept, or knowledge descriptor",
    )
    resource_add.add_argument("--project", help="Link the new resource to a project")
    resource_link = resource_commands.add_parser(
        "link", help="Link two shared resources"
    )
    resource_link.add_argument("source_uri")
    resource_link.add_argument("target_uri")
    resource_link.add_argument("--relation", required=True)
    resource_link.add_argument("--source-revision")
    resource_link.add_argument("--source-xpath")
    resource_links = resource_commands.add_parser(
        "links", help="List links for a shared resource"
    )
    resource_links.add_argument("uri")
    resource_projects = resource_commands.add_parser(
        "projects", help="List projects linked to a shared resource"
    )
    resource_projects.add_argument("uri")
    for command in resource_commands.choices.values():
        command.add_argument(
            "--json", action="store_true", help="Print JSON instead of a table"
        )
    knowledge = commands.add_parser(
        "knowledge", help="Write and inspect cited structured facts"
    )
    knowledge_commands = knowledge.add_subparsers(
        dest="knowledge_command", required=True
    )
    knowledge_add = knowledge_commands.add_parser(
        "add-fact", help="Add a schema-checked fact with an exact citation"
    )
    for field in ("knowledge_uri", "entity_type", "entity_id", "property", "value"):
        knowledge_add.add_argument(field)
    for field in ("source-uri", "source-revision", "source-xpath"):
        knowledge_add.add_argument(f"--{field}", required=True)
    knowledge_facts = knowledge_commands.add_parser(
        "facts", help="List facts in a knowledge dataset"
    )
    knowledge_facts.add_argument("knowledge_uri")
    for command in knowledge_commands.choices.values():
        command.add_argument(
            "--json", action="store_true", help="Print JSON instead of a table"
        )
    status = commands.add_parser("status", help="Show store and job counts")
    status.add_argument(
        "--json", action="store_true", help="Print JSON instead of a table."
    )
    index = commands.add_parser(
        "index", help="Inspect or rebuild lexical and vector indexes"
    )
    index_commands = index.add_subparsers(dest="index_command", required=True)
    index_status = index_commands.add_parser(
        "status", help="Show lexical and vector index status"
    )
    index_status.add_argument(
        "--json", action="store_true", help="Print JSON instead of a table."
    )
    lex = index_commands.add_parser(
        "lex", help="Inspect or rebuild indexed document nodes"
    )
    lex_commands = lex.add_subparsers(dest="index_operation", required=True)
    lex_status = lex_commands.add_parser(
        "status", help="Show indexed document and node counts"
    )
    lex_status.add_argument(
        "--json", action="store_true", help="Print JSON instead of a table."
    )
    lex_rebuild = lex_commands.add_parser(
        "rebuild", help="Rebuild search records from stored DCLX documents"
    )
    lex_rebuild.add_argument(
        "--json", action="store_true", help="Print JSON instead of a table"
    )
    vector = index_commands.add_parser(
        "vector", help="Inspect or rebuild vector embeddings"
    )
    vector_commands = vector.add_subparsers(dest="index_operation", required=True)
    vector_status = vector_commands.add_parser(
        "status", help="Show vector model and coverage"
    )
    vector_status.add_argument(
        "--json", action="store_true", help="Print JSON instead of a table."
    )
    vector_rebuild = vector_commands.add_parser(
        "rebuild", help="Embed indexed nodes and rebuild the vector snapshot"
    )
    vector_rebuild.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"FastEmbed text model or {CLIP_MODEL} (default: {DEFAULT_MODEL}).",
    )
    vector_rebuild.add_argument(
        "--offline",
        action="store_true",
        help="Use cached model files only (default: download missing files).",
    )
    vector_rebuild.add_argument(
        "--backend",
        choices=("auto", "mlx", "onnx"),
        default="auto",
        help="Embedding runtime (default: MLX for supported Apple Silicon models, else ONNX).",
    )
    vector_rebuild.add_argument(
        "--json", action="store_true", help="Print JSON instead of a table"
    )
    add = commands.add_parser(
        "add-resource",
        help="Import one supported document or a folder of documents",
        description="Import any supported Docling or DocLang document, or a folder containing them.",
        epilog="Defaults shown apply without --config. Explicit flags override settings in the config file.",
    )
    add.add_argument(
        "source",
        type=Path,
        help="A supported document file or folder; folders scan their top level by default.",
    )
    add.add_argument(
        "-r",
        "--recursive",
        action="store_true",
        help="Include documents in subfolders (default: off; folders only).",
    )
    add.add_argument(
        "--from",
        dest="from_formats",
        action="append",
        metavar="FORMAT",
        help="Include a source format (repeatable; default: all; e.g. pdf, docx, xlsx, image).",
    )
    add.add_argument("--project", help="Link imported documents to this project")
    add.add_argument(
        "--config",
        type=Path,
        help="Conversion settings JSON (default: built-in settings).",
    )
    add.add_argument(
        "--ocr",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Read text in scanned PDFs and images with OCR (default: on).",
    )
    add.add_argument(
        "--tables",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Reconstruct table rows and cells in accurate mode (default: on).",
    )
    add.add_argument(
        "--charts",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Extract structured data from charts (default: off).",
    )
    add.add_argument(
        "--page-images",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Keep rendered page images in DCLX (default: on).",
    )
    add.add_argument(
        "--picture-images",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Keep extracted picture images in DCLX (default: on).",
    )
    add.add_argument(
        "--max-pages",
        type=int,
        help="Maximum pages converted per document (default: unlimited).",
    )
    add.add_argument(
        "--timeout",
        type=float,
        help="PDF or image conversion time limit in seconds (default: none).",
    )
    add.add_argument(
        "--force",
        action="store_true",
        help="Create a new revision for unchanged input (default: off).",
    )
    add.add_argument(
        "--json", action="store_true", help="Print JSON lines instead of a table"
    )
    jobs = commands.add_parser("jobs", help="Inspect durable ingestion jobs")
    jobs_commands = jobs.add_subparsers(dest="jobs_command", required=True)
    jobs_status = jobs_commands.add_parser("status", help="Show one job by ID")
    jobs_status.add_argument("job_id")
    jobs_status.add_argument("--json", action="store_true")
    listing = commands.add_parser("ls", help="List direct children")
    listing.add_argument("uri")
    listing.add_argument(
        "--json", action="store_true", help="Print JSON lines instead of a table"
    )
    tree = commands.add_parser("tree", help="Walk a context subtree")
    tree.add_argument("uri")
    tree.add_argument("-L", "--depth", type=int, default=2)
    tree_output = tree.add_mutually_exclusive_group()
    tree_output.add_argument(
        "--json", action="store_true", help="Print JSON lines instead of a table"
    )
    tree_output.add_argument(
        "--raw", action="store_true", help="Print an indented URI tree"
    )
    outline = commands.add_parser("outline", help="Read a document TOC sidecar")
    outline.add_argument("uri")
    outline.add_argument("--revision", help="Immutable document revision")
    outline_output = outline.add_mutually_exclusive_group()
    outline_output.add_argument("--json", action="store_true")
    outline_output.add_argument("--raw", action="store_true", help="Print TOC XML only")
    show = commands.add_parser("show", help="Read one cited DocLang node")
    show.add_argument("uri")
    show.add_argument("xpath")
    show.add_argument("--revision", help="Immutable document revision")
    show.add_argument("--format", choices=("text", "xml"), default="text")
    show.add_argument("--max-chars", type=int, default=8_192)
    show_output = show.add_mutually_exclusive_group()
    show_output.add_argument("--json", action="store_true")
    show_output.add_argument(
        "--raw", action="store_true", help="Print node content only"
    )
    for name in ("find", "grep"):
        search = commands.add_parser(name, help="Bounded lexical lookup")
        search.add_argument("term")
        search.add_argument("--uri", default="docling://resources/")
        search.add_argument(
            "--json", action="store_true", help="Print JSON lines instead of a table"
        )
    retrieval = commands.add_parser(
        "search", help="Search indexed DocLang nodes with exact citations"
    )
    retrieval.add_argument(
        "query", nargs="?", default="", help="Query text (optional with --image)."
    )
    retrieval.add_argument(
        "--image", type=Path, help="Image query file (vector mode only)."
    )
    retrieval.add_argument(
        "--mode",
        choices=("auto", "lexical", "vector", "hybrid"),
        default="auto",
        help="Search mode (default: hybrid if vectors exist, else lexical).",
    )
    retrieval.add_argument(
        "--uri",
        default="docling://resources/",
        help="Authorized URI subtree (default: all resources).",
    )
    retrieval.add_argument(
        "--project", help="Search only resources linked to this project"
    )
    retrieval.add_argument(
        "--document", help="Restrict to one document URI (default: all in scope)."
    )
    retrieval.add_argument(
        "--xpath", help="Restrict to one source XPath subtree (default: all nodes)."
    )
    retrieval.add_argument(
        "--tier",
        type=int,
        choices=(0, 1, 2),
        action="append",
        help="Include a context tier: 0 summary, 1 outline, 2 source (repeatable; default: all).",
    )
    retrieval.add_argument(
        "-k", type=int, default=10, help="Maximum cited results (default: 10)."
    )
    retrieval.add_argument(
        "--max-tokens",
        type=int,
        default=2_000,
        help="Maximum tokens across returned citations (default: 2000).",
    )
    retrieval.add_argument(
        "--per-result-tokens",
        type=int,
        default=500,
        help="Maximum tokens per citation (default: 500).",
    )
    retrieval.add_argument(
        "--json",
        action="store_true",
        help="Print complete JSON records instead of the compact table (default: table).",
    )

    session = commands.add_parser("session", help="Record and inspect agent sessions")
    session_commands = session.add_subparsers(dest="session_command", required=True)
    session_start = session_commands.add_parser("start", help="Create an open session")
    session_start.add_argument("--id", help="Session ID (default: generated UUID)")
    session_list = session_commands.add_parser("list", help="List your sessions")
    session_list.add_argument(
        "--status",
        choices=("open", "closed"),
        help="Filter lifecycle state (default: all)",
    )
    for command in (session_list,):
        command.add_argument(
            "--since", help="Updated on or after this ISO date or timestamp"
        )
        command.add_argument(
            "--until", help="Updated on or before this ISO date or timestamp"
        )
        command.add_argument(
            "--limit", type=int, default=100, help="Page size, 1–1000 (default: 100)"
        )
        command.add_argument(
            "--offset", type=int, default=0, help="Skip this many matches (default: 0)"
        )
    for name in ("show", "replay", "close", "delete", "purge", "jobs"):
        action = "List compilation jobs for" if name == "jobs" else f"{name.title()}"
        command = session_commands.add_parser(name, help=f"{action} one session")
        command.add_argument("session_id", help="Session ID or full URI")
        command.add_argument(
            "--json", action="store_true", help="Print JSON instead of a table"
        )
    session_append = session_commands.add_parser(
        "append", help="Append one session event"
    )
    session_append.add_argument(
        "session_id", nargs="?", help="ID or DOCLING_CONTEXT_SESSION"
    )
    session_append.add_argument("text", nargs="?", help="Event text, or use --stdin")
    session_append.add_argument(
        "--text",
        dest="text_option",
        help="Event text, useful with DOCLING_CONTEXT_SESSION",
    )
    session_append.add_argument(
        "--stdin", action="store_true", help="Read event text from stdin"
    )
    session_append.add_argument(
        "--kind",
        required=True,
        choices=("turn", "tool_call", "tool_result", "feedback"),
    )
    session_append.add_argument("--key", required=True, help="Stable idempotency key")
    session_append.add_argument(
        "--turn-id", default="", help="Group related events by turn (default: empty)"
    )
    session_append.add_argument(
        "--attachment", type=Path, help="Optional bounded binary attachment"
    )
    session_append.add_argument(
        "--content-type",
        default="application/octet-stream",
        help="Attachment MIME type (default: application/octet-stream)",
    )
    session_trim = session_commands.add_parser(
        "trim", help="Retain recent session events"
    )
    session_trim.add_argument("session_id")
    session_trim.add_argument("--keep-last", type=int, required=True)
    session_attachment = session_commands.add_parser(
        "attachment", help="Save one event attachment"
    )
    session_attachment.add_argument("session_id")
    session_attachment.add_argument("key", help="Idempotency key of the attached event")
    session_attachment.add_argument("--output", type=Path, required=True)
    session_attachment.add_argument("--json", action="store_true")
    for command in (session_start, session_list, session_append, session_trim):
        command.add_argument("--json", action="store_true")

    memory = commands.add_parser("memory", help="Review and recall durable memories")
    memory_commands = memory.add_subparsers(dest="memory_command", required=True)
    memory_list = memory_commands.add_parser(
        "list", help="List your non-purged memories"
    )
    memory_list.add_argument(
        "--status",
        choices=sorted(MEMORY_STATUSES),
        help="Filter review state (default: all)",
    )
    memory_list.add_argument(
        "--kind", choices=sorted(MEMORY_KINDS), help="Filter memory kind (default: all)"
    )
    memory_list.add_argument(
        "--since", help="Updated on or after this ISO date or timestamp"
    )
    memory_list.add_argument(
        "--until", help="Updated on or before this ISO date or timestamp"
    )
    memory_list.add_argument(
        "--limit", type=int, default=100, help="Page size, 1–1000 (default: 100)"
    )
    memory_list.add_argument(
        "--offset", type=int, default=0, help="Skip this many matches (default: 0)"
    )
    memory_proposals = memory_commands.add_parser(
        "proposals", help="List claims awaiting review"
    )
    memory_proposals.add_argument("--kind", choices=sorted(MEMORY_KINDS))
    memory_proposals.add_argument(
        "--limit", type=int, default=100, help="Page size, 1–1000 (default: 100)"
    )
    memory_proposals.add_argument(
        "--offset", type=int, default=0, help="Skip this many matches (default: 0)"
    )
    for command in (memory_list, memory_proposals):
        command.add_argument(
            "--json", action="store_true", help="Print JSON instead of a table"
        )
    for name in ("show", "accept", "reject", "delete", "purge"):
        command = memory_commands.add_parser(name, help=f"{name.title()} one memory")
        command.add_argument("uri")
        command.add_argument("--json", action="store_true")
        if name in {"reject", "delete"}:
            command.add_argument("--reason", default="")
    memory_correct = memory_commands.add_parser(
        "correct", help="Write an accepted correction"
    )
    memory_correct.add_argument("uri")
    memory_correct.add_argument("claim", nargs="?")
    memory_correct.add_argument("--stdin", action="store_true")
    memory_correct.add_argument("--explanation")
    memory_correct.add_argument("--json", action="store_true")
    memory_create = memory_commands.add_parser(
        "create", help="Create a user-authored claim"
    )
    memory_create.add_argument("kind", choices=sorted(MEMORY_KINDS))
    memory_create.add_argument("claim", nargs="?")
    memory_create.add_argument("--stdin", action="store_true")
    memory_create.add_argument("--explanation", default="")
    memory_create.add_argument("--subject")
    memory_create.add_argument("--confidence", type=float, default=1.0)
    memory_create.add_argument(
        "--accepted",
        action="store_true",
        help="Accept this user-authored claim immediately",
    )
    for field in ("uri", "document_id", "revision_id", "xpath"):
        memory_create.add_argument(f"--source-{field.replace('_', '-')}")
    memory_create.add_argument("--json", action="store_true")
    memory_search = memory_commands.add_parser("search", help="Search accepted claims")
    memory_search.add_argument("query", nargs="?", default="")
    memory_search.add_argument("--limit", type=int, default=10)
    memory_search.add_argument("--json", action="store_true")
    memory_recall = memory_commands.add_parser(
        "recall", help="Recall claims once per session revision"
    )
    memory_recall.add_argument("query", nargs="?", default="")
    memory_recall.add_argument("--session", help="ID or DOCLING_CONTEXT_SESSION")
    memory_recall.add_argument("--limit", type=int, default=10)
    memory_recall.add_argument("--json", action="store_true")
    memory_worker = memory_commands.add_parser(
        "worker", help="Process queued memory compilation jobs"
    )
    memory_worker.add_argument(
        "--once", action="store_true", help="Process one due job then exit"
    )
    memory_worker.add_argument("--json", action="store_true")
    memory_jobs = memory_commands.add_parser(
        "jobs", help="List compilation jobs for a session"
    )
    memory_jobs.add_argument("session_id")
    memory_jobs.add_argument("--limit", type=int, default=100)
    memory_jobs.add_argument("--json", action="store_true")
    return parser


def _session_command(
    args: argparse.Namespace, store: LocalContextStore, principal: Principal
) -> None:
    sessions = SessionStore(store)
    command = args.session_command
    if command == "start":
        _status(asdict(sessions.start(principal, args.id)), json_output=args.json)
    elif command == "list":
        since = _timestamp_filter(args.since)
        until = _timestamp_filter(args.until, upper=True)
        if since and until and since > until:
            raise ValueError("--since is later than --until")
        items = sessions.list(
            principal,
            status=args.status,
            since=since,
            until=until,
            limit=args.limit,
            offset=args.offset,
        )
        _table_or_json(
            _session_rows(items),
            headers=(
                "session_id",
                "status",
                "event_count",
                "updated_at",
                "revision_id",
            ),
            json_output=args.json,
        )
    elif command == "show":
        info = sessions.get(principal, args.session_id)
        values = asdict(info)
        jobs = MemoryCompiler(store).jobs(principal, args.session_id)
        values["compilation"] = jobs[0].status if jobs else "none"
        _status(values, json_output=args.json)
    elif command == "append":
        session_id = _session_id(args.session_id)
        sessions.get(principal, session_id)
        if args.attachment and args.attachment.stat().st_size > MAX_ATTACHMENT_BYTES:
            raise ValueError("session attachment exceeds 4 MiB")
        event = sessions.append(
            principal,
            session_id,
            key=args.key,
            kind=args.kind,
            text=_input_text(args),
            turn_id=args.turn_id,
            attachment=args.attachment.read_bytes() if args.attachment else None,
            content_type=args.content_type,
        )
        _status(asdict(event), json_output=args.json)
    elif command == "replay":
        events = sessions.replay(principal, args.session_id)
        _table_or_json(
            [asdict(event) for event in events],
            headers=("sequence", "kind", "turn_id", "text", "xpath", "created_at"),
            json_output=args.json,
        )
    elif command == "close":
        _status(
            asdict(sessions.close(principal, args.session_id)), json_output=args.json
        )
    elif command == "trim":
        sessions.trim(principal, args.session_id, keep_last=args.keep_last)
        _status(asdict(sessions.get(principal, args.session_id)), json_output=args.json)
    elif command == "attachment":
        payload = sessions.read_attachment(principal, args.session_id, args.key)
        args.output.write_bytes(payload)
        _status(
            {"output": str(args.output), "size_bytes": len(payload)},
            json_output=args.json,
        )
    elif command == "delete":
        sessions.delete(principal, args.session_id)
        _status(
            {"session_id": args.session_id, "status": "deleted"}, json_output=args.json
        )
    elif command == "purge":
        count = sessions.purge(principal, args.session_id)
        _status(
            {"session_id": args.session_id, "purged_revisions": count},
            json_output=args.json,
        )
    elif command == "jobs":
        jobs = MemoryCompiler(store).jobs(principal, args.session_id)
        _table_or_json(
            [asdict(job) for job in jobs],
            headers=(
                "job_id",
                "status",
                "attempts",
                "last_error",
                "session_revision_id",
            ),
            json_output=args.json,
        )


def _memory_command(
    args: argparse.Namespace, store: LocalContextStore, principal: Principal
) -> None:
    memories = MemoryService(store)
    command = args.memory_command
    if command in {"list", "proposals"}:
        since = _timestamp_filter(args.since) if command == "list" else None
        until = _timestamp_filter(args.until, upper=True) if command == "list" else None
        if since and until and since > until:
            raise ValueError("--since is later than --until")
        items = memories.list(
            principal,
            status=args.status if command == "list" else "proposed",
            kind=args.kind,
            since=since,
            until=until,
            limit=args.limit,
            offset=args.offset,
        )
        _table_or_json(
            _memory_rows(items),
            headers=(
                "memory_id",
                "kind",
                "status",
                "claim",
                "confidence",
                "updated_at",
            ),
            json_output=args.json,
        )
    elif command == "show":
        _status(asdict(memories.get(principal, args.uri)), json_output=args.json)
    elif command == "create":
        fields = (
            args.source_uri,
            args.source_document_id,
            args.source_revision_id,
            args.source_xpath,
        )
        if any(fields) and not all(fields):
            raise ValueError("provide all four --source-* citation fields")
        citations = (SourceCitation(*fields),) if all(fields) else ()
        record = memories.create(
            principal,
            args.kind,
            _input_text(args, name="claim"),
            explanation=args.explanation,
            subject=args.subject,
            confidence=args.confidence,
            citations=citations,
            accepted=args.accepted,
        )
        _status(asdict(record), json_output=args.json)
    elif command in {"accept", "reject", "correct", "delete", "purge"}:
        if command == "accept":
            record = memories.accept(principal, args.uri)
        elif command == "reject":
            record = memories.reject(principal, args.uri, reason=args.reason)
        elif command == "correct":
            record = memories.correct(
                principal,
                args.uri,
                _input_text(args, name="claim"),
                explanation=args.explanation,
            )
        elif command == "delete":
            record = memories.delete(principal, args.uri, reason=args.reason)
        else:
            count = memories.purge(principal, args.uri)
            _status({"uri": args.uri, "purged_revisions": count}, json_output=args.json)
            return
        _status(asdict(record), json_output=args.json)
    elif command in {"search", "recall"}:
        items = (
            memories.search(principal, args.query, limit=args.limit)
            if command == "search"
            else memories.recall(
                principal, _session_id(args.session), args.query, limit=args.limit
            )
        )
        _table_or_json(
            _memory_rows(items),
            headers=("memory_id", "kind", "claim", "confidence", "revision_id"),
            json_output=args.json,
        )
    elif command == "jobs":
        jobs = MemoryCompiler(store).jobs(principal, args.session_id, limit=args.limit)
        _table_or_json(
            [asdict(job) for job in jobs],
            headers=(
                "job_id",
                "status",
                "attempts",
                "last_error",
                "session_revision_id",
            ),
            json_output=args.json,
        )
    elif command == "worker":
        compiler = MemoryCompiler(store)
        if args.once:
            job = compiler.run_once()
            _status(asdict(job) if job else {"job": "none"}, json_output=args.json)
        else:
            import time

            try:
                while True:
                    if compiler.run_once() is None:
                        time.sleep(1)
            except KeyboardInterrupt:
                pass


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    principal = Principal(args.tenant, args.user)
    try:
        if args.command == "init":
            with LocalContextStore(args.store):
                pass
            _status(
                {
                    "catalog_schema_version": CATALOG_SCHEMA_VERSION,
                    "store": str(args.store.resolve()),
                },
                json_output=args.json,
            )
            return 0
        if args.command == "overview":
            with LocalContextStore(args.store) as store, Catalog(store) as catalog:
                rows = catalog.overview(principal, limit=args.limit, offset=args.offset)
            _table_or_json(
                rows,
                headers=(
                    "project_id",
                    "title",
                    "documents",
                    "memories",
                    "skills",
                    "concepts",
                    "knowledge",
                    "sessions",
                ),
                json_output=args.json,
            )
            return 0
        with LocalContextStore(args.store) as store:
            if args.command == "project":
                with Catalog(store) as catalog:
                    if args.project_command == "create":
                        _status(
                            asdict(
                                catalog.create_project(
                                    principal, args.project_id, args.title
                                )
                            ),
                            json_output=args.json,
                        )
                    elif args.project_command == "show":
                        _status(
                            asdict(catalog.get_project(principal, args.project_id)),
                            json_output=args.json,
                        )
                    elif args.project_command == "resources":
                        _rows_or_json_lines(
                            catalog.project_resources(principal, args.project_id),
                            headers=("resource_type", "resource_id", "uri"),
                            json_output=args.json,
                        )
                    elif args.project_command == "refresh":
                        catalog.refresh_project(principal, args.project_id)
                        _status(
                            {"project_id": args.project_id, "status": "queued"},
                            json_output=args.json,
                        )
                    elif args.project_command == "jobs":
                        _table_or_json(
                            catalog.context_jobs(principal, args.project_id),
                            headers=(
                                "kind",
                                "status",
                                "generation",
                                "attempts",
                                "last_error",
                                "updated_at",
                            ),
                            json_output=args.json,
                        )
                    elif args.project_command == "worker":
                        context_job = ProjectContextWorker(store).run_once()
                        _status(
                            asdict(context_job) if context_job else {"job": "none"},
                            json_output=args.json,
                        )
                    elif args.project_command.startswith("set-"):
                        kind = args.project_command.removeprefix("set-")
                        catalog.get_project(principal, args.project_id)
                        source = args.source.resolve(strict=True)
                        if not source.name.lower().endswith(
                            (".dclx", ".dclg", ".dclg.xml")
                        ):
                            raise ValueError(
                                "project context source must be DCLX or DCLG"
                            )
                        uri = f"docling://projects/{args.project_id}/.{kind}.dclx"
                        record = Ingestor(
                            store, allowed_roots=(source.parent,)
                        ).add_resource(principal, uri, source)
                        catalog.set_project_document(
                            principal, args.project_id, kind, record
                        )
                        _status(
                            {"uri": record.uri, "revision_id": record.revision_id},
                            json_output=args.json,
                        )
                    elif args.project_command == "session-list":
                        _rows_or_json_lines(
                            catalog.project_sessions(principal, args.project_id),
                            headers=("session_id", "status", "owner_id"),
                            json_output=args.json,
                        )
                    elif args.project_command.startswith("session-"):
                        catalog.get_project(principal, args.project_id)
                        session_id = (
                            args.id or uuid.uuid4().hex
                            if args.project_command == "session-start"
                            else args.session_id
                        )
                        uri = f"docling://projects/{args.project_id}/sessions/{session_id}"
                        sessions = SessionStore(store)
                        if args.project_command == "session-start":
                            info = sessions.start(principal, uri)
                            catalog.register_session(
                                principal, args.project_id, session_id
                            )
                            _status(asdict(info), json_output=args.json)
                        elif args.project_command == "session-append":
                            catalog.project_sessions(principal, args.project_id)
                            info = sessions.get(principal, uri)
                            event = sessions.append(
                                principal,
                                info.uri,
                                key=args.key,
                                kind=args.kind,
                                text=args.text,
                            )
                            _status(asdict(event), json_output=args.json)
                        elif args.project_command == "session-close":
                            info = sessions.close(principal, uri)
                            catalog.register_session(
                                principal,
                                args.project_id,
                                session_id,
                                status="closed",
                            )
                            _status(asdict(info), json_output=args.json)
                        else:
                            _status(
                                asdict(sessions.get(principal, uri)),
                                json_output=args.json,
                            )
                    elif args.project_command == "link":
                        catalog.link(principal, args.project_id, args.uri)
                        _status(
                            {"project_id": args.project_id, "uri": args.uri},
                            json_output=args.json,
                        )
                    else:
                        catalog.unlink(principal, args.project_id, args.uri)
                        _status(
                            {"project_id": args.project_id, "uri": args.uri},
                            json_output=args.json,
                        )
            elif args.command == "resource":
                with Catalog(store) as catalog:
                    if args.resource_command == "links":
                        _rows_or_json_lines(
                            catalog.resource_links(principal, args.uri),
                            headers=(
                                "source_uri",
                                "target_uri",
                                "relation",
                                "source_revision",
                                "source_xpath",
                            ),
                            json_output=args.json,
                        )
                    elif args.resource_command == "projects":
                        _rows_or_json_lines(
                            catalog.resource_projects(principal, args.uri),
                            headers=("project_id", "title"),
                            json_output=args.json,
                        )
                    elif args.resource_command == "link":
                        catalog.link_resources(
                            principal,
                            args.source_uri,
                            args.target_uri,
                            args.relation,
                            source_revision=args.source_revision,
                            source_xpath=args.source_xpath,
                        )
                        _status(
                            {
                                "source_uri": args.source_uri,
                                "target_uri": args.target_uri,
                                "relation": args.relation,
                            },
                            json_output=args.json,
                        )
                    else:
                        source = args.source.resolve(strict=True)
                        if not source.name.lower().endswith(
                            (".dclx", ".dclg", ".dclg.xml")
                        ):
                            raise ValueError(
                                "shared resource source must be DCLX or DCLG"
                            )
                        if args.project:
                            catalog.get_project(principal, args.project)
                        uri = f"docling://resources/{args.type}/{uuid.uuid4().hex}"
                        metadata = None
                        if args.type in DESCRIPTOR_NAMES:
                            supplied = (
                                json.loads(args.metadata.read_text())
                                if args.metadata is not None
                                else {}
                            )
                            if not isinstance(supplied, dict):
                                raise ValueError(
                                    "resource metadata must be a JSON object"
                                )
                            singular = DESCRIPTOR_NAMES[args.type]
                            metadata = {
                                **supplied,
                                "profile": f"{singular}-v1",
                                f"{singular}_id": uri.rsplit("/", 1)[-1],
                                "name": supplied.get("name", source.stem),
                                "summary": supplied.get("summary", ""),
                            }
                            if args.type == "concepts":
                                for field in (
                                    "entity_types",
                                    "relationship_types",
                                    "properties",
                                ):
                                    metadata.setdefault(field, [])
                            elif args.type == "knowledge":
                                metadata.setdefault("fields", {})
                        record = Ingestor(
                            store, allowed_roots=(source.parent,)
                        ).add_resource(
                            principal, uri, source, profile_metadata=metadata
                        )
                        with store.transaction():
                            catalog.register_resource(principal, record, args.type)
                            if args.type == "knowledge":
                                assert metadata is not None
                                fields = metadata["fields"]
                                assert isinstance(fields, dict)
                                catalog.register_knowledge_dataset(
                                    principal, uri, fields
                                )
                            if args.project:
                                catalog.link(principal, args.project, uri)
                        _status(
                            {
                                "uri": uri,
                                "revision_id": record.revision_id,
                                "project_id": args.project,
                            },
                            json_output=args.json,
                        )
            elif args.command == "knowledge":
                catalog = Catalog(store)
                if args.knowledge_command == "add-fact":
                    fact_id = catalog.add_fact(
                        principal,
                        args.knowledge_uri,
                        entity_type=args.entity_type,
                        entity_id=args.entity_id,
                        property=args.property,
                        value=json.loads(args.value),
                        source_uri=args.source_uri,
                        source_revision=args.source_revision,
                        source_xpath=args.source_xpath,
                    )
                    _status({"fact_id": fact_id}, json_output=args.json)
                else:
                    _table_or_json(
                        catalog.knowledge_facts(principal, args.knowledge_uri),
                        headers=(
                            "fact_id",
                            "entity_type",
                            "entity_id",
                            "property",
                            "value",
                            "source_uri",
                            "source_revision",
                            "source_xpath",
                        ),
                        json_output=args.json,
                    )
            elif args.command == "status":
                counts = {
                    table: store.db.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE tenant_id=?",
                        (principal.tenant_id,),
                    ).fetchone()[0]
                    for table in ("documents", "revisions", "jobs")
                }
                user_prefix = (
                    f"docling://users/{principal.tenant_id}/{principal.user_id}/"
                )
                session_prefix = user_prefix + "sessions/"
                counts["sessions"] = store.db.execute(
                    """SELECT COUNT(*) FROM documents WHERE tenant_id=? AND
                       ((substr(uri,1,?)=?) OR uri LIKE 'docling://projects/%/sessions/%')""",
                    (principal.tenant_id, len(session_prefix), session_prefix),
                ).fetchone()[0]
                counts["memories"] = store.db.execute(
                    """SELECT COUNT(*) FROM dc_resources WHERE tenant_id=?
                       AND resource_type='memories'""",
                    (principal.tenant_id,),
                ).fetchone()[0]
                counts["memory_jobs"] = store.db.execute(
                    """SELECT COUNT(*) FROM memory_compile_jobs
                       WHERE tenant_id=? AND user_id=?""",
                    (principal.tenant_id, principal.user_id),
                ).fetchone()[0]
                _status(
                    {"store": str(store.root.resolve()), **counts},
                    json_output=args.json,
                )
            elif args.command == "index":
                if args.index_command == "status":
                    _status(store.index_status(principal), json_output=args.json)
                elif args.index_command == "lex":
                    if args.index_operation == "status":
                        _status(
                            store.index_status(principal),
                            json_output=args.json,
                            fields=("documents", "indexed_documents", "indexed_nodes"),
                        )
                    else:
                        total = store.index_status(principal)["documents"]
                        assert isinstance(total, int)
                        with tqdm(
                            total=total,
                            desc="Indexing documents",
                            unit="doc",
                            file=sys.stderr,
                            disable=not sys.stderr.isatty(),
                        ) as bar:
                            rebuild_result = store.rebuild_index(
                                principal,
                                progress=lambda done, size: _advance_progress(
                                    bar, done, size
                                ),
                            )
                        _status(rebuild_result, json_output=args.json)
                else:
                    if args.index_operation == "status":
                        _status(
                            store.index_status(principal),
                            json_output=args.json,
                            fields=(
                                "vector_model",
                                "indexed_nodes",
                                "embedded_nodes",
                                "vector_snapshot_current",
                            ),
                        )
                    else:
                        store.require_exclusive_vector_scope(principal)
                        total = store.index_status(principal)["indexed_nodes"]
                        assert isinstance(total, int)
                        with tqdm(
                            total=total,
                            desc="Indexing vectors",
                            unit="node",
                            file=sys.stderr,
                            disable=not sys.stderr.isatty(),
                        ) as bar:
                            provider = local_embedding_provider(
                                args.model, backend=args.backend, offline=args.offline
                            )
                            TurbovecIndex(
                                store,
                                provider,
                                force_rebuild=True,
                                progress=lambda done, size: _advance_progress(
                                    bar, done, size
                                ),
                            )
                        _status(store.index_status(principal), json_output=args.json)
            elif args.command == "add-resource":
                _add_resource(args, store, principal)
            elif args.command == "session":
                _session_command(args, store, principal)
            elif args.command == "memory":
                _memory_command(args, store, principal)
            elif args.command == "ls":
                _rows_or_json_lines(
                    [
                        asdict(entry)
                        for entry in store.list_children(principal, _prefix(args.uri))
                    ],
                    headers=("uri", "kind", "document_id", "revision_id"),
                    json_output=args.json,
                )
            elif args.command == "jobs":
                from .service import ContextService, JobStatusRequest

                job_data = (
                    ContextService(store, principal)
                    .job_status(JobStatusRequest(args.job_id))
                    .data
                )
                _status(job_data, json_output=args.json)
            elif args.command == "tree":
                entries = store.walk(
                    principal, _prefix(args.uri), depth=args.depth, limit=1_000
                )
                if args.raw:
                    for entry in entries:
                        print(f"{'  ' * (entry.depth - 1)}{entry.uri}")
                else:
                    _rows_or_json_lines(
                        [asdict(entry) for entry in entries],
                        headers=("depth", "uri", "kind", "document_id", "revision_id"),
                        json_output=args.json,
                    )
            elif args.command in {"outline", "show"}:
                from .service import ContextService, OutlineRequest, ShowRequest

                service = ContextService(store, principal)
                if args.command == "outline":
                    value = service.outline(
                        OutlineRequest(args.uri, args.revision)
                    ).data
                    if args.raw:
                        print(value["toc_xml"])
                    else:
                        _status(value, json_output=args.json)
                else:
                    value = service.show(
                        ShowRequest(
                            args.uri,
                            args.xpath,
                            args.revision,
                            args.max_chars,
                            args.format,
                        )
                    ).data
                    if args.raw:
                        print(value["content"])
                    else:
                        _status(value, json_output=args.json)
            elif args.command in {"find", "grep"}:
                hits = _scan(store, principal, args.term, _prefix(args.uri))
                if args.command == "find":
                    hits.sort(
                        key=lambda hit: (
                            str(hit["text"]).casefold().count(args.term.casefold())
                        ),
                        reverse=True,
                    )
                _rows_or_json_lines(
                    hits,
                    headers=("uri", "revision_id", "xpath", "text", "page"),
                    json_output=args.json,
                )
            elif args.command == "search":
                state = store.db.execute(
                    "SELECT model_id FROM vector_state WHERE singleton=1"
                ).fetchone()
                mode = args.mode
                if mode == "auto":
                    mode = "hybrid" if state is not None else "lexical"
                if args.image is not None and mode == "lexical":
                    raise ValueError("image search needs a vector index")
                if mode in {"vector", "hybrid"}:
                    if state is None:
                        raise ValueError(
                            "run 'dc index vector rebuild' before vector search"
                        )
                    store.require_exclusive_vector_scope(principal)
                    retriever = Retriever(
                        store, embedder=stored_embedding_provider(state["model_id"])
                    )
                else:
                    retriever = Retriever(store)
                result = retriever.search(
                    principal,
                    args.query,
                    image=args.image.read_bytes() if args.image is not None else None,
                    scope=SearchScope(
                        uri=_prefix(args.uri),
                        xpath=args.xpath,
                        document_uri=args.document,
                        tiers=tuple(args.tier) if args.tier else (0, 1, 2),
                        project_id=args.project,
                    ),
                    k=args.k,
                    mode=mode,
                    max_tokens=args.max_tokens,
                    per_result_tokens=args.per_result_tokens,
                )
                if args.json:
                    for retrieval_hit in result.hits:
                        _json(asdict(retrieval_hit))
                else:
                    print(_search_table(result.hits))
                    if result.total_hits is None:
                        print(f"Showing {len(result.hits)} ranked nodes.")
                    else:
                        print(
                            f"Showing {len(result.hits)} of {result.total_hits} matching nodes."
                        )
    except (
        OSError,
        ValueError,
        RuntimeError,
        KeyError,
        PermissionError,
        sqlite3.Error,
    ) as exc:
        print(f"dc: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
