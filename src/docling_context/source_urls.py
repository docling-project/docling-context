"""Bounded downloads for explicitly supplied document URLs."""

from __future__ import annotations

from email.message import Message
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx

from .package import MAX_PACKAGE_BYTES

_CONTENT_EXTENSIONS = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/msword": ".doc",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.ms-excel": ".xls",
    "text/html": ".html",
    "text/plain": ".txt",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/tiff": ".tiff",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
}


def is_source_url(value: str) -> bool:
    """Distinguish HTTP(S) input from a local path and reject other URL schemes."""
    if "://" not in value:
        return False
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("source URL must use HTTP or HTTPS")
    if parsed.username or parsed.password:
        raise ValueError("source URL must not contain credentials")
    return True


def _filename(url: str, headers: httpx.Headers, data: bytes) -> str:
    disposition = Message()
    disposition["Content-Disposition"] = headers.get("content-disposition", "")
    supplied = disposition.get_filename()
    path_name = Path(unquote(urlsplit(url).path).rstrip("/")).name
    name = Path((supplied or path_name or "download").replace("\\", "/")).name
    name = "".join(char for char in name if char.isprintable()) or "download"
    if data.startswith(b"%PDF-"):
        return name if name.lower().endswith(".pdf") else name + ".pdf"
    content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
    extension = _CONTENT_EXTENSIONS.get(content_type)
    if extension and not name.lower().endswith(extension):
        # URLs such as arxiv.org/pdf/2608.23083 have an ID, not a file suffix.
        name += extension
    return name


def download_source(
    url: str,
    *,
    max_bytes: int = MAX_PACKAGE_BYTES,
    timeout: float = 30.0,
    transport: httpx.BaseTransport | None = None,
) -> tuple[bytes, str]:
    """Return bounded source bytes and an inferred filename for conversion."""
    if not is_source_url(url):
        raise ValueError("expected an HTTP or HTTPS source URL")
    if max_bytes < 1 or timeout <= 0:
        raise ValueError("download limits must be positive")
    try:
        with (
            httpx.Client(
                follow_redirects=True,
                max_redirects=5,
                timeout=timeout,
                transport=transport,
            ) as client,
            client.stream("GET", url) as response,
        ):
            response.raise_for_status()
            length = response.headers.get("content-length", "")
            if length.isdecimal() and int(length) > max_bytes:
                raise ValueError("download exceeds the source size limit")
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_bytes(chunk_size=65_536):
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError("download exceeds the source size limit")
                chunks.append(chunk)
            data = b"".join(chunks)
            if not data:
                raise ValueError("source URL returned an empty document")
            return data, _filename(str(response.url), response.headers, data)
    except httpx.HTTPError as exc:
        raise ValueError(f"cannot download source URL: {exc}") from exc
