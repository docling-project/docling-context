"""Shared DocLang authoring guidance and validated outline helpers."""

from __future__ import annotations

from urllib.parse import quote, unquote
from xml.etree import ElementTree as ET

from doclang import DoclangDocument, DocLangXDocument, DocLangXDocumentRole

from .package import PackageError

DEFAULT_SKILL_ID = "doclang-authoring"
DEFAULT_SKILL_URI = f"docling://resources/skills/{DEFAULT_SKILL_ID}"
DEFAULT_SKILL_NAME = "Write and validate DocLang"
DEFAULT_SKILL_SUMMARY = "Author schema-valid DCLG XML and DCLX packages."
DEFAULT_SKILL_TEXT = """Write DocLang sources as DCLG XML with a <doclang> root. Use elements allowed by the bundled DocLang XSD, such as <heading level="1">Title</heading> and <text>Body</text>. Escape XML special characters. A DCLX file is a DocLang package containing a DCLG body and optional companion documents and parts; it is not just XML renamed with a .dclx extension.

Before saving DCLG, parse it with DocLang and run document.validate(allow_empty_namespace=True). This checks the bundled XSD and Schematron rules. Before saving DCLX, validate the body and every generated DCLG companion document, run document.validate_package(), then write_bytes(); reopen the bytes and validate the package again. Validate summaries and outlines as DocLang documents too. Do not invent XML elements such as <toc> or <entry>: they are not valid DocLang body elements. Represent an outline with <heading> nodes, optionally putting <href uri="#ENCODED_XPATH"/> before its title. Use the percent-encoded source XPath in the fragment. Call package.set(DocLangXDocumentRole.TABLE_OF_CONTENTS, outline) to store the native annotations/toc.dclg companion document. Use dc library outline or dc library show to inspect citations.

For the installed Python API: from doclang import DocLangXDocument; document = DocLangXDocument(); document.read_xml(xml); report = document.validate(allow_empty_namespace=True); assert report.ok(), (report.xsd_errors, report.schematron_errors). For a DCLX package, also assert document.validate_package()["ok"]. The doclang package bundles the authoritative XSD and Schematron rules; follow their current version rather than copying a partial grammar from this skill."""


def validate_doclang(document: DoclangDocument, *, label: str) -> None:
    """Reject XML that parses but violates the bundled XSD or Schematron."""
    report = document.validate(allow_empty_namespace=True)
    if not report.ok():
        raise PackageError(
            f"invalid {label}: {report.xsd_errors or report.schematron_errors}"
        )


def get_outline(document: DocLangXDocument) -> DoclangDocument | None:
    """Read the native DCLG outline companion document."""
    return document.get(DocLangXDocumentRole.TABLE_OF_CONTENTS)


def get_summary(document: DocLangXDocument) -> DoclangDocument | None:
    return document.get(DocLangXDocumentRole.SUMMARY)


def set_summary(document: DocLangXDocument, xml: str) -> bool:
    return document.set(DocLangXDocumentRole.SUMMARY, DoclangDocument(xml))


def set_outline(document: DocLangXDocument, xml: str) -> bool:
    return document.set(DocLangXDocumentRole.TABLE_OF_CONTENTS, DoclangDocument(xml))


def outline_xml(entries: list[tuple[str, str, int]]) -> str:
    """Build a DocLang TOC; href fragments preserve source XPaths."""
    root = ET.Element("doclang")
    for xpath, title, level in entries:
        heading = ET.SubElement(root, "heading", {"level": str(max(1, level))})
        ET.SubElement(heading, "href", {"uri": f"#{quote(xpath, safe='')}"})
        heading[-1].tail = title
    return ET.tostring(root, encoding="unicode")


def outline_links(xml: str) -> list[str]:
    links: list[str] = []
    for heading in ET.fromstring(xml).iter("heading"):
        href = heading.find("href")
        if href is not None and (uri := href.get("uri", "")).startswith("#"):
            xpath = unquote(uri[1:])
            if xpath.startswith("/doclang[1]/"):
                links.append(xpath)
    return links
