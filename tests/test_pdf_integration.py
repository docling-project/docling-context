"""Optional end-to-end local PDF conversion check."""

from __future__ import annotations

import pytest

from docling_context import (
    Ingestor,
    LocalContextStore,
    LocalDoclingConverter,
    Principal,
)


def _one_page_pdf() -> bytes:
    content = b"BT /F1 18 Tf 72 720 Td (Hello DocLang) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length "
        + str(len(content)).encode()
        + b" >>\nstream\n"
        + content
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, body in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        output.extend(f"{offset:010} 00000 n \n".encode())
    output.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(output)


def test_local_pdf_to_valid_dclx(tmp_path):
    pytest.importorskip("docling")
    with LocalContextStore(tmp_path) as store:
        record = Ingestor(
            store, converter=LocalDoclingConverter(max_pages=2)
        ).add_resource(
            Principal("tenant", "alice"),
            "docling://resources/papers/hello",
            _one_page_pdf(),
            filename="hello.pdf",
        )
        package = store.get_document(Principal("tenant", "alice"), record.uri)
        assert "Hello DocLang" in package.xml()
        assert package.summary() is not None
        assert package.toc() is not None
        assert any(node["bbox"] is not None for node in package.iter_nodes(limit=1000))
