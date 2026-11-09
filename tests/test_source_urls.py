"""Remote document download and shared-library import behavior."""

from __future__ import annotations

import json

import httpx
import pytest
from doclang import DocLangXDocument

from docling_context import ConversionResult, LocalContextStore, Principal
from docling_context.catalog import Catalog
from docling_context.cli import main
from docling_context.source_urls import download_source, is_source_url


def test_extensionless_pdf_url_downloads_with_a_conversion_filename():
    url = "https://arxiv.org/pdf/2608.23083"
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "application/pdf", "content-length": "8"},
            content=b"%PDF-1.4",
            request=request,
        )
    )
    data, filename = download_source(url, transport=transport)
    assert data == b"%PDF-1.4"
    assert filename == "2608.23083.pdf"

    generic_type = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "application/octet-stream"},
            content=b"%PDF-1.4",
            request=request,
        )
    )
    assert download_source(url, transport=generic_type)[1] == "2608.23083.pdf"


def test_download_rejects_oversized_and_unsupported_sources():
    url = "https://example.test/file.pdf"
    advertised = httpx.MockTransport(
        lambda request: httpx.Response(
            200, headers={"content-length": "100"}, content=b"x", request=request
        )
    )
    with pytest.raises(ValueError, match="size limit"):
        download_source(url, max_bytes=8, transport=advertised)
    streamed = httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"x" * 9, request=request)
    )
    with pytest.raises(ValueError, match="size limit"):
        download_source(url, max_bytes=8, transport=streamed)
    with pytest.raises(ValueError, match="HTTP or HTTPS"):
        is_source_url("file:///etc/passwd")
    with pytest.raises(ValueError, match="credentials"):
        is_source_url("https://alice:secret@example.test/file.pdf")


def test_download_follows_redirect_and_uses_content_disposition():
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/paper":
            return httpx.Response(
                302, headers={"location": "/files/123"}, request=request
            )
        return httpx.Response(
            200,
            headers={"content-disposition": 'attachment; filename="paper.pdf"'},
            content=b"%PDF-1.4",
            request=request,
        )

    assert download_source(
        "https://example.test/paper", transport=httpx.MockTransport(respond)
    ) == (b"%PDF-1.4", "paper.pdf")


def test_url_import_reuses_library_document_and_links_project(
    tmp_path, monkeypatch, capsys
):
    url = "https://arxiv.org/pdf/2608.23083"
    source = b"%PDF-1.4 mocked source"
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "application/pdf"},
            content=source,
            request=request,
        )
    )
    monkeypatch.setattr(
        "docling_context.cli.download_source",
        lambda value: download_source(value, transport=transport),
    )
    monkeypatch.setattr(
        "docling_context.cli._source_page_units", lambda source, filename: 1
    )

    class Converter:
        def __init__(self, _config):
            pass

        def fingerprint_options(self):
            return {}

        def convert(self, data, filename):
            assert data == source and filename == "2608.23083.pdf"
            document = DocLangXDocument()
            assert document.read_xml("<doclang><text>Material study</text></doclang>")
            return ConversionResult(document.write_bytes(), "test", "1")

    monkeypatch.setattr("docling_context.cli.LocalDoclingConverter", Converter)
    base = ["--store", str(tmp_path / "store")]
    assert (
        main(
            [
                *base,
                "project",
                "create",
                "--project",
                "materials",
                "--title",
                "Materials",
            ]
        )
        == 0
    )
    capsys.readouterr()
    command = [
        *base,
        "library",
        "add",
        "--source",
        url,
        "--project",
        "materials",
        "--from",
        "pdf",
        "--json",
    ]
    assert main(command) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["uri"].startswith("docling://resources/library/")
    assert main(command) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["uri"] == first["uri"]
    with LocalContextStore(tmp_path / "store") as store:
        principal = Principal("default", "local")
        record = store.get_record(principal, first["uri"])
        assert record.source["origin_uri"] == url
        assert Catalog(store).overview(principal)[0]["documents"] == 1
