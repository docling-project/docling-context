"""Import source artifacts as revisioned DCLX documents with context tiers."""

from __future__ import annotations

import hashlib
import json
import uuid
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Protocol
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

from doclang import DocLangXDocument

from .contracts import ContextStore
from .converters import ConversionResult, Converter, LocalDoclingConverter
from .local import RecordMissing
from .models import DocumentRecord, Principal
from .package import MAX_PACKAGE_BYTES, PackageError, bounded_nodes, load_package
from .uri import authorize_uri, parse_uri


class SummaryProvider(Protocol):
    def summarize(self, document: DocLangXDocument, source_revision: str) -> str: ...


def _fallback_summary(document: DocLangXDocument) -> str:
    nodes = bounded_nodes(document, limit=10_000, text_bytes=1_024)
    snippets = [
        str(node["text"]).strip()
        for node in nodes
        if node["name"] in {"heading", "text", "paragraph"} and node["text"]
    ]
    body = " ".join(snippets)[:600] or "Empty document"
    return f"<doclang><text>{escape(body)}</text></doclang>"


def _toc(document: DocLangXDocument) -> str:
    headings = [
        node
        for node in bounded_nodes(document, limit=10_000, text_bytes=512)
        if node["name"] == "heading"
    ][:256]
    root = ET.Element("doclang")
    toc = ET.SubElement(root, "toc")
    xml_headings = [
        element
        for element in ET.fromstring(document.xml()).iter()
        if element.tag.rsplit("}", 1)[-1] == "heading"
    ][:256]
    stack: list[tuple[int, ET.Element]] = []
    for node, element in zip(headings, xml_headings, strict=False):
        try:
            level = max(1, min(6, int(element.get("level", "1"))))
        except ValueError:
            level = 1
        while stack and stack[-1][0] >= level:
            stack.pop()
        parent = stack[-1][1] if stack else toc
        entry = ET.SubElement(parent, "entry", {"xpath": str(node["xpath"])})
        ET.SubElement(entry, "description").text = str(node["text"])[:200]
        stack.append((level, entry))
    return ET.tostring(root, encoding="unicode")


def _fingerprint(source: bytes, converter_id: str, options: dict[str, object]) -> str:
    settings = json.dumps(
        {"converter": converter_id, "options": options},
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256()
    digest.update(source)
    digest.update(b"\x00")
    digest.update(settings.encode())
    return digest.hexdigest()


class Ingestor:
    """Local ingestion; file reads require an explicitly allowed root."""

    def __init__(
        self,
        store: ContextStore,
        *,
        allowed_roots: tuple[Path, ...] = (),
        converter: Converter | None = None,
        summary_provider: SummaryProvider | None = None,
    ):
        self.store = store
        self.allowed_roots = tuple(root.resolve() for root in allowed_roots)
        self.converter = converter
        self.summary_provider = summary_provider

    def add_resource(
        self,
        principal: Principal,
        uri: str,
        source: bytes | Path,
        *,
        filename: str | None = None,
        origin_uri: str | None = None,
        force: bool = False,
    ) -> DocumentRecord:
        address = parse_uri(uri)
        authorize_uri(principal, address)
        if address.namespace == "resources" and address.segments[1:] == (
            "_context",
            "summary",
        ):
            raise ValueError("reserved collection summary URI")
        name: str | None
        origin: str | None
        if isinstance(source, Path):
            resolved = source.resolve(strict=True)
            if not any(resolved.is_relative_to(root) for root in self.allowed_roots):
                raise PermissionError("source path is outside allowed roots")
            if resolved.stat().st_size > MAX_PACKAGE_BYTES:
                raise ValueError("source file is too large")
            data = resolved.read_bytes()
            name = filename or resolved.name
            origin = origin_uri or resolved.as_uri()
        elif isinstance(source, bytes):
            data = source
            name = filename
            origin = origin_uri
        else:
            raise TypeError("source must be bytes or an allowed local Path")
        if not name or not data or len(data) > MAX_PACKAGE_BYTES:
            raise ValueError("source needs a filename and bounded nonempty bytes")
        source_sha256 = hashlib.sha256(data).hexdigest()
        origin = origin or f"sha256:{source_sha256}"
        suffix = name.lower()
        native = suffix.endswith((".dclg", ".dclg.xml", ".dclx"))
        converter = self.converter if not native else None
        if not native and converter is None:
            converter = LocalDoclingConverter()
        converter_id = "doclang-native" if native else type(converter).__name__
        options: dict[str, object] = {}
        if not native:
            settings = getattr(converter, "fingerprint_options", None)
            options = (
                settings()
                if callable(settings)
                else {
                    "max_pages": getattr(converter, "max_pages", None),
                    "max_file_bytes": getattr(converter, "max_file_bytes", None),
                }
            )
        if isinstance(converter, LocalDoclingConverter):
            try:
                options["version"] = version("docling")
            except PackageNotFoundError:
                options["version"] = "uninstalled"
        options["summary_provider"] = (
            None
            if self.summary_provider is None
            else type(self.summary_provider).__qualname__
        )
        fingerprint = _fingerprint(data, converter_id, options)
        try:
            current = self.store.get_record(principal, address.value)
        except RecordMissing:
            current = None
        if (
            current is not None
            and current.source.get("fingerprint") == fingerprint
            and not force
        ):
            return current
        revision_id = uuid.uuid4().hex
        if suffix.endswith(".dclx"):
            document = load_package(data)
            conversion = ConversionResult(data, "doclang-native", "1")
        elif suffix.endswith((".dclg", ".dclg.xml")):
            document = DocLangXDocument()
            if not document.read_xml(data.decode("utf-8")):
                raise PackageError(document.last_error() or "invalid DocLang XML")
            conversion = ConversionResult(b"", "doclang-native", "1")
        else:
            assert converter is not None
            conversion = converter.convert(data, name)
            document = load_package(conversion.package)

        method = "extractive"
        summary = _fallback_summary(document)
        if self.summary_provider is not None:
            try:
                proposed = self.summary_provider.summarize(document, revision_id)
                if not isinstance(proposed, str) or len(proposed) > 16_384:
                    raise ValueError("model summary exceeds the size limit")
                probe = DocLangXDocument()
                if not probe.read_xml(proposed):
                    raise ValueError(probe.last_error() or "invalid summary")
                if not document.set_document_summary(proposed):
                    raise ValueError(document.last_error() or "invalid summary sidecar")
                method = "model"
            except Exception:  # noqa: BLE001 - providers may raise arbitrary exceptions
                method = "extractive-fallback"
        if method != "model" and not document.set_document_summary(summary):
            raise PackageError(document.last_error() or "cannot set summary")
        if not document.set_toc(_toc(document)):
            raise PackageError(document.last_error() or "cannot set TOC")
        manifest = {
            "schema_version": 1,
            "source_revision": revision_id,
            "source_fingerprint": fingerprint,
            "summary_method": method,
            "toc_method": "heading-outline",
        }
        document.set_part_text(
            "context/manifest.json",
            json.dumps(manifest, sort_keys=True),
            "application/json",
        )
        package = document.write_bytes()
        load_package(package)
        provenance = {
            "fingerprint": fingerprint,
            "source_sha256": source_sha256,
            "origin_uri": origin,
            "filename": name,
            "converter": conversion.converter,
            "converter_version": conversion.version,
            "converter_options": conversion.options,
            "warnings": list(conversion.warnings),
            "summary_method": method,
        }
        return self.store.put(
            principal,
            address.value,
            package,
            expected_revision=current.revision_id if current else None,
            source=provenance,
            revision_id=revision_id,
        )
