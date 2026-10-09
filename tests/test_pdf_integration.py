"""Optional end-to-end local PDF conversion check."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest
from doclang import DocLangXDocument

from docling_context import (
    Ingestor,
    LocalContextStore,
    LocalDoclingConverter,
    PdfConversionConfig,
    Principal,
)
from docling_context.catalog import Catalog
from docling_context.cli import main


def _native_conversion_available() -> bool:
    if sys.platform != "darwin":
        return True
    try:
        import torch
    except ImportError:
        return False
    return bool(torch.backends.mps.is_available())


native_conversion = pytest.mark.skipif(
    not _native_conversion_available(),
    reason="native Docling layout conversion requires Metal in this macOS environment",
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


@native_conversion
def test_local_pdf_to_valid_dclx(tmp_path):
    pytest.importorskip("docling")
    with LocalContextStore(tmp_path) as store:
        config = PdfConversionConfig(
            do_ocr=False,
            do_table_structure=False,
            do_chart_extraction=False,
            max_pages=2,
        )
        record = Ingestor(store, converter=LocalDoclingConverter(config)).add_resource(
            Principal("tenant", "alice"),
            "docling://resources/library/hello",
            _one_page_pdf(),
            filename="hello.pdf",
        )
        package = store.get_document(Principal("tenant", "alice"), record.uri)
        assert "Hello DocLang" in package.xml()
        assert package.summary() is not None
        assert package.toc() is not None
        assert any(node["bbox"] is not None for node in package.iter_nodes(limit=1000))
        assert record.source["converter_options"]["do_ocr"] is False
        assert record.source["converter_options"]["do_table_structure"] is False


@native_conversion
def test_cli_imports_nested_pdf_folder(tmp_path, capsys):
    pytest.importorskip("docling")
    tenant = "test"
    folder = tmp_path / "papers"
    (folder / "year").mkdir(parents=True)
    (folder / "first.pdf").write_bytes(_one_page_pdf())
    (folder / "year" / "second.pdf").write_bytes(_one_page_pdf())
    config = tmp_path / "config.json"
    config.write_text('{"do_ocr": false, "do_table_structure": false}')
    store = tmp_path / "store"
    with LocalContextStore(store) as local:
        catalog = Catalog(local)
        base = ["--store", str(store), "--tenant", tenant]
        assert main([*base, "project", "create", "papers", "--title", "Papers"]) == 0
        capsys.readouterr()
        assert (
            main(
                [
                    *base,
                    "add-resource",
                    str(folder),
                    "--project",
                    "papers",
                    "-r",
                    "--config",
                    str(config),
                    "--json",
                ]
            )
            == 0
        )
        uris = {
            json.loads(line)["uri"] for line in capsys.readouterr().out.splitlines()
        }
        # Both PDF files contain identical binary bytes and share one library item.
        assert len(uris) == 1
        assert next(iter(uris)).startswith("docling://resources/library/")
        assert catalog.overview(Principal(tenant, "local"))[0]["documents"] == 1


@native_conversion
def test_local_office_and_image_formats_to_valid_dclx(tmp_path):
    pytest.importorskip("docling")
    word = pytest.importorskip("docx")
    powerpoint = pytest.importorskip("pptx")
    excel = pytest.importorskip("openpyxl")
    pillow = pytest.importorskip("PIL.Image")

    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    word_path = source_dir / "report.docx"
    word_document = word.Document()
    word_document.add_paragraph("Word document content")
    word_document.save(word_path)

    slides_path = source_dir / "slides.pptx"
    presentation = powerpoint.Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[5])
    slide.shapes.title.text = "Presentation content"
    presentation.save(slides_path)

    workbook_path = source_dir / "table.xlsx"
    workbook = excel.Workbook()
    workbook.active["A1"] = "Spreadsheet content"
    workbook.save(workbook_path)

    image_path = source_dir / "picture.png"
    pillow.new("RGB", (120, 60), "white").save(image_path)

    with LocalContextStore(tmp_path / "store") as store:
        ingestor = Ingestor(
            store,
            allowed_roots=(source_dir,),
            converter=LocalDoclingConverter(PdfConversionConfig(do_ocr=False)),
        )
        for path in (word_path, slides_path, workbook_path, image_path):
            uri = f"docling://resources/library/{path.stem}"
            record = ingestor.add_resource(Principal("tenant", "alice"), uri, path)
            assert record.source["converter"] == "docling-local"
            package = store.get_document(Principal("tenant", "alice"), uri)
            assert "<doclang" in package.xml()


def test_default_ocr_and_unlimited_pages_are_passed_to_docling(monkeypatch):
    converter_module = pytest.importorskip("docling.document_converter")
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import RapidOcrOptions

    captured = {"initializations": 0, "filenames": []}

    class StubDocument:
        def save_as_doclang_archive(self, path):
            package = DocLangXDocument()
            assert package.read_xml("<doclang><text>Converted</text></doclang>")
            path.write_bytes(package.write_bytes())

    class StubConverter:
        def __init__(self, *, format_options):
            captured["initializations"] += 1
            captured["format_options"] = format_options

        def convert(self, path, **kwargs):
            captured["filename"] = path.name
            captured["filenames"].append(path.name)
            captured["kwargs"] = kwargs
            return SimpleNamespace(document=StubDocument(), errors=[])

    monkeypatch.setattr(converter_module, "DocumentConverter", StubConverter)
    converter = LocalDoclingConverter()
    converter.convert(b"%PDF", "paper.pdf")
    converter.convert(b"%PDF", "second.pdf")
    assert captured["initializations"] == 1
    assert captured["filenames"] == ["paper.pdf", "second.pdf"]
    assert captured["kwargs"]["max_num_pages"] == sys.maxsize
    options = captured["format_options"]
    assert InputFormat.IMAGE in options
    pdf_pipeline = options[InputFormat.PDF].pipeline_options
    assert pdf_pipeline.generate_page_images
    assert pdf_pipeline.generate_picture_images
    assert isinstance(pdf_pipeline.ocr_options, RapidOcrOptions)
    assert pdf_pipeline.ocr_options.lang == ["en"]
    assert pdf_pipeline.ocr_options.backend == "onnxruntime"
    assert pdf_pipeline.ocr_options.model_size == "tiny"
    assert pdf_pipeline.ocr_options.rapidocr_params["Global.log_level"] == "warning"
