# Document conversion settings

Install local document conversion with `uv sync --extra conversion`. Pass a
`PdfConversionConfig` to `LocalDoclingConverter`, or supply a JSON file to
`dc add-resource --config`. Docling runs locally by default; this adapter keeps
external services and external plugins disabled. The conversion extra includes
RapidOCR and its ONNX Runtime engine for the default OCR configuration. The
first OCR run may download model weights; later runs use the local copy.

```python
from docling_context import LocalDoclingConverter, PdfConversionConfig

config = PdfConversionConfig(
    do_ocr=False,
    do_table_structure=True,
    do_chart_extraction=False,
    table_mode="accurate",
    max_pages=None,
)
converter = LocalDoclingConverter(config)
```

| Setting | Default | Effect |
| --- | --- | --- |
| `do_ocr` | `true` | Read scanned text with RapidOCR, English PP-OCRv6 `tiny` |
| `do_table_structure` | `true` | Reconstruct tables and cells |
| `do_chart_extraction` | `false` | Extract structured chart data |
| `do_code_enrichment` | `false` | Enrich code blocks |
| `do_formula_enrichment` | `false` | Enrich formulas |
| `force_backend_text` | `false` | Prefer embedded PDF text |
| `generate_page_images` | `true` | Include rendered page images when available |
| `generate_picture_images` | `true` | Include extracted picture images when available |
| `table_mode` | `accurate` | Fixed to `accurate`; `fast` is rejected |
| `max_pages` | `null` | Unlimited pages by default; set a positive number to limit conversion |
| `max_file_bytes` | `50000000` | Maximum PDF source size |
| `document_timeout` | `null` | Optional Docling processing timeout in seconds |
| `artifacts_path` | `null` | Optional local model-artifact directory |

The PDF and image pipelines use the OCR and image settings; other formats use
Docling's format-specific defaults. The entire config is recorded in each
document's provenance and contributes to its ingestion fingerprint. Changing a
setting creates a new revision on the next ingestion. `dc` flags such as
`--no-ocr`, `--no-tables`, and `--charts` override the corresponding JSON fields.
See the [example JSON](../examples/adding_resources/conversion_config.json).

The opt-in `DoclingServeConverter(endpoint, config=config)` forwards supported
OCR, table, chart, code, formula, and text options to the service. Local limits
such as `max_pages`, model artifact paths, and image generation settings are
not enforced by the service adapter; configure those on the service separately.
Only explicit HTTPS or loopback HTTP endpoints are accepted, and redirects are
rejected. Source URLs are never fetched during ingestion.
