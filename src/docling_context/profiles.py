"""Portable DCLX profiles for agent sessions and reviewed memories."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from typing import Any

from doclang import DocLangXDocument

from .package import bounded_nodes
from .uri import parse_uri

SESSION_PART = "context/session.json"
MEMORY_PART = "context/memory.json"
DESCRIPTOR_PARTS = {
    "skills": "context/skill.json",
    "concepts": "context/concept.json",
    "knowledge": "context/knowledge.json",
}
DESCRIPTOR_NAMES = {"skills": "skill", "concepts": "concept", "knowledge": "knowledge"}
SESSION_KINDS = frozenset({"turn", "tool_call", "tool_result", "feedback"})
SESSION_STATUSES = frozenset({"open", "closed"})
MEMORY_KINDS = frozenset({"preference", "fact", "entity", "concept", "procedure"})
MEMORY_STATUSES = frozenset(
    {"proposed", "accepted", "rejected", "superseded", "deleted"}
)
MAX_SESSION_EVENTS = 1_000
MAX_EVENT_CHARS = 20_000
MAX_ATTACHMENT_BYTES = 4 * 1024 * 1024
MAX_SESSION_ATTACHMENT_BYTES = 32 * 1024 * 1024


def profile_kind(uri: str) -> str | None:
    address = parse_uri(uri)
    if (
        address.namespace == "projects"
        and len(address.segments) == 3
        and address.segments[1] == "sessions"
    ):
        return "sessions"
    if address.namespace == "resources" and address.segments[0] == "memories":
        return "memories"
    if address.namespace != "users":
        return None
    if len(address.segments) == 4 and address.segments[2] == "sessions":
        return "sessions"
    if len(address.segments) == 5 and address.segments[2] == "memories":
        return "memories"
    return None


def _part(document: DocLangXDocument, path: str) -> dict[str, Any]:
    raw = document.get_part_text(path)
    if raw is None or len(raw) > 2_000_000:
        raise ValueError(f"missing or oversized {path}")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid {path}") from exc
    if not isinstance(value, dict):
        raise TypeError(f"invalid {path}")
    return value


def validate_session(document: DocLangXDocument) -> dict[str, Any]:
    metadata = _part(document, SESSION_PART)
    if metadata.get("profile") != "session-v1":
        raise ValueError("unsupported session profile")
    events = metadata.get("events")
    if not isinstance(events, list) or len(events) > MAX_SESSION_EVENTS:
        raise ValueError("session needs a bounded event list")
    if metadata.get("status", "open") not in SESSION_STATUSES:
        raise ValueError("invalid session status")
    for name in ("created_at", "updated_at", "closed_at"):
        if metadata.get(name) is not None:
            try:
                datetime.fromisoformat(metadata[name])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid session {name}") from exc
    if metadata.get("status") == "closed" and not metadata.get("closed_at"):
        raise ValueError("closed session needs closed_at")
    text_nodes = [
        node
        for node in bounded_nodes(document, limit=5_000, text_bytes=MAX_EVENT_CHARS * 4)
        if node["name"] == "text"
    ]
    if len(text_nodes) != len(events):
        raise ValueError("session event and DocLang counts differ")
    keys: set[str] = set()
    attachment_bytes = 0
    for number, (event, node) in enumerate(zip(events, text_nodes, strict=True), 1):
        if not isinstance(event, dict):
            raise TypeError("invalid session event")
        key = event.get("key")
        if (
            not isinstance(key, str)
            or not 1 <= len(key) <= 128
            or key in keys
            or event.get("kind") not in SESSION_KINDS
            or event.get("xpath") != f"/doclang[1]/text[{number}]"
            or not isinstance(event.get("turn_id"), str)
            or not isinstance(event.get("created_at"), str)
            or node["xpath"] != event["xpath"]
            or len(node["text"]) > MAX_EVENT_CHARS
            or node["truncated"]
        ):
            raise ValueError("invalid session event metadata")
        keys.add(key)
        attachment = event.get("attachment")
        if attachment is not None:
            if not isinstance(attachment, dict):
                raise ValueError("invalid session attachment")
            path = attachment.get("path")
            if (
                not isinstance(path, str)
                or not path.startswith("attachments/")
                or not isinstance(attachment.get("content_type"), str)
            ):
                raise ValueError("invalid session attachment path or type")
            payload = document.get_part_bytes(path)
            if (
                payload is None
                or len(payload) > MAX_ATTACHMENT_BYTES
                or attachment.get("size") != len(payload)
                or attachment.get("sha256") != hashlib.sha256(payload).hexdigest()
            ):
                raise ValueError("session attachment is missing or changed")
            attachment_bytes += len(payload)
            if attachment_bytes > MAX_SESSION_ATTACHMENT_BYTES:
                raise ValueError("session attachments exceed the total limit")
    return metadata


def validate_memory(document: DocLangXDocument) -> dict[str, Any]:
    metadata = _part(document, MEMORY_PART)
    if metadata.get("profile") != "memory-v1":
        raise ValueError("unsupported memory profile")
    confidence = metadata.get("confidence")
    if (
        metadata.get("kind") not in MEMORY_KINDS
        or metadata.get("status") not in MEMORY_STATUSES
        or not isinstance(metadata.get("memory_id"), str)
        or not isinstance(metadata.get("claim"), str)
        or not metadata["claim"].strip()
        or len(metadata["claim"]) > 4_000
        or not isinstance(metadata.get("explanation"), str)
        or len(metadata["explanation"]) > 20_000
        or not isinstance(metadata.get("author"), str)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(confidence)
        or not 0 <= confidence <= 1
    ):
        raise ValueError("invalid memory metadata")
    text_nodes = [
        node
        for node in bounded_nodes(document, limit=20, text_bytes=20_000)
        if node["name"] == "text"
    ]
    if not text_nodes or text_nodes[0]["text"] != metadata["claim"]:
        raise ValueError("memory claim differs from DocLang body")
    citations = metadata.get("citations")
    if not isinstance(citations, list) or len(citations) > 100:
        raise ValueError("invalid memory citations")
    aliases = metadata.get("claim_aliases", [])
    if (
        not isinstance(aliases, list)
        or len(aliases) > 100
        or any(not isinstance(alias, str) or len(alias) != 64 for alias in aliases)
    ):
        raise ValueError("invalid memory claim aliases")
    for citation in citations:
        if not isinstance(citation, dict) or any(
            not isinstance(citation.get(name), str) or not citation[name]
            for name in ("uri", "document_id", "revision_id", "xpath")
        ):
            raise ValueError("invalid memory citation")
        if not citation["xpath"].startswith("/doclang[1]"):
            raise ValueError("memory citations need DocLang XPaths")
        parse_uri(citation["uri"])
    for name in ("valid_from", "valid_to"):
        if metadata.get(name) is not None:
            try:
                datetime.fromisoformat(metadata[name])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid {name}") from exc
    if (
        metadata.get("valid_from")
        and metadata.get("valid_to")
        and metadata["valid_from"] > metadata["valid_to"]
    ):
        raise ValueError("memory validity is reversed")
    return metadata


def validate_descriptor(uri: str, document: DocLangXDocument) -> dict[str, Any]:
    address = parse_uri(uri)
    if address.namespace != "resources" or address.segments[0] not in DESCRIPTOR_PARTS:
        raise ValueError("expected a skill, concept, or knowledge URI")
    resource_type, resource_id = address.segments
    metadata = _part(document, DESCRIPTOR_PARTS[resource_type])
    singular = DESCRIPTOR_NAMES[resource_type]
    if (
        metadata.get("profile") != f"{singular}-v1"
        or metadata.get(f"{singular}_id") != resource_id
        or not isinstance(metadata.get("name"), str)
        or not 1 <= len(metadata["name"].strip()) <= 200
        or not isinstance(metadata.get("summary"), str)
        or len(metadata["summary"]) > 2_000
    ):
        raise ValueError(f"invalid {singular} metadata")
    if resource_type == "concepts":
        for field in ("entity_types", "relationship_types", "properties"):
            value = metadata.get(field)
            if not isinstance(value, list) or any(
                not isinstance(item, str) or not item.strip() or len(item) > 100
                for item in value
            ):
                raise ValueError(f"invalid concept {field}")
    if resource_type == "knowledge":
        fields = metadata.get("fields")
        if not isinstance(fields, dict) or any(
            not isinstance(name, str)
            or not name.strip()
            or kind not in {"text", "integer", "number", "boolean", "json"}
            for name, kind in fields.items()
        ):
            raise ValueError("invalid knowledge fields")
    return metadata


def validate_profile(uri: str, document: DocLangXDocument) -> dict[str, Any] | None:
    kind = profile_kind(uri)
    address = parse_uri(uri)
    if kind == "sessions":
        return validate_session(document)
    if kind == "memories":
        metadata = validate_memory(document)
        expected_id = (
            address.segments[1]
            if address.namespace == "resources"
            else address.segments[4]
        )
        if metadata["memory_id"] != expected_id or (
            address.namespace == "users" and metadata["kind"] != address.segments[3]
        ):
            raise ValueError("memory package metadata differs from URI")
        return metadata
    if address.namespace == "resources" and address.segments[0] in DESCRIPTOR_PARTS:
        return validate_descriptor(uri, document)
    return None
