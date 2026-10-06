"""The local dc command for ingestion, inspection, and basic lexical lookup."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter
from dataclasses import asdict, replace
from pathlib import Path

from .conversion_config import PdfConversionConfig
from .converters import LocalDoclingConverter
from .ingestion import Ingestor
from .jobs import CollectionWorker
from .local import LocalContextStore
from .models import Principal
from .package import bounded_nodes
from .uri import authorize_uri, parse_uri


def _json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _prefix(value: str) -> str:
    return parse_uri(value.rstrip("/"), prefix=True).value


def _collection(value: str) -> str:
    uri = value if value.startswith("docling://") else f"docling://resources/{value}"
    address = parse_uri(uri, prefix=True)
    if address.namespace != "resources" or len(address.segments) != 1:
        raise ValueError("collection must be one resources segment")
    return address.value


def _slug(value: str) -> str:
    result = re.sub(r"[^\w.-]+", "-", value, flags=re.UNICODE).strip(".-")
    if not result or result in {".", ".."}:
        raise ValueError(f"cannot make a URI segment from {value!r}")
    if result != value:
        result += f"-{hashlib.sha256(value.encode()).hexdigest()[:8]}"
    return result


def _uri_for(
    path: Path, base: Path, collection: str, *, include_extension: bool = False
) -> str:
    relative = path.relative_to(base)
    stem = path.name[:-9] if path.name.lower().endswith(".dclg.xml") else path.stem
    name = path.name if include_extension else stem
    segments = [_slug(part) for part in relative.parts[:-1]] + [_slug(name)]
    return f"{collection}/{'/'.join(segments)}"


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
    collection = _collection(args.collection)
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
        if args.uri:
            raise ValueError("--uri can only be used with one file")
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
    resources = [(path, args.uri or _uri_for(path, base, collection)) for path in paths]
    collisions = {
        uri for uri, count in Counter(uri for _, uri in resources).items() if count > 1
    }
    resources = [
        (
            path,
            _uri_for(path, base, collection, include_extension=True)
            if uri in collisions
            else uri,
        )
        for path, uri in resources
    ]
    if len({uri for _, uri in resources}) != len(resources):
        raise ValueError("multiple files map to the same context URI")
    ingestor = Ingestor(
        store,
        allowed_roots=(base,),
        converter=LocalDoclingConverter(_config(args)),
    )
    worker = CollectionWorker(store)
    for path, uri in resources:
        record = ingestor.add_resource(principal, uri, path, force=args.force)
        address = parse_uri(record.uri)
        task_id = None
        if address.namespace == "resources":
            collection_uri = f"docling://resources/{address.segments[0]}"
            matches = [
                job
                for job in worker.jobs(principal, collection_uri)
                if job.input_revision == record.revision_id
            ]
            task_id = matches[-1].job_id if matches else None
        _json(
            {"uri": record.uri, "revision_id": record.revision_id, "task_id": task_id}
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
    )
    parser.add_argument(
        "--tenant", default=os.environ.get("DOCLING_CONTEXT_TENANT", "default")
    )
    parser.add_argument(
        "--user", default=os.environ.get("DOCLING_CONTEXT_USER", "local")
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="Show local store and job counts")
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
    add.add_argument(
        "--collection",
        default="documents",
        help="Collection for generated resource URIs (default: documents).",
    )
    add.add_argument(
        "--uri",
        help="Exact URI for a single file (default: derived from collection and filename).",
    )
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
    task = commands.add_parser("task", help="Inspect collection-summary jobs")
    task_commands = task.add_subparsers(dest="task_command", required=True)
    task_status = task_commands.add_parser("status")
    task_status.add_argument("job_id")
    worker = commands.add_parser("worker", help="Process queued collection summaries")
    worker.add_argument("--once", action="store_true")
    listing = commands.add_parser("ls", help="List direct children")
    listing.add_argument("uri")
    tree = commands.add_parser("tree", help="Walk a context subtree")
    tree.add_argument("uri")
    tree.add_argument("-L", "--depth", type=int, default=2)
    for name in ("find", "grep"):
        search = commands.add_parser(name, help="Bounded lexical lookup")
        search.add_argument("term")
        search.add_argument("--uri", default="docling://resources/")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    principal = Principal(args.tenant, args.user)
    try:
        with LocalContextStore(args.store) as store:
            worker = CollectionWorker(store)
            if args.command == "status":
                counts = {
                    table: store.db.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE tenant_id=?",
                        (principal.tenant_id,),
                    ).fetchone()[0]
                    for table in ("documents", "revisions", "jobs")
                }
                _json({"store": str(store.root.resolve()), **counts})
            elif args.command == "add-resource":
                _add_resource(args, store, principal)
            elif args.command == "task":
                _json(asdict(worker.get_job(principal, args.job_id)))
            elif args.command == "worker":
                if args.once:
                    job = worker.run_once()
                    _json({"job_id": job.job_id if job else None})
                else:
                    from threading import Event

                    try:
                        worker.run_forever(Event())
                    except KeyboardInterrupt:
                        pass
            elif args.command == "ls":
                for entry in store.list_children(principal, _prefix(args.uri)):
                    _json(asdict(entry))
            elif args.command == "tree":
                for entry in store.walk(
                    principal, _prefix(args.uri), depth=args.depth, limit=1_000
                ):
                    print(f"{'  ' * (entry.depth - 1)}{entry.uri}")
            elif args.command in {"find", "grep"}:
                hits = _scan(store, principal, args.term, _prefix(args.uri))
                if args.command == "find":
                    hits.sort(
                        key=lambda hit: (
                            str(hit["text"]).casefold().count(args.term.casefold())
                        ),
                        reverse=True,
                    )
                for hit in hits:
                    _json(hit)
    except (OSError, ValueError, RuntimeError, KeyError, PermissionError) as exc:
        print(f"dc: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
