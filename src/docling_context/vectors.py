"""Optional, rebuildable local vector projection with stable external IDs."""

from __future__ import annotations

import hashlib
import math
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from .package import load_package

if TYPE_CHECKING:
    from .local import LocalContextStore


class EmbeddingProvider(Protocol):
    model_id: str
    dimensions: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class VectorIndex(Protocol):
    def upsert(self, identifier: int, vector: list[float]) -> None: ...
    def remove(self, identifier: int) -> None: ...
    def search(
        self, query: list[float], allowed_ids: set[int], k: int
    ) -> list[tuple[int, float]]: ...
    def checkpoint(self) -> None: ...
    def rebuild(self) -> None: ...


class TurbovecIndex:
    """IdMapIndex snapshot; SQLite embeddings and DCLX remain authoritative."""

    def __init__(
        self,
        store: LocalContextStore,
        provider: EmbeddingProvider,
        *,
        force_rebuild: bool = False,
        progress: Callable[[int, int], None] | None = None,
    ):
        try:
            import numpy as np
            from turbovec import IdMapIndex  # type: ignore[import-untyped]
        except ImportError as exc:
            raise RuntimeError(
                "install docling-context[vectors] for vector search"
            ) from exc
        if (
            not provider.model_id
            or not 8 <= provider.dimensions <= 16_384
            or provider.dimensions % 8
        ):
            raise ValueError("embedding dimensions must be a multiple of 8 up to 16384")
        self.store = store
        self.provider = provider
        self.np = np
        self.index_type = IdMapIndex
        self.index = IdMapIndex(dim=provider.dimensions, bit_width=4)
        self.generation = ""
        self._progress = progress
        if force_rebuild:
            self.rebuild()
        else:
            self._restore_or_rebuild()

    def _offset(self) -> int:
        return self.store.db.execute(
            "SELECT COALESCE(MAX(sequence),0) FROM outbox"
        ).fetchone()[0]

    def _path(self, generation: str) -> Path:
        return self.store.root / f"vectors-{generation}.tvim"

    def _restore_or_rebuild(self) -> None:
        state = self.store.db.execute(
            "SELECT * FROM vector_state WHERE singleton=1"
        ).fetchone()
        if state is not None and (
            state["model_id"] == self.provider.model_id
            and state["dimensions"] == self.provider.dimensions
            and state["normalization"] == "l2"
        ):
            path = self._path(state["generation"])
            try:
                if (
                    hashlib.sha256(path.read_bytes()).hexdigest()
                    == state["snapshot_hash"]
                ):
                    loaded = self.index_type.load(str(path))
                    if loaded.dim == self.provider.dimensions:
                        members = self.store.db.execute(
                            "SELECT COUNT(*) FROM vector_members WHERE generation=?",
                            (state["generation"],),
                        ).fetchone()[0]
                        if members or (
                            state["outbox_offset"] == self._offset()
                            and not self.store.db.execute(
                                """SELECT 1 FROM retrieval_units
                                   WHERE index_generation=? AND embedding IS NOT NULL
                                   LIMIT 1""",
                                (state["generation"],),
                            ).fetchone()
                        ):
                            if state["outbox_offset"] == self._offset():
                                self.index = loaded
                                self.generation = state["generation"]
                                return
                            if state["outbox_offset"] >= 0:
                                self._replay(state, loaded)
                                return
            except (OSError, ValueError, RuntimeError):
                pass
        self.rebuild()

    def _replay(self, state, loaded) -> None:
        """Apply changed document URIs to a compatible saved projection."""
        db = self.store.db
        offset = self._offset()
        changed = db.execute(
            "SELECT DISTINCT tenant_id,uri FROM outbox WHERE sequence>? AND sequence<=?",
            (state["outbox_offset"], offset),
        ).fetchall()
        old_ids: set[int] = set()
        rows = []
        for event in changed:
            old_ids.update(
                row[0]
                for row in db.execute(
                    """SELECT vector_id FROM vector_members
                       WHERE generation=? AND tenant_id=? AND uri=?""",
                    (state["generation"], event["tenant_id"], event["uri"]),
                )
            )
            rows.extend(
                db.execute(
                    """SELECT r.*, d.package_hash FROM retrieval_units r JOIN documents d
                       ON d.tenant_id=r.tenant_id AND d.uri=r.uri
                       AND d.revision_id=r.revision_id AND d.available=1
                       WHERE r.tenant_id=? AND r.uri=? ORDER BY r.vector_id""",
                    (event["tenant_id"], event["uri"]),
                ).fetchall()
            )
        for identifier in old_ids:
            loaded.remove(identifier)
        updates: list[tuple[str, int, str, bytes, int]] = []
        member_rows = []
        for start in range(0, len(rows), 32):
            batch = rows[start : start + 32]
            generated = self._generate(batch)
            eligible = [row for row in batch if row["vector_id"] in generated]
            if eligible:
                loaded.add_with_ids(
                    self.np.ascontiguousarray(
                        [generated[row["vector_id"]] for row in eligible],
                        dtype=self.np.float32,
                    ),
                    self.np.asarray(
                        [row["vector_id"] for row in eligible], dtype=self.np.uint64
                    ),
                )
            member_rows.extend(eligible)
            updates.extend(
                (
                    self.provider.model_id,
                    self.provider.dimensions,
                    "l2",
                    generated[row["vector_id"]].tobytes(),
                    row["vector_id"],
                )
                for row in eligible
            )
        generation = uuid.uuid4().hex
        path = self._path(generation)
        loaded.write(str(path))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        try:
            with self.store.transaction():
                if self._offset() != offset:
                    raise RuntimeError("documents changed while replaying vector index")
                db.executemany(
                    """UPDATE retrieval_units SET model_id=?,dimensions=?,normalization=?,
                       embedding=? WHERE vector_id=?""",
                    updates,
                )
                db.execute(
                    "UPDATE retrieval_units SET index_generation=? WHERE index_generation=?",
                    (generation, state["generation"]),
                )
                db.executemany(
                    "UPDATE retrieval_units SET index_generation=? WHERE vector_id=?",
                    ((generation, row["vector_id"]) for row in member_rows),
                )
                db.execute(
                    """INSERT INTO vector_members (generation,tenant_id,uri,vector_id)
                       SELECT ?,tenant_id,uri,vector_id FROM vector_members
                       WHERE generation=?""",
                    (generation, state["generation"]),
                )
                db.executemany(
                    "DELETE FROM vector_members WHERE generation=? AND vector_id=?",
                    ((generation, identifier) for identifier in old_ids),
                )
                db.executemany(
                    "INSERT INTO vector_members VALUES (?,?,?,?)",
                    (
                        (generation, row["tenant_id"], row["uri"], row["vector_id"])
                        for row in member_rows
                    ),
                )
                db.execute(
                    """UPDATE vector_state SET generation=?,outbox_offset=?,snapshot_hash=?
                       WHERE singleton=1 AND generation=?""",
                    (generation, offset, digest, state["generation"]),
                )
                db.execute(
                    "DELETE FROM vector_members WHERE generation=?",
                    (state["generation"],),
                )
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        self.index = loaded
        self.generation = generation
        self._path(state["generation"]).unlink(missing_ok=True)

    def refresh(self) -> None:
        state = self.store.db.execute(
            "SELECT outbox_offset,generation FROM vector_state WHERE singleton=1"
        ).fetchone()
        if (
            state is None
            or state["outbox_offset"] != self._offset()
            or state["generation"] != self.generation
        ):
            self._restore_or_rebuild()

    def _normal(self, values: list[float]):
        if len(values) != self.provider.dimensions or not all(
            math.isfinite(value) and abs(value) < 1e16 for value in values
        ):
            raise ValueError("embedding has invalid dimensions or coordinates")
        array = self.np.ascontiguousarray(values, dtype=self.np.float32)
        magnitude = float(self.np.linalg.norm(array))
        if not math.isfinite(magnitude) or magnitude <= 1e-10:
            raise ValueError("embedding has zero or invalid norm")
        return self.np.ascontiguousarray(array / magnitude, dtype=self.np.float32)

    def _generate(self, rows) -> dict[int, Any]:
        """Embed text in batches and attached images only when supported."""
        from .embeddings import embed_image, embed_text_image

        supports_images = getattr(
            self.provider,
            "supports_images",
            callable(getattr(self.provider, "embed_image", None)),
        )
        text_rows = [
            row
            for row in rows
            if row["text"].strip() and (not row["asset_path"] or not supports_images)
        ]
        result: dict[int, Any] = {}
        if text_rows:
            values = self.provider.embed([row["text"] for row in text_rows])
            if len(values) != len(text_rows):
                raise ValueError(
                    "embedding provider returned the wrong number of vectors"
                )
            result.update(
                (row["vector_id"], self._normal(vector))
                for row, vector in zip(text_rows, values, strict=True)
            )
        if supports_images:
            packages: dict[str, Any] = {}
            for row in rows:
                if not row["asset_path"]:
                    continue
                package_hash = row["package_hash"]
                if package_hash not in packages:
                    packages[package_hash] = load_package(
                        self.store.packages.get(package_hash)
                    )
                image = packages[package_hash].get_part_bytes(row["asset_path"])
                if image is None:
                    continue
                vector = (
                    embed_text_image(self.provider, row["text"], image)
                    if row["text"].strip()
                    else embed_image(self.provider, image)
                )
                if vector is not None:
                    result[row["vector_id"]] = self._normal(vector)
        return result

    def upsert(self, identifier: int, vector: list[float]) -> None:
        normalized = self._normal(vector).reshape(1, -1)
        self.index.remove(identifier)
        self.index.add_with_ids(
            normalized, self.np.asarray([identifier], dtype=self.np.uint64)
        )

    def remove(self, identifier: int) -> None:
        self.index.remove(identifier)

    def search(
        self, query: list[float], allowed_ids: set[int], k: int
    ) -> list[tuple[int, float]]:
        if not allowed_ids or k < 1:
            return []
        permitted = sorted(
            identifier for identifier in allowed_ids if identifier in self.index
        )
        if not permitted:
            return []
        vector = self._normal(query).reshape(1, -1)
        scores, identifiers = self.index.search(
            vector,
            k=min(k, len(permitted)),
            allowlist=self.np.asarray(permitted, dtype=self.np.uint64),
        )
        return [
            (int(identifier), float(score))
            for identifier, score in zip(identifiers[0], scores[0], strict=True)
        ]

    def checkpoint(self) -> None:
        if not self.generation:
            raise RuntimeError("vector generation is not set")
        path = self._path(self.generation)
        self.index.write(str(path))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        with self.store.transaction():
            self.store.db.execute(
                "UPDATE vector_state SET snapshot_hash=? WHERE singleton=1 AND generation=?",
                (digest, self.generation),
            )

    def rebuild(self) -> None:
        """Build a new model generation and switch it only after its snapshot exists."""
        db = self.store.db
        previous = db.execute(
            "SELECT generation FROM vector_state WHERE singleton=1"
        ).fetchone()
        rows = db.execute(
            """SELECT r.*, d.package_hash FROM retrieval_units r JOIN documents d
               ON d.tenant_id=r.tenant_id AND d.uri=r.uri
               AND d.revision_id=r.revision_id AND d.available=1
               ORDER BY r.vector_id"""
        ).fetchall()
        if self._progress is not None:
            self._progress(0, len(rows))
        offset = self._offset()
        index = self.index_type(dim=self.provider.dimensions, bit_width=4)
        updates = []
        member_rows = []
        for start in range(0, len(rows), 32):
            batch = rows[start : start + 32]
            missing = [
                row
                for row in batch
                if row["model_id"] != self.provider.model_id
                or row["dimensions"] != self.provider.dimensions
                or row["normalization"] != "l2"
                or row["embedding"] is None
                or len(row["embedding"]) != 4 * self.provider.dimensions
            ]
            missing_ids = {row["vector_id"] for row in missing}
            new_vectors = self._generate(missing) if missing else {}
            vectors = []
            eligible = []
            for row in batch:
                vector = new_vectors.get(row["vector_id"])
                if vector is None and row["vector_id"] not in missing_ids:
                    vector = self.np.frombuffer(row["embedding"], dtype=self.np.float32)
                if vector is None:
                    continue
                updates.append(
                    (
                        self.provider.model_id,
                        self.provider.dimensions,
                        "l2",
                        vector.tobytes(),
                        row["vector_id"],
                    )
                )
                vectors.append(vector)
                eligible.append(row)
            if eligible:
                index.add_with_ids(
                    self.np.ascontiguousarray(vectors, dtype=self.np.float32),
                    self.np.asarray(
                        [row["vector_id"] for row in eligible], dtype=self.np.uint64
                    ),
                )
            member_rows.extend(eligible)
            if self._progress is not None:
                self._progress(min(start + len(batch), len(rows)), len(rows))
        generation = uuid.uuid4().hex
        path = self._path(generation)
        index.write(str(path))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        with self.store.transaction():
            if self._offset() != offset:
                path.unlink(missing_ok=True)
                raise RuntimeError("documents changed while rebuilding vector index")
            db.execute(
                """UPDATE retrieval_units SET model_id=NULL,dimensions=NULL,
                   normalization=NULL,embedding=NULL,index_generation=NULL"""
            )
            db.executemany(
                """UPDATE retrieval_units SET model_id=?,dimensions=?,normalization=?,
                   embedding=? WHERE vector_id=?""",
                updates,
            )
            db.execute(
                "UPDATE retrieval_units SET index_generation=? WHERE embedding IS NOT NULL",
                (generation,),
            )
            db.execute("DELETE FROM vector_members")
            db.executemany(
                "INSERT INTO vector_members VALUES (?,?,?,?)",
                (
                    (generation, row["tenant_id"], row["uri"], row["vector_id"])
                    for row in member_rows
                ),
            )
            db.execute(
                """INSERT INTO vector_state
                   (singleton,generation,model_id,dimensions,normalization,outbox_offset,snapshot_hash)
                   VALUES (1,?,?,?,?,?,?) ON CONFLICT(singleton) DO UPDATE SET
                   generation=excluded.generation,model_id=excluded.model_id,
                   dimensions=excluded.dimensions,normalization=excluded.normalization,
                   outbox_offset=excluded.outbox_offset,snapshot_hash=excluded.snapshot_hash""",
                (
                    generation,
                    self.provider.model_id,
                    self.provider.dimensions,
                    "l2",
                    offset,
                    digest,
                ),
            )
        old = previous["generation"] if previous is not None else self.generation
        self.index = index
        self.generation = generation
        if old:
            self._path(old).unlink(missing_ok=True)
