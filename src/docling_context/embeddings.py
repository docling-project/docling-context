"""Local embedding adapters with optional image and mixed-input support."""

from __future__ import annotations

import math
import platform
import sys
import tempfile
from importlib.util import find_spec
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .vectors import EmbeddingProvider

DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"
CLIP_MODEL = "Qdrant/clip-ViT-B-32"
_CLIP_TEXT = "Qdrant/clip-ViT-B-32-text"
_CLIP_IMAGE = "Qdrant/clip-ViT-B-32-vision"
_MAX_IMAGE_BYTES = 20 * 1024 * 1024
_MLX_MODELS = {
    "BAAI/bge-small-en-v1.5": "bge-small",
    "BAAI/bge-base-en-v1.5": "bge-base",
    "BAAI/bge-large-en-v1.5": "bge-large",
}
_MLX_PREFIX = "mlx:"
_BGE_QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "


def _apple_silicon() -> bool:
    return sys.platform == "darwin" and platform.machine() == "arm64"


def embed_text(provider: EmbeddingProvider, text: str) -> list[float]:
    """Embed a passage with any text-capable provider."""
    method = getattr(provider, "embed_text", None)
    return method(text) if callable(method) else provider.embed([text])[0]


def embed_image(provider: EmbeddingProvider, image: bytes) -> list[float] | None:
    """Return None when the selected provider has no image encoder."""
    method = getattr(provider, "embed_image", None)
    return method(image) if callable(method) else None


def embed_text_image(
    provider: EmbeddingProvider, text: str, image: bytes
) -> list[float] | None:
    """Combine modalities when supported; otherwise use available text."""
    method = getattr(provider, "embed_text_image", None)
    if callable(method):
        return method(text, image)
    if text.strip():
        return embed_text(provider, text)
    return embed_image(provider, image)


def _unit(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if not math.isfinite(norm) or norm <= 1e-10:
        raise ValueError("embedding has zero or invalid norm")
    return [value / norm for value in vector]


class FastEmbedProvider:
    """FastEmbed text models, with paired CLIP encoders for multimodal input."""

    def __init__(self, model_id: str = DEFAULT_MODEL, *, offline: bool = False):
        try:
            from fastembed import TextEmbedding  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "install docling-context[vectors] for local embedding models"
            ) from exc
        text_model = _CLIP_TEXT if model_id == CLIP_MODEL else model_id
        supported = {entry["model"] for entry in TextEmbedding.list_supported_models()}
        if text_model not in supported:
            raise ValueError(f"unsupported local text embedding model: {model_id}")
        self.model_id = model_id
        self.supports_images = model_id == CLIP_MODEL
        self.dimensions = TextEmbedding.get_embedding_size(text_model)
        self._text = TextEmbedding(model_name=text_model, local_files_only=offline)
        self._image: Any = None
        self._offline = offline

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [vector.tolist() for vector in self._text.passage_embed(texts)]

    def embed_query(self, query: str) -> list[float]:
        return next(iter(self._text.query_embed(query))).tolist()

    def embed_text(self, text: str) -> list[float]:
        return self.embed([text])[0]

    def embed_image(self, image: bytes) -> list[float] | None:
        if self.model_id != CLIP_MODEL:
            return None
        if not image or len(image) > _MAX_IMAGE_BYTES:
            raise ValueError("image input is empty or too large")
        if self._image is None:
            from fastembed import ImageEmbedding  # type: ignore[import-not-found]

            self._image = ImageEmbedding(
                model_name=_CLIP_IMAGE, local_files_only=self._offline
            )
        with tempfile.TemporaryDirectory(prefix="dc-embedding-") as directory:
            path = Path(directory) / "image"
            path.write_bytes(image)
            return next(iter(self._image.embed([str(path)]))).tolist()

    def embed_text_image(self, text: str, image: bytes) -> list[float] | None:
        if self.model_id != CLIP_MODEL:
            return self.embed_text(text) if text.strip() else None
        visual = self.embed_image(image)
        if not text.strip():
            return visual
        assert visual is not None
        passage = self.embed_text(text)
        return _unit(
            [a + b for a, b in zip(_unit(passage), _unit(visual), strict=True)]
        )


class MlxEmbeddingProvider:
    """Apple Silicon text embeddings with explicit backend identity."""

    def __init__(self, model_name: str):
        if model_name not in _MLX_MODELS:
            raise ValueError(f"MLX does not support this model: {model_name}")
        if not _apple_silicon():
            raise RuntimeError("MLX embeddings require Apple Silicon")
        try:
            import mlx_embedding_models.registry as mlx_registry  # type: ignore[import-not-found,import-untyped]
            from mlx_embedding_models.embedding import (  # type: ignore[import-not-found,import-untyped]
                EmbeddingModel,
            )
        except ImportError as exc:
            raise RuntimeError(
                "install docling-context[vectors] for MLX embeddings"
            ) from exc
        registry_name = _MLX_MODELS[model_name]
        self.model_id = f"{_MLX_PREFIX}{model_name}"
        self.supports_images = False
        self.dimensions = int(mlx_registry.registry[registry_name]["ndim"])
        self._model = EmbeddingModel.from_registry(registry_name)

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return self._model.encode(texts, show_progress=False).tolist()

    def embed_query(self, query: str) -> list[float]:
        return self.embed([_BGE_QUERY_INSTRUCTION + query])[0]


def local_embedding_provider(
    model_name: str = DEFAULT_MODEL,
    *,
    backend: str = "auto",
    offline: bool = False,
) -> EmbeddingProvider:
    """Choose MLX where available, retaining ONNX for other models and hosts."""
    if backend not in {"auto", "mlx", "onnx"}:
        raise ValueError("backend must be auto, mlx, or onnx")
    if backend == "mlx":
        if offline:
            raise ValueError("--offline is not supported with the MLX backend")
        return MlxEmbeddingProvider(model_name)
    if (
        backend == "auto"
        and not offline
        and model_name in _MLX_MODELS
        and _apple_silicon()
        and find_spec("mlx_embedding_models") is not None
    ):
        return MlxEmbeddingProvider(model_name)
    return FastEmbedProvider(model_name, offline=offline)


def stored_embedding_provider(model_id: str) -> EmbeddingProvider:
    """Open the same backend that wrote a vector generation."""
    if model_id.startswith(_MLX_PREFIX):
        return MlxEmbeddingProvider(model_id[len(_MLX_PREFIX) :])
    return FastEmbedProvider(model_id)
