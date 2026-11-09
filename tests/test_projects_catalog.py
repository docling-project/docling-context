"""End-to-end checks for the SQLite project catalog."""

from __future__ import annotations

import base64
import json

import pytest

from docling_context.catalog import Catalog
from docling_context.cli import main
from docling_context.doclang_authoring import DEFAULT_SKILL_URI
from docling_context.ingest_queue import IngestQueue
from docling_context.local import LocalContextStore
from docling_context.models import Principal
from docling_context.service import (
    ContextService,
    IngestRequest,
    OverviewRequest,
    SearchRequest,
)


@pytest.fixture
def catalog_scope(tmp_path):
    with LocalContextStore(tmp_path / "store") as store:
        yield Catalog(store), "test"


def test_import_link_overview_and_scoped_search(catalog_scope, tmp_path, capsys):
    catalog, tenant = catalog_scope
    store = tmp_path / "store"
    source = tmp_path / "source.dclg"
    source.write_text("<doclang><text>Project telescope evidence</text></doclang>")
    args = ["--store", str(store), "--tenant", tenant]
    principal = Principal(tenant, "local")
    catalog.create_project(principal, "alpha", "Alpha")
    catalog.create_project(principal, "beta", "Beta")

    assert (
        main(
            [
                *args,
                "library",
                "add",
                "--source",
                str(source),
                "--project",
                "alpha",
                "--json",
            ]
        )
        == 0
    )
    first = json.loads(capsys.readouterr().out)
    assert first["uri"].startswith("docling://resources/library/")
    assert (
        main(
            [
                *args,
                "library",
                "add",
                "--source",
                str(source),
                "--project",
                "beta",
                "--json",
            ]
        )
        == 0
    )
    second = json.loads(capsys.readouterr().out)
    assert second["uri"] == first["uri"]
    assert second["revision_id"] == first["revision_id"]

    overview = catalog.overview(principal)
    assert [
        (row["project_id"], row["documents"], row["skills"]) for row in overview
    ] == [("alpha", 1, 1), ("beta", 1, 1)]
    assert main([*args, "project", "show", "-p", "alpha", "-j"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["project_id"] == "alpha"
    assert {key: shown[key] for key in ("documents", "concepts", "skills")} == {
        "documents": 1,
        "concepts": 0,
        "skills": 1,
    }
    assert main([*args, "project", "show", "-p", "alpha"]) == 0
    table = capsys.readouterr().out
    assert "Documents" in table and "Concepts" in table and "Skills" in table
    assert DEFAULT_SKILL_URI in {
        row["uri"] for row in catalog.project_resources(principal, "alpha")
    }
    assert DEFAULT_SKILL_URI in {
        row["uri"] for row in catalog.project_resources(principal, "beta")
    }
    default_skill = catalog.store.get_document(principal, DEFAULT_SKILL_URI)
    assert default_skill.validate(allow_empty_namespace=True).ok()
    assert "validate_package" in default_skill.xml()
    with pytest.raises(ValueError, match="required by every project"):
        catalog.unlink(principal, "alpha", DEFAULT_SKILL_URI)
    with LocalContextStore(store) as local:
        service = ContextService(local, principal)
        assert service.overview(OverviewRequest()).data[0]["documents"] == 1
        found = service.search(
            SearchRequest("telescope", mode="lexical", project_id="alpha")
        ).data
        assert any(hit["uri"] == first["uri"] for hit in found["hits"])
    assert (
        main(
            [
                *args,
                "library",
                "search",
                "--query",
                "telescope",
                "--project",
                "alpha",
                "--mode",
                "lexical",
                "--json",
            ]
        )
        == 0
    )
    assert first["uri"] in capsys.readouterr().out

    skill_source = tmp_path / "skill.dclg"
    skill_source.write_text("<doclang><text>Method protocol</text></doclang>")
    assert (
        main(
            [
                *args,
                "skill",
                "create",
                "--id",
                "method",
                "--from",
                str(skill_source),
                "--project",
                "alpha",
                "--json",
            ]
        )
        == 0
    )
    skill = json.loads(capsys.readouterr().out)
    catalog.link_resources(principal, first["uri"], skill["uri"], "uses-method")
    assert (
        catalog.resource_links(principal, first["uri"])[0]["target_uri"] == skill["uri"]
    )
    assert catalog.overview(principal)[0]["skills"] == 2

    catalog.unlink(principal, "alpha", first["uri"])
    assert catalog.overview(principal)[0]["documents"] == 0
    assert catalog.find_source(principal, first["source_sha256"]) == first["uri"]
    assert (
        main(
            [
                *args,
                "library",
                "search",
                "--query",
                "telescope",
                "--project",
                "alpha",
                "--mode",
                "lexical",
                "--json",
            ]
        )
        == 0
    )
    assert capsys.readouterr().out == ""

    assert (
        main(
            [
                *args,
                "session",
                "start",
                "--project",
                "alpha",
                "--no-launch",
                "--agent",
                "codex",
                "--id",
                "chat-1",
                "--json",
            ]
        )
        == 0
    )
    session = json.loads(capsys.readouterr().out)
    assert session["uri"] == "docling://projects/alpha/sessions/chat-1"
    assert (
        main(
            [
                *args,
                "session",
                "append",
                "--id",
                "chat-1",
                "--text",
                "Remember: telescope evidence is reusable",
                "--kind",
                "turn",
                "--key",
                "turn-1",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert main([*args, "session", "close", "--id", "chat-1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "closed"
    assert catalog.overview(principal)[0]["sessions"] == 1
    assert main([*args, "memory", "worker", "--once", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "completed"
    assert catalog.overview(principal)[0]["memories"] == 1
    linked = catalog.project_resources(principal, "alpha")
    assert any(item["resource_type"] == "memories" for item in linked)

    with LocalContextStore(store) as local:
        service = ContextService(local, principal)
        payload = base64.b64encode(
            b"<doclang><text>Queued project finding</text></doclang>"
        ).decode()
        queued = service.ingest(
            IngestRequest("", "finding.dclg", payload, project_id="alpha")
        ).data
        assert queued["status"] == "queued"
        finished = IngestQueue(local).run_once()
        assert finished is not None and finished.status == "completed"
        assert finished.uri.startswith("docling://resources/library/")
        assert catalog.overview(principal)[0]["documents"] == 1


def test_existing_project_recovers_required_authoring_skill(tmp_path):
    root = tmp_path / "store"
    principal = Principal("team", "alice")
    with LocalContextStore(root) as store:
        catalog = Catalog(store)
        catalog.create_project(principal, "alpha", "Alpha")
        store.db.execute(
            """DELETE FROM dc_project_resource_links
               WHERE tenant_id=? AND project_id=? AND resource_type='skills'
                 AND resource_id='doclang-authoring'""",
            (principal.tenant_id, "alpha"),
        )
    with LocalContextStore(root) as store:
        assert DEFAULT_SKILL_URI in {
            row["uri"] for row in Catalog(store).project_resources(principal, "alpha")
        }


def test_typed_descriptors_and_cited_knowledge(tmp_path, capsys):
    root = tmp_path / "store"
    source = tmp_path / "source.dclg"
    source.write_text("<doclang><text>Basalt density is 3.0.</text></doclang>")
    descriptor = tmp_path / "descriptor.dclg"
    descriptor.write_text("<doclang><text>Material observations</text></doclang>")
    metadata = tmp_path / "knowledge.json"
    metadata.write_text(
        json.dumps(
            {
                "name": "Materials",
                "schema": {
                    "entity_types": ["material"],
                    "properties": {"density": {"type": "number"}},
                },
            }
        )
    )
    base = ["--store", str(root)]
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
    assert (
        main(
            [
                *base,
                "library",
                "add",
                "--source",
                str(source),
                "--project",
                "materials",
                "--json",
            ]
        )
        == 0
    )
    library = json.loads(capsys.readouterr().out)
    assert (
        main(
            [
                *base,
                "concept",
                "create",
                "--id",
                "material-properties",
                "--from",
                str(descriptor),
                "--metadata",
                str(metadata),
                "--project",
                "materials",
                "--json",
            ]
        )
        == 0
    )
    concept = json.loads(capsys.readouterr().out)
    assert (
        main(
            [
                *base,
                "knowledge",
                "create",
                "--id",
                "materials",
                "--concept",
                "material-properties",
                "--project",
                "materials",
                "--json",
            ]
        )
        == 0
    )
    knowledge = json.loads(capsys.readouterr().out)
    assert knowledge["concept_revision"] == concept["revision_id"]
    assert (
        main(
            [
                *base,
                "knowledge",
                "add-fact",
                "--uri",
                knowledge["uri"],
                "--entity-type",
                "material",
                "--entity-id",
                "basalt",
                "--property",
                "density",
                "--value",
                "3.0",
                "--source-uri",
                library["uri"],
                "--source-revision",
                library["revision_id"],
                "--source-xpath",
                "/doclang[1]/text[1]",
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["fact_id"]
    assert main([*base, "knowledge", "facts", "--uri", knowledge["uri"], "--json"]) == 0
    facts = json.loads(capsys.readouterr().out)
    assert facts[0]["value"] == 3.0
    assert facts[0]["source_uri"] == library["uri"]
    with LocalContextStore(root) as store:
        catalog = Catalog(store)
        principal = Principal("default", "local")
        citation = {
            "source_uri": library["uri"],
            "source_revision": library["revision_id"],
            "source_xpath": "/doclang[1]/text[1]",
        }
        for invalid in (
            {"entity_type": "mineral", "property": "density", "value": 3.0},
            {"entity_type": "material", "property": "porosity", "value": 3.0},
            {"entity_type": "material", "property": "density", "value": "3.0"},
        ):
            with pytest.raises(ValueError, match="schema"):
                catalog.add_fact(
                    principal,
                    knowledge["uri"],
                    entity_id="basalt",
                    **citation,
                    **invalid,
                )
    assert main([*base, "overview", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["projects"][0]["knowledge"] == 1


def test_concept_schema_required_and_dataset_revision_is_pinned(tmp_path, capsys):
    root = tmp_path / "store"
    descriptor = tmp_path / "concept.dclg"
    descriptor.write_text("<doclang><text>Formation measurements</text></doclang>")
    metadata = tmp_path / "concept.json"
    base = ["--store", str(root)]

    assert (
        main(
            [*base, "concept", "create", "--id", "formation", "--from", str(descriptor)]
        )
        == 1
    )
    assert "schema" in capsys.readouterr().err
    metadata.write_text(
        json.dumps(
            {
                "schema": {
                    "entity_types": ["formation"],
                    "properties": {"porosity": {"type": "number", "unit": "%"}},
                }
            }
        )
    )
    create = [
        *base,
        "concept",
        "create",
        "--id",
        "formation",
        "--from",
        str(descriptor),
        "--metadata",
        str(metadata),
        "--json",
    ]
    assert main(create) == 0
    first_revision = json.loads(capsys.readouterr().out)["revision_id"]
    assert (
        main(
            [
                *base,
                "knowledge",
                "create",
                "--id",
                "measurements",
                "--concept",
                "formation",
                "--json",
            ]
        )
        == 0
    )
    dataset = json.loads(capsys.readouterr().out)
    assert dataset["concept_revision"] == first_revision

    metadata.write_text(
        json.dumps(
            {
                "schema": {
                    "entity_types": ["formation"],
                    "properties": {"porosity": {"type": "integer"}},
                }
            }
        )
    )
    assert main(create) == 0
    assert json.loads(capsys.readouterr().out)["revision_id"] != first_revision
    assert main([*base, "knowledge", "show", "--uri", "measurements", "--json"]) == 0
    pinned = json.loads(capsys.readouterr().out)
    assert pinned["concept_revision"] == first_revision
    assert pinned["schema"]["properties"]["porosity"] == {"type": "number", "unit": "%"}


def test_library_relationship_commands(tmp_path, capsys):
    root = tmp_path / "store"
    source = tmp_path / "source.dclg"
    source.write_text("<doclang><text>Shared document</text></doclang>")
    base = ["--store", str(root)]
    assert (
        main([*base, "project", "create", "--project", "alpha", "--title", "Alpha"])
        == 0
    )
    capsys.readouterr()
    assert (
        main(
            [
                *base,
                "library",
                "add",
                "--source",
                str(source),
                "--project",
                "alpha",
                "--json",
            ]
        )
        == 0
    )
    resource = json.loads(capsys.readouterr().out)
    document_id = resource["uri"].rsplit("/", 1)[-1]
    assert (
        main([*base, "library", "projects", "--document", document_id, "--json"]) == 0
    )
    assert json.loads(capsys.readouterr().out)[0]["project_id"] == "alpha"
    assert (
        main(
            [
                *base,
                "library",
                "link",
                "--document",
                document_id,
                "--target",
                resource["uri"],
                "--relation",
                "related",
                "--json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert main([*base, "library", "links", "--document", document_id, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["relation"] == "related"


def test_project_context_jobs_refresh_after_membership_change(tmp_path, capsys):
    root = tmp_path / "store"
    source = tmp_path / "source.dclg"
    source.write_text("<doclang><text>Basalt field notes</text></doclang>")
    base = ["--store", str(root)]
    assert (
        main([*base, "project", "create", "--project", "geology", "--title", "Geology"])
        == 0
    )
    capsys.readouterr()
    for _ in range(2):
        assert main([*base, "project", "context", "worker", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["status"] == "completed"
    with LocalContextStore(root) as store:
        project = Catalog(store).get_project(Principal("default", "local"), "geology")
        assert project.abstract_uri and project.overview_uri
        before = store.get_record(Principal("default", "local"), project.overview_uri)
    assert (
        main([*base, "library", "add", "--source", str(source), "--project", "geology"])
        == 0
    )
    capsys.readouterr()
    assert (
        main([*base, "project", "context", "jobs", "--project", "geology", "--json"])
        == 0
    )
    assert {job["status"] for job in json.loads(capsys.readouterr().out)} == {"queued"}
    for _ in range(2):
        assert main([*base, "project", "context", "worker", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["status"] == "completed"
    with LocalContextStore(root) as store:
        principal = Principal("default", "local")
        project = Catalog(store).get_project(principal, "geology")
        assert project.overview_uri is not None
        after = store.get_record(principal, project.overview_uri)
        assert after.revision_id != before.revision_id
        assert "library" in store.get_document(principal, project.overview_uri).xml()
