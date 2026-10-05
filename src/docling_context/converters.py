"""Opt-in conversion adapters that return portable DocLang packages."""

from __future__ import annotations

import io
import tempfile
import uuid
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zipfile import ZipFile

from .package import MAX_PACKAGE_BYTES, load_package


@dataclass(frozen=True, slots=True)
class ConversionResult:
    package: bytes
    converter: str
    version: str
    options: dict[str, object] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()


class Converter(Protocol):
    def convert(self, source: bytes, filename: str) -> ConversionResult: ...


class LocalDoclingConverter:
    """Use an installed Docling distribution without network input URLs."""

    def __init__(self, *, max_pages: int = 100, max_file_bytes: int = 50_000_000):
        if max_pages < 1 or max_file_bytes < 1:
            raise ValueError("conversion limits must be positive")
        self.max_pages = max_pages
        self.max_file_bytes = max_file_bytes

    def convert(self, source: bytes, filename: str) -> ConversionResult:
        if Path(filename).suffix.lower() != ".pdf":
            raise ValueError("local conversion currently accepts PDF files")
        if len(source) > self.max_file_bytes:
            raise ValueError("source exceeds converter size limit")
        try:
            from docling.document_converter import (
                DocumentConverter,  # type: ignore[import-not-found]
            )
        except ImportError as exc:
            raise RuntimeError(
                "install docling-context[conversion] for PDF conversion"
            ) from exc
        try:
            converter_version = version("docling")
        except PackageNotFoundError:
            converter_version = "unknown"
        with tempfile.TemporaryDirectory(prefix="context-convert-") as work:
            root = Path(work)
            input_path = root / "input.pdf"
            output_path = root / "result.dclx"
            input_path.write_bytes(source)
            result = DocumentConverter().convert(
                input_path,
                max_num_pages=self.max_pages,
                max_file_size=self.max_file_bytes,
            )
            result.document.save_as_doclang_archive(output_path)
            package = output_path.read_bytes()
        load_package(package)
        return ConversionResult(
            package,
            "docling-local",
            converter_version,
            {"max_pages": self.max_pages, "max_file_bytes": self.max_file_bytes},
            tuple(str(item)[:500] for item in result.errors[:100]),
        )


class DoclingServeConverter:
    """Explicit service endpoint; source bytes are uploaded, never fetched."""

    def __init__(
        self,
        endpoint: str,
        *,
        timeout: float = 120,
        max_response_bytes: int = MAX_PACKAGE_BYTES,
    ):
        parsed = urlparse(endpoint)
        loopback_http = parsed.scheme == "http" and parsed.hostname in {
            "localhost",
            "127.0.0.1",
            "::1",
        }
        if (
            not (parsed.scheme == "https" or loopback_http)
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "conversion endpoint must use HTTPS or loopback HTTP, without credentials"
            )
        if timeout <= 0 or max_response_bytes < 1:
            raise ValueError("invalid remote conversion limits")
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes

    def convert(self, source: bytes, filename: str) -> ConversionResult:
        if len(source) > 50_000_000:
            raise ValueError("source exceeds remote conversion size limit")
        boundary = uuid.uuid4().hex
        safe_name = Path(filename).name.replace('"', "_")
        body = (
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{safe_name}"\r\n'
                "Content-Type: application/octet-stream\r\n\r\n"
            ).encode()
            + source
            + (
                f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="to_formats"\r\n\r\ndclx'
                f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="target_type"\r\n\r\nzip'
                f"\r\n--{boundary}--\r\n"
            ).encode()
        )
        request = Request(
            f"{self.endpoint}/v1/convert/file",
            data=body,
            method="POST",
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Accept": "application/zip",
            },
        )
        with build_opener(_NoRedirect()).open(
            request, timeout=self.timeout
        ) as response:
            payload = response.read(self.max_response_bytes + 1)
        if len(payload) > self.max_response_bytes:
            raise ValueError("remote conversion response is too large")
        if payload.startswith(b"PK"):
            try:
                load_package(payload)
                package = payload
            except ValueError:
                with ZipFile(io.BytesIO(payload)) as archive:
                    names = [
                        name for name in archive.namelist() if name.endswith(".dclx")
                    ]
                    if len(names) != 1:
                        raise ValueError("remote response must contain one DCLX result")
                    if archive.getinfo(names[0]).file_size > MAX_PACKAGE_BYTES:
                        raise ValueError("remote package is too large")
                    package = archive.read(names[0])
        else:
            raise ValueError("remote converter did not return a ZIP package")
        load_package(package)
        return ConversionResult(
            package, "docling-serve", "v1", {"endpoint": self.endpoint}
        )


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise ValueError("remote converter redirected outside its configured endpoint")
