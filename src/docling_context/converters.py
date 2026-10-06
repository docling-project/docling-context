"""Opt-in conversion adapters that return portable DocLang packages."""

from __future__ import annotations

import io
import re
import sys
import tempfile
import uuid
from dataclasses import dataclass, field, replace
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zipfile import ZipFile

from .conversion_config import PdfConversionConfig
from .package import MAX_PACKAGE_BYTES, load_package

if TYPE_CHECKING:
    from docling.document_converter import DocumentConverter


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

    def __init__(
        self,
        config: PdfConversionConfig | None = None,
        *,
        max_pages: int | None = None,
        max_file_bytes: int | None = None,
    ):
        selected = config or PdfConversionConfig()
        if max_pages is not None or max_file_bytes is not None:
            selected = replace(
                selected,
                max_pages=selected.max_pages if max_pages is None else max_pages,
                max_file_bytes=selected.max_file_bytes
                if max_file_bytes is None
                else max_file_bytes,
            )
        self.config = selected
        self.max_pages = selected.max_pages
        self.max_file_bytes = selected.max_file_bytes
        self._document_converter: DocumentConverter | None = None
        self._conversion_lock = Lock()

    def fingerprint_options(self) -> dict[str, object]:
        return self.config.to_dict()

    def convert(self, source: bytes, filename: str) -> ConversionResult:
        if len(source) > self.max_file_bytes:
            raise ValueError("source exceeds converter size limit")
        try:
            from docling.datamodel.base_models import (
                FormatToExtensions,
                InputFormat,  # type: ignore[import-not-found]
            )
            from docling.datamodel.pipeline_options import (  # type: ignore[import-not-found]
                PdfPipelineOptions,
                RapidOcrOptions,
                TableFormerMode,
                TableStructureOptions,
            )
            from docling.document_converter import (  # type: ignore[import-not-found]
                DocumentConverter,
                ImageFormatOption,
                PdfFormatOption,
            )
        except ImportError as exc:
            raise RuntimeError(
                "install docling-context[conversion] for document conversion"
            ) from exc
        name = Path(filename).name
        if not any(
            name.lower().endswith(f".{extension.lower()}")
            for extensions in FormatToExtensions.values()
            for extension in extensions
        ):
            raise ValueError("unsupported document extension")
        try:
            converter_version = version("docling")
        except PackageNotFoundError:
            converter_version = "unknown"
        with tempfile.TemporaryDirectory(prefix="context-convert-") as work:
            root = Path(work)
            input_path = root / name
            output_path = root / "result.dclx"
            input_path.write_bytes(source)
            settings = self.config
            with self._conversion_lock:
                if self._document_converter is None:
                    pipeline = PdfPipelineOptions(
                        do_ocr=settings.do_ocr,
                        do_table_structure=settings.do_table_structure,
                        do_chart_extraction=settings.do_chart_extraction,
                        do_code_enrichment=settings.do_code_enrichment,
                        do_formula_enrichment=settings.do_formula_enrichment,
                        force_backend_text=settings.force_backend_text,
                        generate_page_images=settings.generate_page_images,
                        generate_picture_images=settings.generate_picture_images,
                        ocr_options=RapidOcrOptions(
                            lang=["en"],
                            backend="onnxruntime",
                            model_size="tiny",
                            rapidocr_params={"Global.log_level": "warning"},
                        ),
                        table_structure_options=TableStructureOptions(
                            mode=TableFormerMode(settings.table_mode)
                        ),
                        document_timeout=settings.document_timeout,
                        artifacts_path=settings.artifacts_path,
                        enable_remote_services=False,
                        allow_external_plugins=False,
                    )
                    self._document_converter = DocumentConverter(
                        format_options={
                            InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline),
                            InputFormat.IMAGE: ImageFormatOption(
                                pipeline_options=pipeline
                            ),
                        },
                    )
                result = self._document_converter.convert(
                    input_path,
                    max_file_size=self.max_file_bytes,
                    max_num_pages=self.max_pages or sys.maxsize,
                )
            result.document.save_as_doclang_archive(output_path)
            package = output_path.read_bytes()
        load_package(package)
        return ConversionResult(
            package,
            "docling-local",
            converter_version,
            settings.to_dict(),
            tuple(str(item)[:500] for item in result.errors[:100]),
        )


class DoclingServeConverter:
    """Explicit service endpoint; source bytes are uploaded, never fetched."""

    def __init__(
        self,
        endpoint: str,
        *,
        config: PdfConversionConfig | None = None,
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
        self.config = config or PdfConversionConfig()
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes

    def fingerprint_options(self) -> dict[str, object]:
        return {"endpoint": self.endpoint, "config": self.config.to_dict()}

    def convert(self, source: bytes, filename: str) -> ConversionResult:
        if len(source) > self.config.max_file_bytes:
            raise ValueError("source exceeds remote conversion size limit")
        boundary = uuid.uuid4().hex
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", Path(filename).name)
        fields: dict[str, str | bool | float] = {
            "to_formats": "dclx",
            "target_type": "zip",
            "do_ocr": self.config.do_ocr,
            "do_table_structure": self.config.do_table_structure,
            "do_chart_extraction": self.config.do_chart_extraction,
            "do_code_enrichment": self.config.do_code_enrichment,
            "do_formula_enrichment": self.config.do_formula_enrichment,
            "force_backend_text": self.config.force_backend_text,
            "table_mode": self.config.table_mode,
        }
        if self.config.document_timeout is not None:
            fields["document_timeout"] = self.config.document_timeout
        form_fields = "".join(
            f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{str(value).lower() if isinstance(value, bool) else value}'
            for key, value in fields.items()
        )
        body = (
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{safe_name}"\r\n'
                "Content-Type: application/octet-stream\r\n\r\n"
            ).encode()
            + source
            + (form_fields + f"\r\n--{boundary}--\r\n").encode()
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
            package, "docling-serve", "v1", self.fingerprint_options()
        )


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise ValueError("remote converter redirected outside its configured endpoint")
