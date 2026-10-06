"""The local dc command for ingestion, inspection, and basic lexical lookup."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter
from collections.abc import Mapping
from dataclasses import asdict, replace
from pathlib import Path

from tabulate import tabulate  # type: ignore[import-untyped]
from tqdm import tqdm  # type: ignore[import-untyped]

from .conversion_config import PdfConversionConfig
from .converters import LocalDoclingConverter
from .embeddings import (
    CLIP_MODEL,
    DEFAULT_MODEL,
    local_embedding_provider,
    stored_embedding_provider,
)
from .ingestion import Ingestor
from .jobs import CollectionWorker
from .local import LocalContextStore
from .models import Principal
from .package import bounded_nodes
from .retrieval import RetrievalHit, Retriever, SearchScope
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
    lex_commands.add_parser(
        "rebuild", help="Rebuild search records from stored DCLX documents"
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
    task_status.add_argument(
        "--json", action="store_true", help="Print JSON instead of a table."
    )
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
                        _json(rebuild_result)
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
                        _json(store.index_status(principal))
            elif args.command == "add-resource":
                _add_resource(args, store, principal)
            elif args.command == "task":
                _status(
                    asdict(worker.get_job(principal, args.job_id)),
                    json_output=args.json,
                )
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
    except (OSError, ValueError, RuntimeError, KeyError, PermissionError) as exc:
        print(f"dc: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
