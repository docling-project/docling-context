"""Configuration and command-line workflows."""

from __future__ import annotations

import json
import logging
import warnings

import pytest
from doclang import DocLangXDocument

from docling_context import (
    ConversionResult,
    Ingestor,
    LocalContextStore,
    PdfConversionConfig,
    Principal,
)
from docling_context import cli as cli_module
from docling_context.catalog import Catalog
from docling_context.cli import main


@pytest.fixture
def project_catalog(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCLING_CONTEXT_STORE", str(tmp_path / "store"))
    with LocalContextStore(tmp_path / "store") as store:
        yield Catalog(store)


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


def test_add_resource_help_describes_defaults(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["library", "add", "--help"])
    assert exc.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    for expected in (
        "--recursive",
        "default: off",
        "default: on",
        "--project PROJECT",
        "default: unlimited",
        "default: none",
        "--from FORMAT",
        "Explicit flags override settings",
    ):
        assert expected in help_text
    assert "--table-mode" not in help_text


def test_project_help_explains_subcommands(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["project", "--help"])
    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    assert "Create a project" in help_text
    assert "List linked resources" in help_text
    assert "Read and generate project context" in help_text
    assert "session-start" not in help_text


def test_bare_cli_shows_starting_points_and_legacy_route_is_absent(tmp_path, capsys):
    assert main(["--store", str(tmp_path / "store")]) == 0
    output = capsys.readouterr().out
    assert "Create a project:" in output
    assert "dc library add" in output
    assert "dc session start" in output
    with pytest.raises(SystemExit) as exc:
        main(["--store", str(tmp_path / "store"), "add-resource", "paper.pdf"])
    assert exc.value.code == 2


def test_init_creates_local_catalog(tmp_path, capsys):
    root = tmp_path / "fresh"
    assert main(["--store", str(root), "init", "--json"]) == 0
    assert (root / "context.sqlite3").is_file()
    assert json.loads(capsys.readouterr().out)["catalog_schema_version"] == 1
    assert main(["--store", str(root), "overview"]) == 0
    assert capsys.readouterr().out.startswith("No records.\n")


def test_cli_folder_scanning_supports_native_documents(
    tmp_path, capsys, project_catalog
):
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
    base = [
        "--store",
        str(tmp_path / "store"),
        "library",
        "add",
        "--source",
        str(folder),
        "--json",
    ]
    assert main(base) == 0
    top_level = {
        json.loads(line)["uri"] for line in capsys.readouterr().out.splitlines()
    }
    assert len(top_level) == 3
    assert all(uri.startswith("docling://resources/library/") for uri in top_level)
    assert main(base + ["-r"]) == 0
    recursive = {
        json.loads(line)["uri"] for line in capsys.readouterr().out.splitlines()
    }
    assert top_level < recursive and len(recursive) == 4
    assert (
        main(
            [
                "--store",
                str(tmp_path / "store"),
                "library",
                "add",
                "--source",
                str(folder / "first.dclg"),
                "-r",
            ]
        )
        == 1
    )
    assert "requires a folder" in capsys.readouterr().err


def test_add_resource_resume_reports_existing_documents_and_suppresses_warnings(
    tmp_path, capsys, monkeypatch, project_catalog
):
    folder = tmp_path / "sources"
    folder.mkdir()
    (folder / "first.dclg").write_text("<doclang><text>First</text></doclang>")
    (folder / "second.dclg").write_text("<doclang><text>Second</text></doclang>")
    command = [
        "--store",
        str(tmp_path / "store"),
        "library",
        "add",
        "--source",
        str(folder),
    ]

    progress: list[object] = []

    class ProgressSpy:
        def __init__(self, *args, total, desc, initial=0, **kwargs):
            self.total = total
            self.desc = desc
            self.n = initial
            self.initial = initial
            self.postfixes: list[str] = []
            progress.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def update(self, count):
            self.n += count

        def set_postfix_str(self, value):
            self.postfixes.append(value)

    monkeypatch.setattr(cli_module, "tqdm", ProgressSpy)
    monkeypatch.setattr(
        cli_module,
        "_source_page_units",
        lambda source, filename: 3 if source.name == "first.dclg" else 2,
    )
    original_add_resource = Ingestor.add_resource
    calls = 0

    def interrupt_second(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt
        warnings.warn("conversion warning", UserWarning, stacklevel=1)
        logging.getLogger(__name__).warning("conversion log warning")
        return original_add_resource(self, *args, **kwargs)

    monkeypatch.setattr(Ingestor, "add_resource", interrupt_second)
    assert main(command) == 130
    interrupted = capsys.readouterr()
    assert "3 of 5 pages" in interrupted.err
    assert "conversion warning" not in interrupted.err
    assert "conversion log warning" not in interrupted.err

    positions_during_ingestion: list[int] = []

    def observe_progress(self, *args, **kwargs):
        positions_during_ingestion.append(progress[-1].n)
        return original_add_resource(self, *args, **kwargs)

    monkeypatch.setattr(Ingestor, "add_resource", observe_progress)
    assert main(command) == 0
    capsys.readouterr()
    resumed = [item for item in progress if item.desc == "Importing pages"][-1]
    assert resumed.total == 5
    assert resumed.initial == 0
    assert resumed.n == 5
    assert positions_during_ingestion == [0, 3]
    assert resumed.postfixes[0] == "existing=1 docs, added=0 docs"
    assert resumed.postfixes[-1] == "existing=1 docs, added=1 docs"

    positions_during_ingestion.clear()
    assert main(command + ["--json"]) == 0
    output = capsys.readouterr()
    assert len([json.loads(line) for line in output.out.splitlines()]) == 2
    assert "Importing pages" not in output.out
    assert positions_during_ingestion == [0, 3]
    assert progress[-1].n == 5


def test_pdf_page_count_uses_pdf_pages_and_one_unit_for_other_sources(tmp_path):
    pdfium = pytest.importorskip("pypdfium2")
    pdf_path = tmp_path / "three-pages.pdf"
    pdf = pdfium.PdfDocument.new()
    for _ in range(3):
        pdf.new_page(width=612, height=792)
    pdf.save(pdf_path)
    pdf.close()

    assert cli_module._source_page_units(pdf_path, None) == 3
    assert cli_module._source_page_units(pdf_path.read_bytes(), pdf_path.name) == 3
    assert cli_module._source_page_units(tmp_path / "notes.dclg", None) == 1


def test_cli_from_filter_and_filename_collisions(tmp_path, capsys, project_catalog):
    folder = tmp_path / "sources"
    folder.mkdir()
    (folder / "same.dclg").write_text("<doclang><text>One</text></doclang>")
    document = DocLangXDocument()
    assert document.read_xml("<doclang><text>Two</text></doclang>")
    (folder / "same.dclx").write_bytes(document.write_bytes())
    (folder / "other.pdf").write_bytes(b"%PDF")
    base = [
        "--store",
        str(tmp_path / "store"),
        "library",
        "add",
        "--source",
        str(folder),
        "--json",
    ]
    assert main(base + ["--from", "dclg", "--from", "dclx"]) == 0
    uris = {json.loads(line)["uri"] for line in capsys.readouterr().out.splitlines()}
    assert len(uris) == 2
    assert all(uri.startswith("docling://resources/library/") for uri in uris)
    assert main(base + ["--from", "unknown"]) == 1
    assert "unknown source format" in capsys.readouterr().err
    (folder / "other.pdf").unlink()
    assert main(base + ["--from", "pdf"]) == 1
    assert "no documents matching" in capsys.readouterr().err


def test_cli_import_worker_inspection_and_lexical_lookup(
    tmp_path, capsys, project_catalog
):
    source = tmp_path / "guide.dclg"
    source.write_text(
        "<doclang><heading>Guide</heading><text>Blue glaciers</text></doclang>"
    )
    store = tmp_path / "store"
    base = ["--store", str(store)]
    assert main(base + ["overview"]) == 0
    assert capsys.readouterr().out.startswith("No records.\n")
    assert (
        main(base + ["project", "create", "--project", "manuals", "--title", "Manuals"])
        == 0
    )
    project_table = capsys.readouterr().out
    assert "Project id" in project_table and "Manuals" in project_table
    assert (
        main(base + ["library", "add", "--source", str(source), "--project", "manuals"])
        == 0
    )
    import_table = capsys.readouterr().out
    assert "Uri" in import_table and "Revision Id" in import_table
    assert (
        main(
            base
            + [
                "library",
                "add",
                "--source",
                str(source),
                "--project",
                "manuals",
                "--json",
            ]
        )
        == 0
    )
    added = json.loads(capsys.readouterr().out)
    assert added["uri"].startswith("docling://resources/library/")
    assert added["uri"] in import_table
    assert main(base + ["overview"]) == 0
    overview_table = capsys.readouterr().out
    assert "Documents" in overview_table and "Manuals" in overview_table
    assert main(base + ["overview", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["projects"][0]["documents"] == 1
    assert main(base + ["library", "list"]) == 0
    assert "docling://resources/library" in capsys.readouterr().out
    assert (
        main(
            base
            + ["library", "tree", "--uri", "docling://resources/library", "-L", "2"]
        )
        == 0
    )
    assert added["uri"] in capsys.readouterr().out
    assert (
        main(
            base
            + [
                "library",
                "find",
                "--term",
                "glaciers",
                "--uri",
                "docling://resources/library",
            ]
        )
        == 0
    )
    assert "glaciers" in capsys.readouterr().out
    assert (
        main(
            base
            + [
                "library",
                "grep",
                "--term",
                "blue",
                "--uri",
                "docling://resources/library",
            ]
        )
        == 0
    )
    assert "Blue glaciers" in capsys.readouterr().out
    assert main(base + ["overview"]) == 0
    store_table = capsys.readouterr().out
    assert "Field" in store_table and "Documents" in store_table
    assert main(base + ["overview", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["counts"]["documents"] == 2


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


def test_folder_import_skips_failing_documents_and_reports_them(
    tmp_path, capsys, project_catalog
):
    folder = tmp_path / "sources"
    folder.mkdir()
    (folder / "a_broken.dclg").write_text("<doclang><text>unclosed</doclang>")
    (folder / "b_good.dclg").write_text("<doclang><text>Good</text></doclang>")
    base = [
        "--store",
        str(tmp_path / "store"),
        "library",
        "add",
        "--source",
        str(folder),
    ]

    assert main(base + ["--json"]) == 1
    captured = capsys.readouterr()
    rows = [json.loads(line) for line in captured.out.splitlines()]
    imported = [row for row in rows if "uri" in row]
    failed = [row for row in rows if row.get("status") == "failed"]
    assert len(imported) == 1
    assert [row["source"] for row in failed] == ["a_broken.dclg"]
    assert "1 of 2 documents failed" in captured.err

    assert main(base + ["--json", "--fail-fast"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("dc: ")


def test_cli_has_no_positional_arguments():
    """Every value is passed by name; only subcommand names are positional."""
    import argparse

    def positionals(parser, path):
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, sub in action.choices.items():
                    yield from positionals(sub, [*path, name])
            elif not action.option_strings:
                yield f"dc {' '.join(path)} <{action.dest}>"

    assert list(positionals(cli_module._parser(), [])) == []
