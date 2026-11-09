"""The local dc command for ingestion, inspection, and basic lexical lookup."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import uuid
import warnings
from collections.abc import Mapping, Sequence
from dataclasses import asdict, replace
from datetime import UTC, date, datetime, time
from pathlib import Path
from xml.sax.saxutils import escape

from doclang import DocLangXDocument
from tabulate import tabulate  # type: ignore[import-untyped]
from tqdm import tqdm  # type: ignore[import-untyped]

from .agent_bridge import AGENTS, inspect_agent, install_agent, remove_agent
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
from .integration import IntegrationSpec
from .local import LocalContextStore
from .models import Principal
from .package import MAX_PACKAGE_BYTES, bounded_nodes, load_package
from .profiles import (
    DESCRIPTOR_NAMES,
    DESCRIPTOR_PARTS,
    MAX_ATTACHMENT_BYTES,
    MEMORY_KINDS,
    MEMORY_STATUSES,
    profile_kind,
    validate_descriptor,
    validate_memory,
)
from .project_context import ProjectContextWorker
from .retrieval import RetrievalHit, Retriever, SearchScope
from .sessions import SessionInfo, SessionStore
from .source_urls import download_source, is_source_url
from .uri import authorize_uri, parse_uri
from .vectors import TurbovecIndex, verified_vector_state


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
        raise ValueError(
            "session ID required; pass --id (or --session) or set DOCLING_CONTEXT_SESSION"
        )
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
    if args.stdin:
        if value is not None:
            raise ValueError(f"use either --{name} or --stdin")
        value = sys.stdin.read(20_001)
    if value is None:
        raise ValueError(f"--{name} or --stdin is required")
    return value


def _advance_progress(bar: tqdm, completed: int, total: int) -> None:
    if bar.total != total:
        bar.total = total
        bar.refresh()
    bar.update(completed - bar.n)


def _source_page_units(source: bytes | Path, filename: str | None) -> int:
    name = source.name if isinstance(source, Path) else filename
    if not name or not name.lower().endswith(".pdf"):
        return 1
    try:
        from pypdfium2 import PdfDocument  # type: ignore[import-untyped]

        pdf = PdfDocument(source)
        try:
            return len(pdf)
        finally:
            pdf.close()
    except ImportError as exc:
        raise RuntimeError(
            "install docling-context[conversion] to count PDF pages"
        ) from exc
    except Exception as exc:
        raise ValueError(f"cannot count pages in {name}: {exc}") from exc


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
) -> int:
    from_url = is_source_url(args.source)
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

    paths: list[Path] = []
    base: Path | None = None
    if from_url:
        if args.recursive:
            raise ValueError("--recursive requires a folder source")
    else:
        source = Path(args.source).resolve(strict=True)
        if source.is_dir():
            candidates = source.rglob("*") if args.recursive else source.iterdir()
            paths = sorted(
                path for path in candidates if path.is_file() and selected(path)
            )
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
        allowed_roots=(base,) if base is not None else (),
        converter=LocalDoclingConverter(_config(args)),
    )
    with Catalog(store) as catalog:
        if args.project:
            catalog.get_project(principal, args.project)
        sources: list[tuple[bytes | Path, str | None, str | None]]
        if from_url:
            data, fetched_filename = download_source(args.source)
            if not selected(Path(fetched_filename)):
                raise ValueError(
                    "source URL does not identify a supported selected format"
                )
            sources = [(data, fetched_filename, args.source)]
        else:
            sources = [(path, None, None) for path in paths]
        # A single unreadable or unconvertible file must not abort a folder
        # import: record it, keep going, and report every failure at the end.
        keep_going = len(sources) > 1 and not args.fail_fast
        failures: list[dict[str, str]] = []

        def source_label(item: bytes | Path, filename: str | None) -> str:
            if isinstance(item, Path):
                return str(item.relative_to(base)) if base is not None else str(item)
            return filename or "<download>"

        def record_failure(label: str, exc: Exception) -> None:
            message = str(exc) or type(exc).__name__
            failures.append({"source": label, "error": message})
            tqdm.write(f"dc: skipped {label}: {message}", file=sys.stderr)

        checked: list[tuple[bytes | Path, str | None, str | None]] = []
        source_digests: list[str] = []
        existing_uris: list[str | None] = []
        page_units: list[int] = []
        progress_enabled = not args.json and sys.stderr.isatty()
        if progress_enabled:
            print("Counting pages and checking library...", file=sys.stderr)
        for item, filename, origin in sources:
            try:
                if isinstance(item, Path):
                    if item.stat().st_size > MAX_PACKAGE_BYTES:
                        raise ValueError(f"source file is too large: {item}")
                    with item.open("rb") as source_file:
                        digest = hashlib.file_digest(source_file, "sha256").hexdigest()
                else:
                    digest = hashlib.sha256(item).hexdigest()
                existing = catalog.find_source(principal, digest)
                units = _source_page_units(item, filename)
            except (OSError, ValueError) as exc:
                if not keep_going:
                    raise
                record_failure(source_label(item, filename), exc)
                continue
            checked.append((item, filename, origin))
            source_digests.append(digest)
            existing_uris.append(existing)
            page_units.append(units)
        sources = checked

        already_in_library = sum(uri is not None for uri in existing_uris)
        total_page_units = sum(page_units)
        imported: list[dict[str, object]] = []
        newly_added: set[str] = set()
        with tqdm(
            total=total_page_units,
            initial=0,
            desc="Importing pages",
            unit="page",
            file=sys.stderr,
            disable=not progress_enabled,
        ) as bar:
            bar.set_postfix_str(f"existing={already_in_library} docs, added=0 docs")
            stop_refresh = threading.Event()

            def refresh_progress() -> None:
                while not stop_refresh.wait(1):
                    bar.refresh()

            refresh_thread = threading.Thread(target=refresh_progress, daemon=True)
            if progress_enabled:
                refresh_thread.start()
            try:
                for (item, filename, origin_uri), digest, existing_uri, units in zip(
                    sources, source_digests, existing_uris, page_units, strict=True
                ):
                    bar.set_postfix_str(
                        f"existing={already_in_library} docs, "
                        f"added={len(newly_added)} docs, current={units} pages"
                    )
                    try:
                        with store.transaction():
                            uri = (
                                catalog.find_source(principal, digest)
                                or new_document_uri()
                            )
                            record = ingestor.add_resource(
                                principal,
                                uri,
                                item,
                                filename=filename,
                                origin_uri=origin_uri,
                                force=args.force,
                            )
                            catalog.register_resource(principal, record)
                            if args.project:
                                catalog.link(principal, args.project, record.uri)
                    except Exception as exc:
                        if not keep_going:
                            raise
                        record_failure(source_label(item, filename), exc)
                        bar.update(units)
                        continue
                    imported.append(
                        {
                            "uri": record.uri,
                            "revision_id": record.revision_id,
                            "source_sha256": digest,
                            "project_id": args.project,
                        }
                    )
                    if existing_uri is None:
                        newly_added.add(digest)
                    bar.update(units)
                    bar.set_postfix_str(
                        f"existing={already_in_library} docs, "
                        f"added={len(newly_added)} docs"
                    )
            except KeyboardInterrupt:
                print(
                    f"dc: interrupted after processing {bar.n} of {total_page_units} pages; "
                    f"{len(newly_added)} new documents added. Rerun the command to continue.",
                    file=sys.stderr,
                )
                raise
            finally:
                stop_refresh.set()
                if progress_enabled:
                    refresh_thread.join()
        _rows_or_json_lines(
            imported,
            headers=("uri", "revision_id", "source_sha256", "project_id"),
            json_output=args.json,
        )
        if failures:
            if args.json:
                for failure in failures:
                    _json({"status": "failed", **failure})
            print(
                f"dc: {len(failures)} of {len(failures) + len(imported)} documents "
                "failed and were skipped; fix them and rerun the command "
                "(already imported documents are not converted again):",
                file=sys.stderr,
            )
            for failure in failures:
                print(f"  {failure['source']}: {failure['error']}", file=sys.stderr)
            return 1
        return 0


def _scan(
    store: LocalContextStore,
    principal: Principal,
    term: str,
    prefix: str,
    *,
    document_limit: int = 1_000,
    hit_limit: int = 100,
    project_id: str | None = None,
) -> list[dict[str, object]]:
    address = parse_uri(prefix, prefix=True)
    authorize_uri(principal, address)
    base = address.value
    descendant = base + "/"
    if project_id:
        Catalog(store).get_project(principal, project_id)
    rows = store.db.execute(
        """SELECT uri FROM documents WHERE tenant_id=? AND available=1
           AND (uri=? OR substr(uri,1,?)=?)
           AND (? IS NULL OR uri IN (SELECT r.uri FROM dc_project_resource_links l
             JOIN dc_resources r USING (tenant_id,resource_type,resource_id)
             WHERE l.tenant_id=? AND l.project_id=?)) ORDER BY uri LIMIT ?""",
        (
            principal.tenant_id,
            base,
            len(descendant),
            descendant,
            project_id,
            principal.tenant_id,
            project_id,
            document_limit,
        ),
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
    commands = parser.add_subparsers(dest="command")
    init = commands.add_parser("init", help="Initialize the local SQLite store")
    init.add_argument(
        "-j", "--json", action="store_true", help="Print JSON instead of a table"
    )
    overview = commands.add_parser(
        "overview", help="List projects and linked resource counts"
    )
    overview.add_argument("-j", "--json", action="store_true")
    overview.add_argument("--limit", type=int, default=100)
    overview.add_argument("--offset", type=int, default=0)
    project = commands.add_parser(
        "project", help="Create and inspect projects and links"
    )
    project_commands = project.add_subparsers(dest="project_command", required=True)
    project_create = project_commands.add_parser("create", help="Create a project")
    project_create.add_argument(
        "-p", "--project", dest="project_id", required=True, help="Project ID"
    )
    project_create.add_argument("--title", required=True)
    project_show = project_commands.add_parser("show", help="Show project details")
    project_show.add_argument(
        "-p", "--project", dest="project_id", required=True, help="Project ID"
    )
    for name in ("link", "unlink"):
        command = project_commands.add_parser(
            name, help=f"{'Link' if name == 'link' else 'Unlink'} a shared resource"
        )
        command.add_argument(
            "-p", "--project", dest="project_id", required=True, help="Project ID"
        )
        command.add_argument("-u", "--uri", required=True, help="Resource URI")
    project_list = project_commands.add_parser(
        "resources", help="List linked resources"
    )
    project_list.add_argument(
        "-p", "--project", dest="project_id", required=True, help="Project ID"
    )
    context = project_commands.add_parser(
        "context", help="Read and generate project context"
    )
    context_commands = context.add_subparsers(dest="context_command", required=True)
    for name, help_text in (
        ("refresh", "Queue context regeneration"),
        ("show", "Read project context"),
        ("jobs", "Inspect context jobs"),
        ("worker", "Process a queued context job"),
    ):
        sub = context_commands.add_parser(name, help=help_text)
        sub.set_defaults(project_command="context-show" if name == "show" else name)
        if name != "worker":
            sub.add_argument(
                "-p", "--project", dest="project_id", required=True, help="Project ID"
            )
        sub.add_argument("-j", "--json", action="store_true")
    context_set = context_commands.add_parser(
        "set", help="Set a project context document"
    )
    context_set.add_argument(
        "-p", "--project", dest="project_id", required=True, help="Project ID"
    )
    context_set.add_argument("--description", type=Path)
    context_set.add_argument("--abstract", type=Path)
    context_set.add_argument("--overview", type=Path)
    context_set.add_argument("-j", "--json", action="store_true")
    context_set.set_defaults(project_command="context-set")
    for command in project_commands.choices.values():
        command.add_argument(
            "-j", "--json", action="store_true", help="Print JSON instead of a table"
        )
    library = commands.add_parser(
        "library", help="Import, inspect, and search source documents"
    )
    library_commands = library.add_subparsers(dest="library_command", required=True)
    for group, kind in (("skill", "skills"), ("concept", "concepts")):
        typed = commands.add_parser(group, help=f"Create and inspect shared {kind}")
        typed_commands = typed.add_subparsers(dest="typed_command", required=True)
        create = typed_commands.add_parser(
            "create", help=f"Create a named {group} from DCLG or DCLX"
        )
        create.add_argument(
            "-i", "--id", dest="resource_id", required=True, help=f"{group.title()} ID"
        )
        create.add_argument("--from", dest="source", type=Path, required=True)
        create.add_argument(
            "--metadata", type=Path, help="Descriptor JSON; required for DCLG concepts"
        )
        create.add_argument("-p", "--project")
        create.set_defaults(command="typed-create", type=kind)
        for name in ("list", "show", "projects", "links", "link"):
            action_help = {
                "list": f"List shared {kind}",
                "show": f"Show one {group}",
                "projects": f"List projects using a {group}",
                "links": f"List a {group}'s resource links",
                "link": f"Link a {group} to another resource",
            }
            sub = typed_commands.add_parser(name, help=action_help[name])
            sub.set_defaults(command="typed-inspect", type=kind)
            if name == "list":
                sub.add_argument("-p", "--project")
            else:
                sub.add_argument(
                    "-i",
                    "--id",
                    dest="resource_id",
                    required=True,
                    help=f"{group.title()} ID",
                )
            if name == "link":
                sub.add_argument(
                    "--target", dest="target_uri", required=True, help="Target URI"
                )
                sub.add_argument("--relation", required=True)
        for sub in typed_commands.choices.values():
            sub.add_argument("-j", "--json", action="store_true")
    knowledge = commands.add_parser(
        "knowledge", help="Manage datasets of cited facts defined by concepts"
    )
    knowledge_commands = knowledge.add_subparsers(
        dest="knowledge_command", required=True
    )
    knowledge_create = knowledge_commands.add_parser(
        "create", help="Create a dataset from a concept revision"
    )
    knowledge_create.add_argument(
        "-i", "--id", dest="resource_id", required=True, help="Dataset ID"
    )
    knowledge_create.add_argument("--concept", required=True)
    knowledge_create.add_argument("-p", "--project")
    for name, help_text in (
        ("list", "List knowledge datasets"),
        ("show", "Show a dataset's pinned concept and schema"),
    ):
        sub = knowledge_commands.add_parser(name, help=help_text)
        if name == "list":
            sub.add_argument("-p", "--project")
        else:
            sub.add_argument(
                "-u", "--uri", dest="knowledge_uri", required=True, help="Dataset URI"
            )
    knowledge_add = knowledge_commands.add_parser(
        "add-fact", help="Add a schema-checked fact with an exact citation"
    )
    knowledge_add.add_argument(
        "-u", "--uri", dest="knowledge_uri", required=True, help="Dataset URI"
    )
    for field in ("entity-type", "entity-id", "property", "value"):
        knowledge_add.add_argument(f"--{field}", required=True)
    for field in ("source-uri", "source-revision", "source-xpath"):
        knowledge_add.add_argument(f"--{field}", required=True)
    knowledge_facts = knowledge_commands.add_parser(
        "facts", help="List facts in a knowledge dataset"
    )
    knowledge_facts.add_argument(
        "-u", "--uri", dest="knowledge_uri", required=True, help="Dataset URI"
    )
    knowledge_facts.add_argument("--entity")
    knowledge_facts.add_argument("--property")
    knowledge_add.add_argument("--unit")
    for command in knowledge_commands.choices.values():
        command.add_argument(
            "-j", "--json", action="store_true", help="Print JSON instead of a table"
        )
    index = commands.add_parser(
        "index", help="Inspect or rebuild lexical and vector indexes"
    )
    index_commands = index.add_subparsers(dest="index_command", required=True)
    index_status = index_commands.add_parser(
        "status", help="Show lexical and vector index status"
    )
    index_status.add_argument(
        "-j", "--json", action="store_true", help="Print JSON instead of a table."
    )
    lex = index_commands.add_parser(
        "lex", help="Inspect or rebuild indexed document nodes"
    )
    lex_commands = lex.add_subparsers(dest="index_operation", required=True)
    lex_status = lex_commands.add_parser(
        "status", help="Show indexed document and node counts"
    )
    lex_status.add_argument(
        "-j", "--json", action="store_true", help="Print JSON instead of a table."
    )
    lex_rebuild = lex_commands.add_parser(
        "rebuild", help="Rebuild search records from stored DCLX documents"
    )
    lex_rebuild.add_argument(
        "-j", "--json", action="store_true", help="Print JSON instead of a table"
    )
    vector = index_commands.add_parser(
        "vector", help="Inspect or rebuild vector embeddings"
    )
    vector_commands = vector.add_subparsers(dest="index_operation", required=True)
    vector_status = vector_commands.add_parser(
        "status", help="Show vector model and coverage"
    )
    vector_status.add_argument(
        "-j", "--json", action="store_true", help="Print JSON instead of a table."
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
        "-j", "--json", action="store_true", help="Print JSON instead of a table"
    )
    add = library_commands.add_parser(
        "add",
        help="Import one supported document or a folder of documents",
        description="Import a supported local document, folder, or HTTP(S) document URL.",
        epilog="Defaults shown apply without --config. Explicit flags override settings in the config file.",
    )
    add.set_defaults(command="add-resource")
    add.add_argument(
        "--source",
        required=True,
        help="A local document file, folder, or HTTP(S) URL; folders scan their top level by default.",
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
    add.add_argument("-p", "--project", help="Link imported documents to this project")
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
        "--fail-fast",
        action="store_true",
        help="Stop a folder import at the first failing document "
        "(default: off; failures are skipped and reported at the end).",
    )
    add.add_argument(
        "-j", "--json", action="store_true", help="Print JSON lines instead of a table"
    )
    jobs = library_commands.add_parser("jobs", help="Inspect durable ingestion jobs")
    jobs.set_defaults(command="jobs")
    jobs_commands = jobs.add_subparsers(dest="jobs_command", required=True)
    jobs_status = jobs_commands.add_parser("status", help="Show one job by ID")
    jobs_status.add_argument("-i", "--id", dest="job_id", required=True, help="Job ID")
    jobs_status.add_argument("-j", "--json", action="store_true")
    listing = library_commands.add_parser("list", help="List library documents")
    listing.set_defaults(command="library-list")
    listing.add_argument("-p", "--project")
    for name, help_text in (
        ("projects", "List projects linked to a document"),
        ("links", "List a document's resource links"),
        ("link", "Link a document to another resource"),
    ):
        sub = library_commands.add_parser(name, help=help_text)
        sub.set_defaults(command="library-relations")
        sub.add_argument(
            "-d",
            "--document",
            dest="document_id",
            required=True,
            help="Document ID or URI",
        )
        if name == "link":
            sub.add_argument(
                "--target", dest="target_uri", required=True, help="Target URI"
            )
            sub.add_argument("--relation", required=True)
        sub.add_argument("-j", "--json", action="store_true")
    listing.add_argument(
        "-j", "--json", action="store_true", help="Print JSON lines instead of a table"
    )
    tree = library_commands.add_parser("tree", help="Walk a context subtree")
    tree.set_defaults(command="tree")
    tree.add_argument(
        "-u",
        "--uri",
        default="docling://resources/library/",
        help="Subtree URI (default: docling://resources/library/).",
    )
    tree.add_argument("-L", "--depth", type=int, default=2)
    tree_output = tree.add_mutually_exclusive_group()
    tree_output.add_argument(
        "-j", "--json", action="store_true", help="Print JSON lines instead of a table"
    )
    tree_output.add_argument(
        "--raw", action="store_true", help="Print an indented URI tree"
    )
    outline = library_commands.add_parser("outline", help="Read a document TOC sidecar")
    outline.set_defaults(command="outline")
    outline.add_argument("-u", "--uri", required=True, help="Document URI or ID")
    outline.add_argument("--revision", help="Immutable document revision")
    outline_output = outline.add_mutually_exclusive_group()
    outline_output.add_argument("-j", "--json", action="store_true")
    outline_output.add_argument("--raw", action="store_true", help="Print TOC XML only")
    show = library_commands.add_parser("show", help="Read one cited DocLang node")
    show.set_defaults(command="show")
    show.add_argument("-u", "--uri", required=True, help="Document URI or ID")
    show.add_argument("-x", "--xpath", required=True, help="DocLang node XPath")
    show.add_argument("--revision", help="Immutable document revision")
    show.add_argument("--format", choices=("text", "xml"), default="text")
    show.add_argument("--max-chars", type=int, default=8_192)
    show_output = show.add_mutually_exclusive_group()
    show_output.add_argument("-j", "--json", action="store_true")
    show_output.add_argument(
        "--raw", action="store_true", help="Print node content only"
    )
    for name in ("find", "grep"):
        search = library_commands.add_parser(name, help="Bounded lexical lookup")
        search.set_defaults(command=name)
        search.add_argument("-p", "--project")
        search.add_argument("--term", required=True, help="Literal search term")
        search.add_argument("-u", "--uri", default="docling://resources/")
        search.add_argument(
            "-j",
            "--json",
            action="store_true",
            help="Print JSON lines instead of a table",
        )
    retrieval = library_commands.add_parser(
        "search", help="Search indexed DocLang nodes with exact citations"
    )
    retrieval.set_defaults(command="search")
    retrieval.add_argument(
        "-q", "--query", default="", help="Query text (optional with --image)."
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
        "-u",
        "--uri",
        default="docling://resources/library/",
        help="Authorized URI subtree (default: shared library).",
    )
    retrieval.add_argument(
        "-p", "--project", help="Search only resources linked to this project"
    )
    retrieval.add_argument(
        "-d", "--document", help="Restrict to one document URI (default: all in scope)."
    )
    retrieval.add_argument(
        "-x",
        "--xpath",
        help="Restrict to one source XPath subtree (default: all nodes).",
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
        "-j",
        "--json",
        action="store_true",
        help="Print complete JSON records instead of the compact table (default: table).",
    )

    session = commands.add_parser("session", help="Record and inspect agent sessions")
    session_commands = session.add_subparsers(dest="session_command", required=True)
    session_start = session_commands.add_parser(
        "start", help="Launch an agent in a project session"
    )
    session_start.add_argument(
        "-i", "--id", help="Session ID (default: generated UUID)"
    )
    session_start.add_argument(
        "-p", "--project", required=True, help="Owning project ID"
    )
    session_start.add_argument("-a", "--agent", choices=AGENTS, required=True)
    session_start.add_argument(
        "--no-launch",
        action="store_true",
        help="Record a session without launching an agent",
    )
    session_list = session_commands.add_parser(
        "list", help="List user sessions or sessions in a project"
    )
    session_list.add_argument(
        "-p", "--project", help="List sessions owned by this project"
    )
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
        command.add_argument(
            "-i",
            "--id",
            dest="session_id",
            required=True,
            help="Session ID or full URI",
        )
        command.add_argument(
            "-j", "--json", action="store_true", help="Print JSON instead of a table"
        )
    session_append = session_commands.add_parser(
        "append", help="Append one session event"
    )
    session_append.add_argument(
        "-i",
        "--id",
        dest="session_id",
        help="Session ID or full URI (default: DOCLING_CONTEXT_SESSION)",
    )
    session_append.add_argument("--text", help="Event text, or use --stdin")
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
    session_trim.add_argument(
        "-i", "--id", dest="session_id", required=True, help="Session ID or full URI"
    )
    session_trim.add_argument("--keep-last", type=int, required=True)
    session_attachment = session_commands.add_parser(
        "attachment", help="Save one event attachment"
    )
    session_attachment.add_argument(
        "-i", "--id", dest="session_id", required=True, help="Session ID or full URI"
    )
    session_attachment.add_argument(
        "--key", required=True, help="Idempotency key of the attached event"
    )
    session_attachment.add_argument("--output", type=Path, required=True)
    session_attachment.add_argument("-j", "--json", action="store_true")
    for command in (session_start, session_list, session_append, session_trim):
        command.add_argument("-j", "--json", action="store_true")

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
            "-j", "--json", action="store_true", help="Print JSON instead of a table"
        )
    for name in ("show", "accept", "reject", "delete", "purge"):
        command = memory_commands.add_parser(name, help=f"{name.title()} one memory")
        command.add_argument("-u", "--uri", required=True, help="Memory URI")
        command.add_argument("-j", "--json", action="store_true")
        if name in {"reject", "delete"}:
            command.add_argument("--reason", default="")
    memory_correct = memory_commands.add_parser(
        "correct", help="Write an accepted correction"
    )
    memory_correct.add_argument("-u", "--uri", required=True, help="Memory URI")
    memory_correct.add_argument("--claim", help="Corrected claim, or use --stdin")
    memory_correct.add_argument("--stdin", action="store_true")
    memory_correct.add_argument("--explanation")
    memory_correct.add_argument("-j", "--json", action="store_true")
    memory_create = memory_commands.add_parser(
        "create", help="Create a user-authored claim"
    )
    memory_create.add_argument(
        "--kind",
        default="fact",
        choices=sorted(MEMORY_KINDS),
        help="Memory kind (default: fact)",
    )
    memory_create.add_argument("--from", dest="source", type=Path)
    memory_create.add_argument("--claim", help="Claim text, or use --stdin or --from")
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
    memory_create.add_argument("-j", "--json", action="store_true")
    memory_search = memory_commands.add_parser("search", help="Search accepted claims")
    memory_search.add_argument("-q", "--query", default="", help="Query text")
    memory_search.add_argument("--limit", type=int, default=10)
    memory_search.add_argument("-j", "--json", action="store_true")
    memory_recall = memory_commands.add_parser(
        "recall", help="Recall claims once per session revision"
    )
    memory_recall.add_argument("-q", "--query", default="", help="Query text")
    memory_recall.add_argument("-s", "--session", help="ID or DOCLING_CONTEXT_SESSION")
    memory_recall.add_argument("--limit", type=int, default=10)
    memory_recall.add_argument("-j", "--json", action="store_true")
    memory_worker = memory_commands.add_parser(
        "worker", help="Process queued memory compilation jobs"
    )
    memory_worker.add_argument(
        "--once", action="store_true", help="Process one due job then exit"
    )
    memory_worker.add_argument("-j", "--json", action="store_true")
    memory_jobs = memory_commands.add_parser(
        "jobs", help="List compilation jobs for a session"
    )
    memory_jobs.add_argument(
        "-s",
        "--session",
        dest="session_id",
        required=True,
        help="Session ID or full URI",
    )
    memory_jobs.add_argument("--limit", type=int, default=100)
    memory_jobs.add_argument("-j", "--json", action="store_true")
    for sub in (memory_list, memory_create, memory_search, memory_recall):
        sub.add_argument("-p", "--project")
    agent = commands.add_parser(
        "agent", help="Install the dc skill and MCP tools in an agent"
    )
    agent_commands = agent.add_subparsers(dest="agent_command", required=True)
    for action, help_text in (
        ("install", "Install the managed skill and MCP connection"),
        ("status", "Check the managed skill and MCP connection"),
        ("remove", "Remove the managed skill and MCP connection"),
    ):
        sub = agent_commands.add_parser(action, help=help_text)
        sub.add_argument("-a", "--agent", choices=AGENTS, required=True)
        sub.add_argument("--scope", choices=("user", "project"), default="user")
        sub.add_argument("--project-root", type=Path, default=Path.cwd())
        sub.add_argument(
            "--home", type=Path, default=Path.home(), help=argparse.SUPPRESS
        )
        sub.add_argument("-j", "--json", action="store_true")
    return parser


def _source_document(source: Path) -> DocLangXDocument:
    if source.name.lower().endswith(".dclx"):
        return load_package(source.read_bytes())
    document = DocLangXDocument()
    if not document.read_xml(source.read_text()):
        raise ValueError(document.last_error() or "invalid DCLG document")
    from .doclang_authoring import validate_doclang

    validate_doclang(document, label="resource source")
    return document


def _typed_create(
    args: argparse.Namespace, store: LocalContextStore, principal: Principal
) -> None:
    catalog = Catalog(store)
    uri = catalog.resource_uri(args.type, args.resource_id)
    source = args.source.resolve(strict=True)
    document = _source_document(source)
    raw = document.get_part_text(DESCRIPTOR_PARTS[args.type])
    supplied = (
        json.loads(args.metadata.read_text())
        if args.metadata
        else (json.loads(raw) if raw else {})
    )
    if not isinstance(supplied, dict):
        raise TypeError("descriptor metadata must be a JSON object")
    singular = DESCRIPTOR_NAMES[args.type]
    metadata = {
        **supplied,
        "profile": f"{singular}-v1",
        f"{singular}_id": args.resource_id,
        "name": supplied.get("name", args.resource_id),
        "summary": supplied.get("summary", ""),
    }
    document.set_part_text(
        DESCRIPTOR_PARTS[args.type], json.dumps(metadata), "application/json"
    )
    validate_descriptor(uri, document)
    if args.project:
        catalog.get_project(principal, args.project)
    with store.transaction():
        record = Ingestor(store).add_resource(
            principal,
            uri,
            document.write_bytes(),
            filename="resource.dclx",
            profile_metadata=metadata,
        )
        catalog.register_resource(principal, record, args.type)
        if args.project:
            catalog.link(principal, args.project, uri)
    _status(
        {"uri": uri, "revision_id": record.revision_id, "project_id": args.project},
        json_output=args.json,
    )


def _knowledge_create(
    args: argparse.Namespace, store: LocalContextStore, principal: Principal
) -> None:
    catalog = Catalog(store)
    uri = catalog.resource_uri("knowledge", args.resource_id)
    concept_uri = catalog.resource_uri("concepts", args.concept)
    concept = store.get_record(principal, concept_uri)
    definition = validate_descriptor(
        concept_uri, store.get_document(principal, concept_uri)
    )
    metadata = {
        "profile": "knowledge-v1",
        "knowledge_id": args.resource_id,
        "name": args.resource_id,
        "summary": definition["summary"],
        "concept_uri": concept_uri,
        "concept_revision": concept.revision_id,
        "fields": {
            key: value["type"]
            for key, value in definition["schema"]["properties"].items()
        },
    }
    if args.project:
        catalog.get_project(principal, args.project)
    data = f"<doclang><text>{escape(args.resource_id)}: dataset defined by {escape(concept_uri)} at revision {concept.revision_id}</text></doclang>".encode()
    with store.transaction():
        record = Ingestor(store).add_resource(
            principal, uri, data, filename="dataset.dclg", profile_metadata=metadata
        )
        catalog.register_resource(principal, record, "knowledge")
        catalog.register_knowledge_dataset(
            principal, uri, concept_uri, concept.revision_id
        )
        if args.project:
            catalog.link(principal, args.project, uri)
            catalog.link(principal, args.project, concept_uri)
    _status(
        {
            "uri": uri,
            "revision_id": record.revision_id,
            "concept_uri": concept_uri,
            "concept_revision": concept.revision_id,
        },
        json_output=args.json,
    )


def _session_command(
    args: argparse.Namespace, store: LocalContextStore, principal: Principal
) -> None:
    sessions = SessionStore(store)
    command = args.session_command
    if getattr(args, "session_id", None) and not args.session_id.startswith(
        "docling://"
    ):
        rows = store.db.execute(
            "SELECT s.project_id,s.session_id FROM dc_project_sessions s JOIN documents d ON d.tenant_id=s.tenant_id AND d.uri='docling://projects/'||s.project_id||'/sessions/'||s.session_id WHERE s.tenant_id=? AND s.owner_id=? AND s.session_id=? AND d.available=1",
            (principal.tenant_id, principal.user_id, args.session_id),
        ).fetchall()
        if len(rows) > 1:
            raise ValueError("session ID is ambiguous; use its full URI")
        if rows:
            args.session_id = (
                f"docling://projects/{rows[0]['project_id']}/sessions/{args.session_id}"
            )
    if command == "list":
        since = _timestamp_filter(args.since)
        until = _timestamp_filter(args.until, upper=True)
        if since and until and since > until:
            raise ValueError("--since is later than --until")
        if args.project:
            if not 1 <= args.limit <= 1000 or args.offset < 0:
                raise ValueError("invalid session list filter or page")
            rows = Catalog(store).project_sessions(principal, args.project)
            if args.status:
                rows = [row for row in rows if row["status"] == args.status]
            if since:
                rows = [
                    row
                    for row in rows
                    if datetime.fromisoformat(row["created_at"])
                    .replace(tzinfo=UTC)
                    .isoformat()
                    >= since
                ]
            if until:
                rows = [
                    row
                    for row in rows
                    if datetime.fromisoformat(row["created_at"])
                    .replace(tzinfo=UTC)
                    .isoformat()
                    <= until
                ]
            _table_or_json(
                rows[args.offset : args.offset + args.limit],
                headers=("session_id", "agent", "status", "owner_id", "created_at"),
                json_output=args.json,
            )
            return
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
        address = parse_uri(info.uri)
        if address.namespace == "projects":
            values["project_id"] = address.segments[0]
            matching = [
                row
                for row in Catalog(store).project_sessions(
                    principal, address.segments[0]
                )
                if row["session_id"] == address.segments[2]
            ]
            values["agent"] = matching[0]["agent"] if matching else None
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


def _agent_spec(args: argparse.Namespace, principal: Principal) -> IntegrationSpec:
    return IntegrationSpec(
        harness=args.agent,
        scope=args.scope,
        project_root=args.project_root.resolve(),
        home=args.home.expanduser().resolve(),
        store=args.store.expanduser().resolve(),
        tenant=principal.tenant_id,
        user=principal.user_id,
        allow_writes=True,
    )


def _agent_command(agent: str, executable: str, prompt: str) -> list[str]:
    if agent == "hermes":
        return [executable, "chat", "-q", prompt]
    if agent in {"claude", "pi"}:
        return [executable, "--append-system-prompt", prompt]
    return [executable, prompt]


def _start_agent_session(args: argparse.Namespace, principal: Principal) -> int:
    session_id = args.id or uuid.uuid4().hex
    uri = f"docling://projects/{args.project}/sessions/{session_id}"
    with LocalContextStore(args.store) as store:
        catalog = Catalog(store)
        catalog.get_project(principal, args.project)
        existing = [
            row
            for row in catalog.project_sessions(principal, args.project)
            if row["session_id"] == session_id
        ]
        if existing and existing[0]["agent"] not in (None, args.agent):
            raise ValueError("session ID already belongs to another agent")
        if existing and existing[0]["status"] == "closed":
            raise ValueError("session ID already closed")
    executable = None if args.no_launch else shutil.which(args.agent)
    if not args.no_launch and executable is None:
        raise ValueError(f"{args.agent} is not installed or is not on PATH")
    if not args.no_launch:
        spec = IntegrationSpec(
            harness=args.agent,
            scope="user",
            project_root=Path.cwd(),
            home=Path.home(),
            store=args.store.expanduser().resolve(),
            tenant=principal.tenant_id,
            user=principal.user_id,
            allow_writes=True,
        )
        install_agent(spec)
    with LocalContextStore(args.store) as store:
        sessions = SessionStore(store)
        info = sessions.start(principal, uri)
        Catalog(store).register_session(
            principal,
            args.project,
            session_id,
            agent=args.agent,
            revision_id=info.revision_id,
        )
    _status(
        {**asdict(info), "project_id": args.project, "agent": args.agent},
        json_output=args.json,
    )
    if args.no_launch:
        return 0
    assert executable is not None
    environment = os.environ.copy()
    environment.update(
        DOCLING_CONTEXT_STORE=str(args.store.expanduser().resolve()),
        DOCLING_CONTEXT_TENANT=principal.tenant_id,
        DOCLING_CONTEXT_USER=principal.user_id,
        DOCLING_CONTEXT_PROJECT=args.project,
        DOCLING_CONTEXT_SESSION=uri,
        DOCLING_CONTEXT_AGENT=args.agent,
    )
    environment["PATH"] = (
        str(Path(sys.executable).parent) + os.pathsep + environment.get("PATH", "")
    )
    prompt = (
        f"Work in Docling Context project {args.project}. Your session URI is {uri}. "
        "Use the docling-context MCP tools with this project ID for retrieval and exact citations. "
        "Record useful conclusions with session_append using the full session URI. "
        "The launcher will close the session when you exit."
    )
    command = _agent_command(args.agent, executable, prompt)
    try:
        code = subprocess.run(command, env=environment, check=False).returncode
        return 128 - code if code < 0 else code
    except KeyboardInterrupt:
        return 130
    finally:
        with LocalContextStore(args.store) as store:
            current = SessionStore(store).get(principal, uri)
            if current.status == "open":
                SessionStore(store).close(principal, uri)


def _memory_command(
    args: argparse.Namespace, store: LocalContextStore, principal: Principal
) -> None:
    memories = MemoryService(store)
    command = args.memory_command
    if getattr(args, "uri", None):
        args.uri = Catalog.resource_uri("memories", args.uri)
    project_id = getattr(args, "project", None)
    if project_id:
        Catalog(store).get_project(principal, project_id)
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
            project_id=project_id,
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
        if args.source:
            if args.claim or args.stdin:
                raise ValueError("use --from or a claim, not both")
            document = _source_document(args.source.resolve(strict=True))
            text_nodes = [
                str(node["text"])
                for node in bounded_nodes(document, limit=1000, text_bytes=20000)
                if node["name"] == "text" and node["text"]
            ]
            if not text_nodes:
                raise ValueError("memory document needs a text node for its claim")
            claim = text_nodes[0]
        else:
            document = None
            claim = _input_text(args, name="claim")
        record = memories.create(
            principal,
            args.kind,
            claim,
            explanation=args.explanation,
            subject=args.subject,
            confidence=args.confidence,
            citations=citations,
            accepted=args.accepted,
            source_document=document,
        )
        if project_id:
            Catalog(store).link(principal, project_id, record.uri)
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
            memories.search(
                principal, args.query, limit=args.limit, project_id=project_id
            )
            if command == "search"
            else memories.recall(
                principal,
                _session_id(args.session),
                args.query,
                limit=args.limit,
                project_id=project_id,
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


def _store_counts(store: LocalContextStore, principal: Principal) -> dict[str, object]:
    counts = {
        table: store.db.execute(
            f"SELECT COUNT(*) FROM {table} WHERE tenant_id=?",
            (principal.tenant_id,),
        ).fetchone()[0]
        for table in ("documents", "revisions", "jobs")
    }
    user_prefix = f"docling://users/{principal.tenant_id}/{principal.user_id}/"
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
    for label, table in (
        ("pending_ingestion_jobs", "jobs"),
        ("pending_project_jobs", "dc_project_context_jobs"),
        ("pending_memory_jobs", "memory_compile_jobs"),
    ):
        user_filter = " AND user_id=?" if label == "pending_memory_jobs" else ""
        counts[label] = store.db.execute(
            f"SELECT COUNT(*) FROM {table} WHERE tenant_id=? AND status IN ('queued','running'){user_filter}",
            (principal.tenant_id, principal.user_id)
            if user_filter
            else (principal.tenant_id,),
        ).fetchone()[0]
    return {"store": str(store.root.resolve()), **counts}


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    principal = Principal(args.tenant, args.user)
    bare = args.command is None
    if bare:
        args.command, args.json, args.limit, args.offset = "overview", False, 100, 0
    try:
        if args.command == "agent":
            spec = _agent_spec(args, principal)
            agent_result = (
                install_agent(spec)
                if args.agent_command == "install"
                else inspect_agent(spec)
                if args.agent_command == "status"
                else remove_agent(spec)
            )
            _status(agent_result, json_output=args.json)
            return 0 if agent_result["status"] != "incomplete" else 1
        if args.command == "session" and args.session_command == "start":
            return _start_agent_session(args, principal)
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
                counts = _store_counts(store, principal)
            if args.json:
                _json({"projects": rows, "counts": counts})
            else:
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
                    json_output=False,
                )
                _status(counts, json_output=False)
                if bare:
                    print(
                        '\nCreate a project: dc project create --project materials --title "Materials"\nAdd documents:    dc library add --source ./papers --project materials\nStart an agent:   dc session start --project materials --agent codex'
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
                            catalog.project_summary(principal, args.project_id),
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
                    elif args.project_command == "context-show":
                        project_info = catalog.get_project(principal, args.project_id)
                        _status(
                            {
                                kind: store.get_document(principal, uri).xml()
                                if uri
                                else None
                                for kind, uri in (
                                    ("description", project_info.description_uri),
                                    ("abstract", project_info.abstract_uri),
                                    ("overview", project_info.overview_uri),
                                )
                            },
                            json_output=args.json,
                        )
                    elif args.project_command == "context-set":
                        supplied = [
                            (kind, getattr(args, kind))
                            for kind in ("description", "abstract", "overview")
                            if getattr(args, kind)
                        ]
                        if len(supplied) != 1:
                            raise ValueError(
                                "provide exactly one of --description, --abstract, --overview"
                            )
                        kind, args.source = supplied[0]
                        args.project_command = f"set-{kind}"
                        catalog.get_project(principal, args.project_id)
                        source = args.source.resolve(strict=True)
                        uri = f"docling://projects/{args.project_id}/.{kind}.dclx"
                        record = Ingestor(
                            store, allowed_roots=(source.parent,)
                        ).add_resource(principal, uri, source)
                        catalog.set_project_document(
                            principal, args.project_id, kind, record
                        )
                        _status(
                            {"uri": uri, "revision_id": record.revision_id},
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
            elif args.command == "typed-create":
                _typed_create(args, store, principal)
            elif args.command == "typed-inspect":
                catalog = Catalog(store)
                if args.typed_command == "list":
                    _table_or_json(
                        catalog.resources(principal, args.type, args.project),
                        headers=("resource_id", "uri"),
                        json_output=args.json,
                    )
                else:
                    uri = catalog.resource_uri(args.type, args.resource_id)
                    store.get_record(principal, uri)
                    if args.typed_command == "show":
                        _status(
                            validate_descriptor(
                                uri, store.get_document(principal, uri)
                            ),
                            json_output=args.json,
                        )
                    elif args.typed_command == "projects":
                        _table_or_json(
                            catalog.resource_projects(principal, uri),
                            headers=("project_id", "title"),
                            json_output=args.json,
                        )
                    elif args.typed_command == "links":
                        _table_or_json(
                            catalog.resource_links(principal, uri),
                            headers=("source_uri", "target_uri", "relation"),
                            json_output=args.json,
                        )
                    else:
                        catalog.link_resources(
                            principal, uri, args.target_uri, args.relation
                        )
                        _status(
                            {
                                "source_uri": uri,
                                "target_uri": args.target_uri,
                                "relation": args.relation,
                            },
                            json_output=args.json,
                        )
            elif args.command == "knowledge":
                catalog = Catalog(store)
                if args.knowledge_command == "create":
                    _knowledge_create(args, store, principal)
                    return 0
                if args.knowledge_command == "list":
                    _table_or_json(
                        catalog.resources(principal, "knowledge", args.project),
                        headers=("resource_id", "uri"),
                        json_output=args.json,
                    )
                    return 0
                args.knowledge_uri = catalog.resource_uri(
                    "knowledge", args.knowledge_uri
                )
                if args.knowledge_command == "show":
                    _status(
                        catalog.knowledge_dataset(principal, args.knowledge_uri),
                        json_output=args.json,
                    )
                    return 0
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
                        unit=args.unit,
                    )
                    _status({"fact_id": fact_id}, json_output=args.json)
                else:
                    _table_or_json(
                        [
                            fact
                            for fact in catalog.knowledge_facts(
                                principal, args.knowledge_uri
                            )
                            if (not args.entity or fact["entity_id"] == args.entity)
                            and (not args.property or fact["property"] == args.property)
                        ],
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
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    previous_logging_disable = logging.root.manager.disable
                    logging.disable(logging.WARNING)
                    try:
                        if _add_resource(args, store, principal):
                            return 1
                    finally:
                        logging.disable(previous_logging_disable)
            elif args.command == "session":
                _session_command(args, store, principal)
            elif args.command == "memory":
                _memory_command(args, store, principal)
            elif args.command == "library-list":
                _table_or_json(
                    Catalog(store).resources(principal, "library", args.project),
                    headers=("resource_id", "uri", "revision_id"),
                    json_output=args.json,
                )
            elif args.command == "library-relations":
                catalog = Catalog(store)
                uri = catalog.resource_uri("library", args.document_id)
                store.get_record(principal, uri)
                if args.library_command == "projects":
                    _table_or_json(
                        catalog.resource_projects(principal, uri),
                        headers=("project_id", "title"),
                        json_output=args.json,
                    )
                elif args.library_command == "links":
                    _table_or_json(
                        catalog.resource_links(principal, uri),
                        headers=("source_uri", "target_uri", "relation"),
                        json_output=args.json,
                    )
                else:
                    catalog.link_resources(
                        principal, uri, args.target_uri, args.relation
                    )
                    _status(
                        {
                            "source_uri": uri,
                            "target_uri": args.target_uri,
                            "relation": args.relation,
                        },
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
                if not args.uri.startswith("docling://"):
                    args.uri = Catalog.resource_uri("library", args.uri)
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
                hits = _scan(
                    store,
                    principal,
                    args.term,
                    _prefix(args.uri),
                    project_id=args.project,
                )
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
                    state = verified_vector_state(store)
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
                        document_uri=Catalog.resource_uri("library", args.document)
                        if args.document
                        else None,
                        tiers=tuple(args.tier) if args.tier else (0, 1, 2),
                        project_id=args.project,
                    ),
                    k=args.k,
                    mode=mode,
                    max_tokens=args.max_tokens,
                    per_result_tokens=args.per_result_tokens,
                )
                for warning in result.warnings:
                    print(f"dc: warning: {warning}", file=sys.stderr)
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
    except KeyboardInterrupt:
        return 130
    except (
        OSError,
        ValueError,
        TypeError,
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
