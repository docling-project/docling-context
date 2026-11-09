"""Revision-aware lexical retrieval and bounded DocLang context assembly."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import TYPE_CHECKING

from doclang import DocLangXDocument

from .doclang_authoring import get_outline, get_summary
from .models import DocumentRecord, Principal
from .package import MAX_NODE_TEXT_CHARS, bounded_nodes, load_package
from .profiles import MEMORY_PART, profile_kind
from .uri import authorize_uri, parse_uri

if TYPE_CHECKING:
    from .local import LocalContextStore
    from .vectors import EmbeddingProvider, VectorIndex

_SEMANTIC_NAMES = {
    "text",
    "paragraph",
    "caption",
    "table",
    "formula",
    "code",
    "list-item",
    "item",
    "title",
    "footnote",
    "description",
}
_MAX_RESULTS = 100
_RETRIEVAL_COLUMNS = (
    "r.vector_id,r.tenant_id,r.uri,r.document_id,r.revision_id,r.xpath,"
    "r.parent_xpath,r.section_xpath,r.tier,r.page,r.bbox_json,r.text,"
    "r.updated_at,r.asset_path"
)


def remove_index_records(db: sqlite3.Connection, tenant_id: str, uri: str) -> None:
    ids = db.execute(
        "SELECT vector_id FROM retrieval_units WHERE tenant_id=? AND uri=?",
        (tenant_id, uri),
    ).fetchall()
    db.executemany("DELETE FROM retrieval_fts WHERE rowid=?", ids)
    db.execute(
        "DELETE FROM retrieval_units WHERE tenant_id=? AND uri=?", (tenant_id, uri)
    )


def index_package(
    db: sqlite3.Connection,
    record: DocumentRecord,
    package: bytes,
    *,
    document: DocLangXDocument | None = None,
) -> None:
    """Replace one document's projection inside the caller's transaction."""
    document = document or load_package(package)
    remove_index_records(db, record.tenant_id, record.uri)
    if profile_kind(record.uri) == "memories":
        metadata = json.loads(document.get_part_text(MEMORY_PART) or "{}")
        if metadata.get("status") != "accepted":
            db.execute(
                """INSERT INTO lexical_state (tenant_id,uri,revision_id,unit_count)
                   VALUES (?,?,?,0) ON CONFLICT(tenant_id,uri)
                   DO UPDATE SET revision_id=excluded.revision_id,unit_count=0""",
                (record.tenant_id, record.uri, record.revision_id),
            )
            return
    units: list[
        tuple[str, str | None, str | None, int, int | None, str | None, str, str | None]
    ] = []
    archive_paths = set(document.archive_paths())
    picture_assets = []
    for element in ET.fromstring(document.xml()).iter():
        if element.tag.rsplit("}", 1)[-1] != "picture":
            continue
        source = next(
            (child for child in element if child.tag.rsplit("}", 1)[-1] == "src"),
            None,
        )
        path = source.get("uri") if source is not None else None
        picture_assets.append(
            path
            if path and path in archive_paths and path.startswith("assets/")
            else None
        )
    picture_index = 0
    summary = get_summary(document)
    if summary is not None:
        for node in bounded_nodes(summary, limit=100, text_bytes=4_096):
            if node["name"] in {"text", "paragraph"} and node["text"].strip():
                units.append(
                    (
                        f"@summary:{node['xpath']}",
                        None,
                        None,
                        0,
                        None,
                        None,
                        node["text"],
                        None,
                    )
                )
                break
    toc = get_outline(document)
    if toc is not None:
        for node in bounded_nodes(toc, limit=512, text_bytes=1_024):
            if node["name"] in {"heading", "description"} and node["text"].strip():
                units.append(
                    (
                        f"@toc:{node['xpath']}",
                        None,
                        None,
                        1,
                        None,
                        None,
                        node["text"],
                        None,
                    )
                )
    section_xpath = None
    for node in bounded_nodes(document, limit=10_000, text_bytes=8_192):
        name = node["name"].lower()
        if name == "heading":
            section_xpath = node["xpath"]
        asset_path = None
        if name == "picture":
            asset_path = (
                picture_assets[picture_index]
                if picture_index < len(picture_assets)
                else None
            )
            picture_index += 1
        if not node["text"].strip() and asset_path is None:
            continue
        if name == "heading":
            tier = 1
        elif name in _SEMANTIC_NAMES or asset_path is not None:
            tier = 2
        else:
            continue
        units.append(
            (
                node["xpath"],
                node["parent_xpath"],
                section_xpath,
                tier,
                node["page"] if node["page"] > 0 else None,
                json.dumps(node["bbox"]) if node["bbox"] is not None else None,
                node["text"],
                asset_path,
            )
        )
    for xpath, parent, section, tier, page, bbox, content, asset_path in units:
        asset = document.get_part_bytes(asset_path) if asset_path else None
        digest = hashlib.sha256(content.encode("utf-8") + (asset or b"")).hexdigest()
        cursor = db.execute(
            """INSERT INTO retrieval_units
               (tenant_id,uri,document_id,revision_id,xpath,parent_xpath,section_xpath,
                tier,page,bbox_json,text,updated_at,input_hash,asset_path)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                record.tenant_id,
                record.uri,
                record.document_id,
                record.revision_id,
                xpath,
                parent,
                section,
                tier,
                page,
                bbox,
                content,
                record.updated_at,
                digest,
                asset_path,
            ),
        )
        db.execute(
            "INSERT INTO retrieval_fts(rowid,text) VALUES (?,?)",
            (cursor.lastrowid, content),
        )
    db.execute(
        """INSERT INTO lexical_state (tenant_id,uri,revision_id,unit_count)
           VALUES (?,?,?,?) ON CONFLICT(tenant_id,uri)
           DO UPDATE SET revision_id=excluded.revision_id,
             unit_count=excluded.unit_count""",
        (record.tenant_id, record.uri, record.revision_id, len(units)),
    )


@dataclass(frozen=True, slots=True)
class SearchScope:
    uri: str = "docling://resources"
    xpath: str | None = None
    document_uri: str | None = None
    tiers: tuple[int, ...] = (0, 1, 2)
    project_id: str | None = None


@dataclass(frozen=True, slots=True)
class RetrievalHit:
    uri: str
    document_id: str
    revision_id: str
    xpath: str
    parent_xpath: str | None
    section_xpath: str | None
    tier: int
    page: int | None
    bbox: tuple[tuple[float, float], tuple[float, float]] | None
    text: str
    asset_path: str | None
    lexical_score: float
    vector_score: float
    tier_score: float
    freshness_score: float
    proximity_score: float
    score: float
    truncated: bool


@dataclass(frozen=True, slots=True)
class ContextResult:
    hits: tuple[RetrievalHit, ...]
    tokens_used: int
    truncated: bool
    total_hits: int | None = None
    warnings: tuple[str, ...] = ()


def _query_terms(query: str, operator: str = "OR") -> str:
    terms = re.findall(r"\w+", query, flags=re.UNICODE)[:16]
    if not terms:
        raise ValueError("search query must contain a word")
    return f" {operator} ".join(f'"{term}"' for term in terms)


def _token_prefix(text: str, limit: int) -> tuple[str, int, bool]:
    spans = list(re.finditer(r"\S+", text))
    if len(spans) <= limit:
        return text, len(spans), False
    return text[: spans[limit - 1].end()], limit, True


class Retriever:
    """Search an authorized SQLite projection and cite current DCLX bytes."""

    def __init__(
        self,
        store: LocalContextStore,
        *,
        embedder: EmbeddingProvider | None = None,
        vector_index: VectorIndex | None = None,
    ):
        self.store = store
        self.embedder = embedder
        if embedder is None and vector_index is not None:
            raise ValueError("vector index needs an embedding provider")
        if embedder is not None and vector_index is None:
            from .vectors import TurbovecIndex

            vector_index = TurbovecIndex(store, embedder, search_only=True)
        self.vector_index = vector_index

    def _scope_conditions(
        self, principal: Principal, scope: SearchScope
    ) -> tuple[str, list[object]]:
        address = parse_uri(scope.uri.rstrip("/"), prefix=True)
        authorize_uri(principal, address)
        base = address.value
        if scope.document_uri is not None:
            doc = parse_uri(scope.document_uri)
            authorize_uri(principal, doc)
            if doc.value != base and not doc.value.startswith(base + "/"):
                raise ValueError("document is outside URI scope")
        if scope.xpath is not None and (
            not scope.xpath.startswith("/doclang[1]") or len(scope.xpath) > 2_048
        ):
            raise ValueError("invalid XPath scope")
        if not scope.tiers or set(scope.tiers) - {0, 1, 2}:
            raise ValueError("tiers must be a nonempty subset of 0, 1, 2")
        clauses = [
            "r.tenant_id=?",
            "d.available=1",
            "d.revision_id=r.revision_id",
            "(r.uri=? OR substr(r.uri,1,?)=?)",
        ]
        prefix = base + "/"
        params: list[object] = [principal.tenant_id, base, len(prefix), prefix]
        if scope.project_id is not None:
            from .catalog import Catalog

            with Catalog(self.store) as catalog:
                members = catalog.project_resource_uris(principal, scope.project_id)
            if members:
                clauses.append(f"r.uri IN ({','.join('?' for _ in members)})")
                params.extend(members)
            else:
                clauses.append("0=1")
        if scope.document_uri is not None:
            clauses.append("r.uri=?")
            params.append(doc.value)
        if scope.xpath is not None:
            child = scope.xpath + "/"
            clauses.append("(r.xpath=? OR substr(r.xpath,1,?)=?)")
            params.extend((scope.xpath, len(child), child))
        clauses.append(f"r.tier IN ({','.join('?' for _ in scope.tiers)})")
        params.extend(scope.tiers)
        return " AND ".join(clauses), params

    def _allowed(
        self, principal: Principal, scope: SearchScope, *, terms: str | None = None
    ) -> list[sqlite3.Row]:
        where, params = self._scope_conditions(principal, scope)
        if terms is not None:
            source = """retrieval_fts JOIN retrieval_units r
                        ON r.vector_id=retrieval_fts.rowid"""
            where = "retrieval_fts MATCH ? AND " + where
            params = [terms, *params]
        else:
            source = "retrieval_units r"
        rows = self.store.db.execute(
            "SELECT "
            + _RETRIEVAL_COLUMNS
            + " FROM "
            + source
            + " JOIN documents d ON d.tenant_id=r.tenant_id AND d.uri=r.uri WHERE "
            + where
            + " ORDER BY r.vector_id",
            params,
        ).fetchall()
        return [
            row
            for row in rows
            if parse_uri(row["uri"]).namespace != "users"
            or _user_allowed(principal, row["uri"])
        ]

    def _allowed_ids(self, principal: Principal, scope: SearchScope) -> set[int]:
        """Collect every scoped ID without loading text or embeddings."""
        where, params = self._scope_conditions(principal, scope)
        cursor = self.store.db.execute(
            """SELECT r.vector_id,r.uri FROM retrieval_units r
               JOIN documents d ON d.tenant_id=r.tenant_id AND d.uri=r.uri WHERE """
            + where,
            params,
        )
        allowed: set[int] = set()
        while batch := cursor.fetchmany(1_000):
            allowed.update(
                row["vector_id"]
                for row in batch
                if parse_uri(row["uri"]).namespace != "users"
                or _user_allowed(principal, row["uri"])
            )
        return allowed

    def _rows_for_ids(self, identifiers: list[int]) -> list[sqlite3.Row]:
        """Load only vector finalists after the index has searched its allowlist."""
        rows: list[sqlite3.Row] = []
        for start in range(0, len(identifiers), 500):
            batch = identifiers[start : start + 500]
            marks = ",".join("?" for _ in batch)
            rows.extend(
                self.store.db.execute(
                    "SELECT "
                    + _RETRIEVAL_COLUMNS
                    + " FROM retrieval_units r JOIN documents d"
                    " ON d.tenant_id=r.tenant_id AND d.uri=r.uri"
                    " WHERE d.available=1 AND d.revision_id=r.revision_id"
                    f" AND r.vector_id IN ({marks})",
                    batch,
                ).fetchall()
            )
        return rows

    def search(
        self,
        principal: Principal,
        query: str,
        *,
        image: bytes | None = None,
        scope: SearchScope | None = None,
        k: int = 10,
        mode: str = "hybrid",
        max_tokens: int = 2_000,
        per_result_tokens: int = 500,
    ) -> ContextResult:
        if (
            not 1 <= k <= _MAX_RESULTS
            or not 1 <= per_result_tokens <= max_tokens <= 20_000
        ):
            raise ValueError("retrieval limits are out of bounds")
        if mode not in {"lexical", "vector", "hybrid"}:
            raise ValueError("mode must be lexical, vector, or hybrid")
        if mode == "vector" and self.vector_index is None:
            raise ValueError("vector search needs an embedding provider and index")
        if not query.strip() and (image is None or mode != "vector"):
            raise ValueError("search needs query text or a vector-mode image")
        warnings: list[str] = []
        lexical_warning = self.store.lexical_warning(principal)
        if lexical_warning:
            warnings.append(lexical_warning)
        if self.vector_index is not None:
            refresh = getattr(self.vector_index, "refresh", None)
            if callable(refresh):
                refresh()
        if mode in {"vector", "hybrid"} and self.vector_index is not None:
            warning = getattr(self.vector_index, "warning", None)
            if callable(warning):
                message = warning(principal.tenant_id)
                if message:
                    warnings.append(message)
        terms = _query_terms(query) if query.strip() else None
        effective_scope = scope or SearchScope()
        lexical_only = mode == "lexical" or (
            mode == "hybrid" and self.vector_index is None
        )
        if lexical_only and terms is not None and len(query.split()) > 1:
            conjunctive = _query_terms(query, "AND")
            focused = self._allowed(principal, effective_scope, terms=conjunctive)
            if focused:
                terms = conjunctive
                rows = focused
            else:
                rows = self._allowed(principal, effective_scope, terms=terms)
        elif mode != "vector":
            rows = self._allowed(principal, effective_scope, terms=terms)
        else:
            rows = []
        total_hits = len(rows) if lexical_only and terms is not None else None
        by_id = {row["vector_id"]: row for row in rows}
        if not by_id and (mode == "lexical" or self.vector_index is None):
            return ContextResult((), 0, False, total_hits, tuple(warnings))
        scores: dict[int, list[float]] = {}
        if mode in {"lexical", "hybrid"} and terms is not None:
            query_words = re.findall(r"\w+", query.casefold())
            for identifier, row in by_id.items():
                content = row["text"].casefold()
                frequency = sum(content.count(term) for term in query_words)
                scores[identifier] = [min(1.0, frequency / 4), 0.0]
        if mode in {"vector", "hybrid"} and self.vector_index is not None:
            assert self.embedder is not None
            if image is not None:
                from .embeddings import embed_image, embed_text_image

                query_vector = (
                    embed_text_image(self.embedder, query, image)
                    if query.strip()
                    else embed_image(self.embedder, image)
                )
            else:
                query_embed = getattr(self.embedder, "embed_query", None)
                query_vector = (
                    query_embed(query)
                    if callable(query_embed)
                    else self.embedder.embed([query])[0]
                )
            allowed_ids = self._allowed_ids(principal, effective_scope)
            found = (
                self.vector_index.search(query_vector, allowed_ids, min(k * 4, 100))
                if query_vector is not None
                else []
            )
            missing_ids = [
                vector_id
                for vector_id, _ in found
                if vector_id in allowed_ids and vector_id not in by_id
            ]
            for row in self._rows_for_ids(missing_ids):
                by_id[row["vector_id"]] = row
            for vector_id, score in found:
                if vector_id in by_id:
                    scores.setdefault(vector_id, [0.0, 0.0])[1] = score
        timestamps = sorted({by_id[identifier]["updated_at"] for identifier in scores})
        fresh_rank = {value: rank for rank, value in enumerate(timestamps)}
        siblings: dict[tuple[str, str], int] = {}
        for identifier in scores:
            row = by_id[identifier]
            section = row["section_xpath"] or row["parent_xpath"]
            if section and section != "/doclang[1]":
                key = (row["uri"], section)
                siblings[key] = siblings.get(key, 0) + 1

        def components(identifier: int) -> tuple[float, float, float]:
            row = by_id[identifier]
            tier = 0.06 if row["tier"] == 0 else 0.03 if row["tier"] == 1 else 0.0
            freshness = (
                0.02 * fresh_rank[row["updated_at"]] / (len(timestamps) - 1)
                if len(timestamps) > 1
                else 0.0
            )
            proximity = (
                0.02
                if siblings.get(
                    (row["uri"], row["section_xpath"] or row["parent_xpath"]), 0
                )
                > 1
                else 0.0
            )
            return tier, freshness, proximity

        ranked = sorted(
            scores,
            key=lambda identifier: (
                -(
                    scores[identifier][0]
                    + max(0.0, scores[identifier][1])
                    + sum(components(identifier))
                ),
                -identifier,
            ),
        )
        hits: list[RetrievalHit] = []
        documents: dict[tuple[str, str], DocLangXDocument] = {}
        used = 0
        truncated = False
        selected_paths: set[tuple[str, str]] = set()
        for identifier in ranked:
            if len(hits) >= k or used >= max_tokens:
                truncated = True
                break
            row = by_id[identifier]
            path = (row["uri"], row["xpath"])
            if any(
                prior_uri == path[0]
                and (
                    path[1].startswith(prior_xpath + "/")
                    or prior_xpath.startswith(path[1] + "/")
                )
                for prior_uri, prior_xpath in selected_paths
            ):
                continue
            try:
                current = self.store.get_record(principal, row["uri"])
                if current.revision_id != row["revision_id"]:
                    continue
                text, source_truncated = self._exact_text(principal, row, documents)
                if (
                    self.store.get_record(principal, row["uri"]).revision_id
                    != row["revision_id"]
                ):
                    continue
            except (KeyError, OSError, ValueError):
                continue
            available = min(per_result_tokens, max_tokens - used)
            excerpt, count, shortened = _token_prefix(text, available)
            if count == 0 and row["asset_path"] is None:
                continue
            used += count
            selected_paths.add(path)
            lexical, vector = scores[identifier]
            tier_score, freshness_score, proximity_score = components(identifier)
            bbox_value = json.loads(row["bbox_json"]) if row["bbox_json"] else None
            bbox = (
                (
                    (float(bbox_value[0][0]), float(bbox_value[0][1])),
                    (float(bbox_value[1][0]), float(bbox_value[1][1])),
                )
                if bbox_value
                else None
            )
            hits.append(
                RetrievalHit(
                    row["uri"],
                    row["document_id"],
                    row["revision_id"],
                    row["xpath"],
                    row["parent_xpath"],
                    row["section_xpath"],
                    row["tier"],
                    row["page"],
                    bbox,
                    excerpt,
                    row["asset_path"],
                    lexical,
                    vector,
                    tier_score,
                    freshness_score,
                    proximity_score,
                    lexical
                    + max(0.0, vector)
                    + tier_score
                    + freshness_score
                    + proximity_score,
                    shortened or source_truncated,
                )
            )
            truncated |= shortened or source_truncated
        return ContextResult(tuple(hits), used, truncated, total_hits, tuple(warnings))

    def _exact_text(
        self,
        principal: Principal,
        row: sqlite3.Row,
        documents: dict[tuple[str, str], DocLangXDocument],
    ) -> tuple[str, bool]:
        key = (row["uri"], row["revision_id"])
        document = documents.get(key)
        if document is None:
            document = self.store.get_document(principal, row["uri"])
            documents[key] = document
        if row["xpath"].startswith(("@summary:", "@toc:")):
            sidecar_name, xpath = row["xpath"].split(":", 1)
            sidecar = (
                get_summary(document)
                if sidecar_name == "@summary"
                else get_outline(document)
            )
            if sidecar is None:
                raise KeyError(row["xpath"])
            nodes = bounded_nodes(
                sidecar, xpath=xpath, limit=1, text_bytes=MAX_NODE_TEXT_CHARS
            )
            if not nodes or nodes[0]["xpath"] != xpath:
                raise KeyError(xpath)
            return nodes[0]["text"], nodes[0]["truncated"]
        nodes = bounded_nodes(
            document,
            xpath=row["xpath"],
            limit=1,
            text_bytes=MAX_NODE_TEXT_CHARS * 4,
        )
        if not nodes or nodes[0]["xpath"] != row["xpath"]:
            raise KeyError(row["xpath"])
        value = nodes[0]["text"]
        return value[:MAX_NODE_TEXT_CHARS], (
            nodes[0]["truncated"] or len(value) > MAX_NODE_TEXT_CHARS
        )


def _user_allowed(principal: Principal, uri: str) -> bool:
    try:
        authorize_uri(principal, parse_uri(uri))
    except PermissionError:
        return False
    return True
