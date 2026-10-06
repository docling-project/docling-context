"""Direct text, image, and mixed-input embedding behavior."""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import pytest

from docling_context import (
    CLIP_MODEL,
    DEFAULT_MODEL,
    FastEmbedProvider,
    embed_image,
    embed_text,
    embed_text_image,
    embeddings,
)


class TextOnly:
    model_id = "fixture"
    dimensions = 8

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[float(len(text)), *([0.0] * 7)] for text in texts]


def test_text_only_provider_skips_unsupported_images():
    provider = TextOnly()
    assert embed_text(provider, "rock") == [4.0, *([0.0] * 7)]
    assert embed_image(provider, b"image") is None
    assert embed_text_image(provider, "rock", b"image") == embed_text(provider, "rock")
    assert embed_text_image(provider, "", b"image") is None


def test_fastembed_provider_keeps_query_and_passage_paths_separate(monkeypatch):
    class FakeVector(list):
        def tolist(self):
            return list(self)

    class FakeTextEmbedding:
        @staticmethod
        def list_supported_models():
            return [
                {"model": DEFAULT_MODEL},
                {"model": "Qdrant/clip-ViT-B-32-text"},
            ]

        @staticmethod
        def get_embedding_size(_model):
            return 8

        def __init__(self, **_kwargs):
            pass

        def passage_embed(self, texts):
            return iter(FakeVector([1.0, *([0.0] * 7)]) for _ in texts)

        def query_embed(self, _text):
            return iter([FakeVector([0.0, 1.0, *([0.0] * 6)])])

    class FakeImageEmbedding:
        def __init__(self, **_kwargs):
            pass

        def embed(self, paths):
            assert list(paths)
            return iter([FakeVector([0.0, 1.0, *([0.0] * 6)])])

    monkeypatch.setitem(
        sys.modules,
        "fastembed",
        SimpleNamespace(
            TextEmbedding=FakeTextEmbedding, ImageEmbedding=FakeImageEmbedding
        ),
    )
    text_provider = FastEmbedProvider()
    assert text_provider.embed_text("rock")[0] == 1.0
    assert text_provider.embed_query("rock")[1] == 1.0
    assert text_provider.embed_image(b"image") is None
    assert text_provider.embed_text_image("rock", b"image") == text_provider.embed_text(
        "rock"
    )
    assert text_provider.embed_text_image("", b"image") is None
    mixed = FastEmbedProvider(CLIP_MODEL)
    assert mixed.embed_image(b"image")[1] == 1.0
    vector = mixed.embed_text_image("rock", b"image")
    assert vector is not None and vector[0] > 0 and vector[1] > 0


def test_mlx_auto_selection_and_saved_backend_identity(monkeypatch):
    encoded: list[list[str]] = []

    class FakeResult:
        def __init__(self, texts):
            self.texts = texts

        def tolist(self):
            return [[1.0, *([0.0] * 383)] for _ in self.texts]

    class FakeModel:
        @classmethod
        def from_registry(cls, name):
            assert name == "bge-small"
            return cls()

        def encode(self, texts, *, show_progress):
            assert show_progress is False
            encoded.append(texts)
            return FakeResult(texts)

    package = ModuleType("mlx_embedding_models")
    package.__path__ = []
    embedding_module = ModuleType("mlx_embedding_models.embedding")
    embedding_module.EmbeddingModel = FakeModel
    registry_module = ModuleType("mlx_embedding_models.registry")
    registry_module.registry = {"bge-small": {"ndim": 384}}
    monkeypatch.setitem(sys.modules, "mlx_embedding_models", package)
    monkeypatch.setitem(sys.modules, "mlx_embedding_models.embedding", embedding_module)
    monkeypatch.setitem(sys.modules, "mlx_embedding_models.registry", registry_module)
    monkeypatch.setattr(embeddings, "_apple_silicon", lambda: True)
    monkeypatch.setattr(embeddings, "find_spec", lambda _name: object())

    class FakeOnnx:
        def __init__(self, model_name, *, offline=False):
            self.model_id = model_name
            self.offline = offline

    monkeypatch.setattr(embeddings, "FastEmbedProvider", FakeOnnx)
    provider = embeddings.local_embedding_provider(DEFAULT_MODEL)
    assert provider.model_id == "mlx:BAAI/bge-small-en-v1.5"
    assert provider.dimensions == 384
    assert provider.embed(["rock"])[0][0] == 1.0
    provider.embed_query("rock")
    assert encoded[-1][0].startswith("Represent this sentence")
    assert (
        embeddings.stored_embedding_provider(provider.model_id).model_id
        == provider.model_id
    )
    assert embeddings.stored_embedding_provider(DEFAULT_MODEL).model_id == DEFAULT_MODEL
    assert embeddings.local_embedding_provider(CLIP_MODEL).model_id == CLIP_MODEL
    assert embeddings.local_embedding_provider(DEFAULT_MODEL, offline=True).offline
    monkeypatch.setattr(embeddings, "find_spec", lambda _name: None)
    assert embeddings.local_embedding_provider(DEFAULT_MODEL).model_id == DEFAULT_MODEL
    with pytest.raises(ValueError, match="does not support"):
        embeddings.local_embedding_provider(CLIP_MODEL, backend="mlx")
