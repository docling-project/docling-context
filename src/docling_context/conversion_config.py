"""Portable conversion settings shared by local and service adapters."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class PdfConversionConfig:
    do_ocr: bool = True
    do_table_structure: bool = True
    do_chart_extraction: bool = False
    do_code_enrichment: bool = False
    do_formula_enrichment: bool = False
    force_backend_text: bool = False
    generate_page_images: bool = True
    generate_picture_images: bool = True
    table_mode: Literal["accurate"] = "accurate"
    max_pages: int | None = None
    max_file_bytes: int = 50_000_000
    document_timeout: float | None = None
    artifacts_path: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "do_ocr",
            "do_table_structure",
            "do_chart_extraction",
            "do_code_enrichment",
            "do_formula_enrichment",
            "force_backend_text",
            "generate_page_images",
            "generate_picture_images",
        ):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be a boolean")
        if self.table_mode != "accurate":
            raise ValueError("table_mode must be accurate")
        if self.max_pages is not None and (
            type(self.max_pages) is not int or self.max_pages < 1
        ):
            raise ValueError("max_pages must be a positive integer or null")
        if type(self.max_file_bytes) is not int or self.max_file_bytes < 1:
            raise ValueError("max_file_bytes must be a positive integer")
        if self.document_timeout is not None and (
            isinstance(self.document_timeout, bool)
            or not isinstance(self.document_timeout, (int, float))
            or self.document_timeout <= 0
        ):
            raise ValueError("document_timeout must be positive")
        if self.artifacts_path is not None and not isinstance(self.artifacts_path, str):
            raise TypeError("artifacts_path must be a path string")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> PdfConversionConfig:
        if not isinstance(value, dict):
            raise TypeError("conversion config must be a JSON object")
        unknown = set(value) - {item.name for item in fields(cls)}
        if unknown:
            raise ValueError(
                f"unknown conversion settings: {', '.join(sorted(unknown))}"
            )
        return cls(**value)

    @classmethod
    def from_json_file(cls, path: str | Path) -> PdfConversionConfig:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
