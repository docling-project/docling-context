"""Configuration and command-line workflows."""

from __future__ import annotations

import json

import pytest
from doclang import DocLangXDocument

from docling_context import (
    ConversionResult,
    Ingestor,
    LocalContextStore,
    PdfConversionConfig,
    Principal,
)
from docling_context.cli import _slug, main


def test_conversion_config_json_is_strict(tmp_path):
    config_file = tmp_path / "conversion.json"
    config_file.write_text(
        '{"do_ocr": false, "do_chart_extraction": true, "table_mode": "accurate"}'
    )
    config = PdfConversionConfig.from_json_file(config_file)
    assert not config.do_ocr and config.do_chart_extraction
    assert config.table_mode == "accurate"
    assert config.to_dict()["max_pages"] is None
    assert config.generate_page_images and config.generate_picture_images
    with pytest.raises(ValueError, match="table_mode must be accurate"):
        PdfConversionConfig.from_dict({"table_mode": "fast"})
    with pytest.raises(ValueError, match="unknown conversion"):
        PdfConversionConfig.from_dict({"do_ocr": False, "unexpected": True})
    with pytest.raises(TypeError, match="boolean"):
        PdfConversionConfig.from_dict({"do_ocr": "false"})


def test_filename_normalization_does_not_merge_distinct_names():
    assert _slug("a b") != _slug("a-b")


def test_add_resource_help_describes_defaults(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["add-resource", "--help"])
    assert exc.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    for expected in (
        "--recursive",
        "default: off",
        "default: on",
        "default: documents",
        "default: unlimited",
        "default: none",
        "--from FORMAT",
        "Explicit flags override settings",
    ):
        assert expected in help_text
    assert "--table-mode" not in help_text


def test_cli_folder_scanning_supports_native_documents(tmp_path, capsys):
    folder = tmp_path / "sources"
    nested = folder / "nested"
    nested.mkdir(parents=True)
    (folder / "first.dclg").write_text("<doclang><text>First</text></doclang>")
    (folder / "second.dclg.xml").write_text("<doclang><text>Second</text></doclang>")
    package = DocLangXDocument()
    assert package.read_xml("<doclang><text>Third</text></doclang>")
    (folder / "third.dclx").write_bytes(package.write_bytes())
    (nested / "fourth.dclg").write_text("<doclang><text>Fourth</text></doclang>")
    (folder / "ignored.bin").write_text("Ignored")
    base = ["--store", str(tmp_path / "store"), "add-resource", str(folder)]
    assert main(base) == 0
    top_level = {
        json.loads(line)["uri"] for line in capsys.readouterr().out.splitlines()
    }
    assert top_level == {
        "docling://resources/documents/first",
        "docling://resources/documents/second",
        "docling://resources/documents/third",
    }
    assert main(base + ["-r"]) == 0
    recursive = {
        json.loads(line)["uri"] for line in capsys.readouterr().out.splitlines()
    }
    assert recursive == top_level | {"docling://resources/documents/nested/fourth"}
    assert (
        main(
            [
                "--store",
                str(tmp_path / "store"),
                "add-resource",
                str(folder / "first.dclg"),
                "-r",
            ]
        )
        == 1
    )
    assert "requires a folder" in capsys.readouterr().err


def test_cli_from_filter_and_filename_collisions(tmp_path, capsys):
    folder = tmp_path / "sources"
    folder.mkdir()
    (folder / "same.dclg").write_text("<doclang><text>One</text></doclang>")
    document = DocLangXDocument()
    assert document.read_xml("<doclang><text>Two</text></doclang>")
    (folder / "same.dclx").write_bytes(document.write_bytes())
    (folder / "other.pdf").write_bytes(b"%PDF")
    base = ["--store", str(tmp_path / "store"), "add-resource", str(folder)]
    assert main(base + ["--from", "dclg", "--from", "dclx"]) == 0
    uris = {json.loads(line)["uri"] for line in capsys.readouterr().out.splitlines()}
    assert uris == {
        "docling://resources/documents/same.dclg",
        "docling://resources/documents/same.dclx",
    }
    assert main(base + ["--from", "unknown"]) == 1
    assert "unknown source format" in capsys.readouterr().err
    assert main(base + ["--from", "xml_jats"]) == 1
    assert "no documents matching" in capsys.readouterr().err


def test_cli_import_worker_inspection_and_lexical_lookup(tmp_path, capsys):
    source = tmp_path / "guide.dclg"
    source.write_text(
        "<doclang><heading>Guide</heading><text>Blue glaciers</text></doclang>"
    )
    store = tmp_path / "store"
    base = ["--store", str(store)]
    assert main(base + ["add-resource", str(source), "--collection", "manuals"]) == 0
    added = json.loads(capsys.readouterr().out)
    assert added["uri"] == "docling://resources/manuals/guide"
    assert added["task_id"]
    assert main(base + ["task", "status", added["task_id"]]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "queued"
    assert main(base + ["worker", "--once"]) == 0
    capsys.readouterr()
    assert main(base + ["task", "status", added["task_id"]]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "complete"
    assert main(base + ["ls", "docling://resources/"]) == 0
    assert "docling://resources/manuals" in capsys.readouterr().out
    assert main(base + ["tree", "docling://resources/manuals", "-L", "2"]) == 0
    assert "guide" in capsys.readouterr().out
    assert (
        main(base + ["find", "glaciers", "--uri", "docling://resources/manuals"]) == 0
    )
    assert "glaciers" in capsys.readouterr().out
    assert main(base + ["grep", "blue", "--uri", "docling://resources/manuals"]) == 0
    assert "Blue glaciers" in capsys.readouterr().out
    assert main(base + ["status"]) == 0
    assert json.loads(capsys.readouterr().out)["documents"] == 2


def test_pdf_config_changes_ingestion_fingerprint(tmp_path):
    class Converter:
        def __init__(self, ocr):
            self.ocr = ocr

        def fingerprint_options(self):
            return {"do_ocr": self.ocr}

        def convert(self, _source, _filename):
            document = DocLangXDocument()
            assert document.read_xml("<doclang><text>Body</text></doclang>")
            return ConversionResult(document.write_bytes(), "test", "1")

    principal = Principal("tenant", "user")
    uri = "docling://resources/manuals/one"
    with LocalContextStore(tmp_path) as store:
        first = Ingestor(store, converter=Converter(True)).add_resource(
            principal, uri, b"%PDF", filename="one.pdf"
        )
        repeated = Ingestor(store, converter=Converter(True)).add_resource(
            principal, uri, b"%PDF", filename="one.pdf"
        )
        changed = Ingestor(store, converter=Converter(False)).add_resource(
            principal, uri, b"%PDF", filename="one.pdf"
        )
        assert repeated.revision_id == first.revision_id
        assert changed.revision_id != first.revision_id
