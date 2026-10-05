"""Canonical context URI parsing and access rules."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import unquote

from .models import Principal

_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_ENCODED_SEPARATOR = re.compile(r"%(?:2f|5c)", re.IGNORECASE)


class InvalidURI(ValueError):
    """A context URI does not meet the canonical grammar."""


class AccessDenied(PermissionError):
    """The principal cannot access this URI."""


@dataclass(frozen=True, slots=True)
class ContextURI:
    namespace: str
    segments: tuple[str, ...]

    @property
    def value(self) -> str:
        suffix = f"/{'/'.join(self.segments)}" if self.segments else ""
        return f"docling://{self.namespace}{suffix}"

    @property
    def parent(self) -> str | None:
        if len(self.segments) < 2:
            return None
        return f"docling://{self.namespace}/{'/'.join(self.segments[:-1])}"


def parse_uri(value: str, *, prefix: bool = False) -> ContextURI:
    """Parse one URI without allowing alternate spellings of the same path."""
    if not isinstance(value, str) or not value.startswith("docling://"):
        raise InvalidURI("expected a docling:// URI")
    if (
        any(c in value for c in "?#\\")
        or _ESCAPE.search(value)
        or _ENCODED_SEPARATOR.search(value)
    ):
        raise InvalidURI("URI contains a query, fragment, backslash, or invalid escape")
    rest = value[len("docling://") :]
    namespace, separator, path = rest.partition("/")
    if namespace not in {"resources", "users"} or (
        not separator and not (prefix and namespace == "resources")
    ):
        raise InvalidURI("URI namespace must be resources or users")
    raw_segments = path.split("/") if separator else []
    segments = []
    for raw in raw_segments:
        try:
            segment = unicodedata.normalize(
                "NFC", unquote(raw, encoding="utf-8", errors="strict")
            )
        except UnicodeDecodeError as exc:
            raise InvalidURI("URI contains invalid UTF-8") from exc
        if (
            not segment
            or segment in {".", ".."}
            or any(c in segment for c in "/\\%?#\x00")
        ):
            raise InvalidURI("URI has an empty, unsafe, or ambiguous segment")
        if any(ord(c) < 32 or ord(c) == 127 for c in segment):
            raise InvalidURI("URI contains a control character")
        segments.append(segment)
    minimum = (
        (0 if namespace == "resources" else 2)
        if prefix
        else (2 if namespace == "resources" else 4)
    )
    if len(segments) < minimum:
        raise InvalidURI("URI is too short for its namespace")
    if (
        namespace == "users"
        and len(segments) >= 3
        and segments[2] not in {"memories", "sessions", "skills"}
    ):
        raise InvalidURI("user URI must contain memories, sessions, or skills")
    return ContextURI(namespace, tuple(segments))


def authorize_uri(principal: Principal, uri: ContextURI) -> None:
    """Check namespace-level access; repository reads also filter by tenant owner."""
    if uri.namespace == "users":
        if uri.segments[0] != principal.tenant_id:
            raise AccessDenied("tenant does not match")
        if len(uri.segments) < 2 or uri.segments[1] != principal.user_id:
            raise AccessDenied("user does not match")
