"""Reuse complete v0 ZIM vectors while rebuilding the current index."""

from __future__ import annotations

import logging
import os
import os
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from ...diagnostics import public_cause
from ...indexing import schema
from ...indexing.pipeline import _create_temp_path, _file_digest, _fsync_path, _load
from ...model_meta import (
    MODEL_FINGERPRINT_KEY,
    MODEL_PROBE_SHAPE_KEY,
    MODEL_PROBE_TEXTS_KEY,
    MODEL_PROBE_VECTORS_KEY,
)

logger = logging.getLogger(__name__)
_TABLE = "temp.v0_vectors"


class LegacyIndexError(ValueError):
    """A v0 index cannot safely be reused."""


def _signature(path: Path) -> tuple[int, int, int, int]:
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def _id_batches(index, faiss):
    if isinstance(index, faiss.IndexIDMap):
        if not isinstance(faiss.downcast_index(index.index), faiss.IndexFlatIP):
            raise LegacyIndexError("unsupported v0 FAISS layout")
        ids = faiss.vector_to_array(index.id_map)
        for start in range(0, len(ids), 500):
            yield ids[start : start + 500].tolist()
    elif (
        isinstance(index, faiss.IndexIVFScalarQuantizer)
        and index.sq.qtype == faiss.ScalarQuantizer.QT_8bit
    ):
        for list_number in range(index.nlist):
            size = index.invlists.list_size(list_number)
            if not size:
                continue
            pointer = index.invlists.get_ids(list_number)
            try:
                ids = faiss.rev_swig_ptr(pointer, size)
                for start in range(0, size, 500):
                    yield ids[start : start + 500].tolist()
            finally:
                index.invlists.release_ids(list_number, pointer)
    else:
        raise LegacyIndexError("unsupported v0 FAISS layout")


def _probes(meta: dict[str, Any]) -> dict[str, str] | None:
    import json

    from ...embed import load_model_probes

    try:
        texts = json.loads(meta[MODEL_PROBE_TEXTS_KEY])
        vectors = meta[MODEL_PROBE_VECTORS_KEY]
        shape = meta[MODEL_PROBE_SHAPE_KEY]
        if not isinstance(vectors, bytes) or not isinstance(shape, str):
            return None
        encoded = {
            MODEL_PROBE_TEXTS_KEY: schema.encode_json(texts),
            MODEL_PROBE_VECTORS_KEY: schema.encode_bytes(vectors),
            MODEL_PROBE_SHAPE_KEY: shape,
        }
        return encoded if load_model_probes(encoded) is not None else None
    except (KeyError, TypeError, ValueError):
        return None


class LegacyVectors:
    def __init__(
        self,
        db,
        sqlite_path,
        faiss_path,
        signatures,
        token,
        fingerprint,
        probes,
        count,
        dimension,
    ):
        self.db = db
        self.sqlite_path = sqlite_path
        self.faiss_path = faiss_path
        self.signatures = signatures
        self.token = token
        self.fingerprint = fingerprint
        self.probes = probes
        self.count = count
        self.dimension = dimension
        self.storage_bytes = signatures[1][2]

    def missing_rows(self, rows):
        if not rows:
            return []
        found = set()
        for start in range(0, len(rows), 500):
            chunk = rows[start : start + 500]
            placeholders = ",".join("?" for _ in chunk)
            found.update(
                row[0]
                for row in self.db.execute(
                    f"SELECT id FROM {_TABLE} WHERE id IN ({placeholders})",
                    [row.id for row in chunk],
                )
            )
        return [row for row in rows if row.id not in found]

    def valid_files(self) -> bool:
        try:
            return not (
                _signature(self.sqlite_path) != self.signatures[0]
                or _signature(self.faiss_path) != self.signatures[1]
                or any(
                    Path(f"{self.sqlite_path}{suffix}").exists()
                    for suffix in ("-wal", "-shm", "-journal")
                )
            )
        except OSError:
            return False

    def valid(self) -> bool:
        if not self.valid_files():
            return False
        return (
            self.db.execute(
                f"SELECT 1 FROM {_TABLE} AS old "
                "LEFT JOIN docs AS current ON current.rowid = old.id "
                "WHERE current.rowid IS NULL OR current.path IS NOT old.path "
                "OR current.target IS NOT old.target LIMIT 1"
            ).fetchone()
            is None
        )

    def write(self, path: Path) -> int:
        if not self.valid_files() or (
            _file_digest(self.sqlite_path),
            _file_digest(self.faiss_path),
        ) != tuple(self.token.split(":")[:2]):
            raise LegacyIndexError("legacy index changed during the build")
        count = int(self.db.execute("SELECT COUNT(*) FROM vecs").fetchone()[0])
        temp_path = _create_temp_path(path)
        try:
            if count:
                import faiss

                index = faiss.read_index(str(self.faiss_path))
                last = -1
                while rows := self.db.execute(
                    "SELECT id, v FROM vecs WHERE id > ? ORDER BY id LIMIT 50000",
                    (last,),
                ).fetchall():
                    ids, vectors = _load(rows)
                    if vectors.shape[1] != self.dimension:
                        raise LegacyIndexError("new and v0 vector dimensions differ")
                    index.add_with_ids(vectors, ids)
                    last = int(ids[-1])
                faiss.write_index(index, str(temp_path))
            else:
                shutil.copyfile(self.faiss_path, temp_path)
                if _file_digest(temp_path) != self.token.split(":")[1]:
                    raise LegacyIndexError("legacy FAISS changed while copying")
            if not self.valid_files():
                raise LegacyIndexError("legacy index changed during publication")
            _fsync_path(temp_path)
            os.replace(temp_path, path)
            _fsync_path(path.parent)
        finally:
            temp_path.unlink(missing_ok=True)
        return self.count + count


def open_legacy_vectors(producer, index_dir, db, cfg, embedder):
    """Return a validated v0 vector source, or None for an ordinary rebuild."""
    sqlite_path = Path(index_dir) / f"{producer.path.stem}.sqlite"
    faiss_path = Path(index_dir) / f"{producer.path.stem}.faiss"
    if not sqlite_path.exists() or not faiss_path.exists():
        return None
    try:
        if sqlite_path.is_symlink() or faiss_path.is_symlink():
            raise LegacyIndexError("legacy index files must not be symlinks")
        if any(
            Path(f"{sqlite_path}{suffix}").exists()
            for suffix in ("-wal", "-shm", "-journal")
        ):
            raise LegacyIndexError("legacy SQLite index has active sidecars")
        signatures = (_signature(sqlite_path), _signature(faiss_path))
        with closing(
            sqlite3.connect(sqlite_path.resolve().as_uri() + "?mode=ro", uri=True)
        ) as old:
            definitions = dict(
                old.execute(
                    "SELECT name, sql FROM sqlite_master "
                    "WHERE type = 'table' AND name IN ('docs', 'meta')"
                )
            )
            expected = {
                "docs": (
                    "CREATE VIRTUAL TABLE docs USING fts5("
                    "title, path UNINDEXED, target UNINDEXED, "
                    "tokenize='unicode61 remove_diacritics 2')"
                ),
                "meta": "CREATE TABLE meta(key TEXT PRIMARY KEY, value)",
            }
            if any(
                "".join(str(definitions.get(name, "")).split()).casefold()
                != "".join(sql.split()).casefold()
                for name, sql in expected.items()
            ):
                raise LegacyIndexError("unrecognized v0 SQLite layout")
            meta = dict(old.execute("SELECT key, value FROM meta"))
            if (
                str(meta.get("done")) != "1"
                or int(meta.get("filesize", -1)) != producer.source_size
            ):
                raise LegacyIndexError(
                    "legacy index is incomplete or source size differs"
                )
            fingerprint = meta.get(MODEL_FINGERPRINT_KEY)
            if (
                not isinstance(fingerprint, str)
                or len(fingerprint) != 64
                or schema.encode_fingerprint(fingerprint) != fingerprint
            ):
                raise LegacyIndexError("legacy model fingerprint is missing")
            from ...embed import (
                embed_model_probes,
                load_model_probes,
                model_fingerprint,
                model_probes_match,
                serialize_model_probes,
            )

            current = (
                model_fingerprint(cfg["model_dir"]) if cfg.get("model_dir") else ""
            )
            if not current:
                current = getattr(embedder, "fingerprint", "")
            probes = _probes(meta)
            if fingerprint != current:
                loaded = load_model_probes(probes or {})
                if loaded is None or embedder is None:
                    raise LegacyIndexError("legacy model cannot be verified")
                texts, vectors = loaded
                compatible, _, _ = model_probes_match(texts, vectors, embedder)
                if not compatible:
                    raise LegacyIndexError("legacy model probes do not match")
            if not current:
                raise LegacyIndexError("current model fingerprint is unavailable")

            sqlite_digest = _file_digest(sqlite_path)
            faiss_digest = _file_digest(faiss_path)
            import faiss

            flags = faiss.IO_FLAG_READ_ONLY
            if os.name != "nt":
                flags |= faiss.IO_FLAG_MMAP
            # Windows FAISS builds cannot mmap legacy IVF inverted lists.
            index = faiss.read_index(str(faiss_path), flags)
            if index.metric_type != faiss.METRIC_INNER_PRODUCT or not index.is_trained:
                raise LegacyIndexError("unsupported v0 FAISS metric or training state")
            if not index.ntotal or not index.d:
                raise LegacyIndexError("legacy FAISS index has no vectors")
            loaded = load_model_probes(probes or {})
            if loaded is None:
                if embedder is None:
                    raise LegacyIndexError("cannot verify legacy vector dimensions")
                texts, vectors = embed_model_probes(embedder)
                probes = serialize_model_probes(texts, vectors)
                loaded = (texts, vectors)
            if loaded[1].shape[1] != index.d:
                raise LegacyIndexError("legacy probe and vector dimensions differ")
            db.execute(f"DROP TABLE IF EXISTS {_TABLE}")
            db.execute(
                f"CREATE TEMP TABLE {_TABLE} ("
                "id INTEGER PRIMARY KEY, path TEXT NOT NULL, target INTEGER)"
            )
            inserted = 0
            for ids in _id_batches(index, faiss):
                if any(
                    type(value) is not int or value < 0 or value >= producer.count
                    for value in ids
                ):
                    raise LegacyIndexError("legacy vector IDs are outside the archive")
                placeholders = ",".join("?" for _ in ids)
                rows = old.execute(
                    f"SELECT rowid, path, target FROM docs WHERE rowid IN ({placeholders})",
                    ids,
                ).fetchall()
                if len(rows) != len(ids) or any(
                    not isinstance(path, str)
                    or not path
                    or (target is not None and not isinstance(target, int))
                    for _, path, target in rows
                ):
                    raise LegacyIndexError(
                        "legacy vectors do not match their SQLite rows"
                    )
                db.executemany(f"INSERT INTO {_TABLE} VALUES (?, ?, ?)", rows)
                inserted += len(rows)
            if inserted != index.ntotal:
                raise LegacyIndexError("legacy vector count does not match FAISS")
            count, dimension = int(index.ntotal), int(index.d)
        token = f"{sqlite_digest}:{faiss_digest}:{current}"
        reuse = LegacyVectors(
            db,
            sqlite_path,
            faiss_path,
            signatures,
            token,
            fingerprint,
            probes,
            count,
            dimension,
        )
        if not reuse.valid_files():
            raise LegacyIndexError("legacy index changed while opening")
        logger.info("%s: reusing %d v0 vectors", producer.key, count)
        return reuse
    except (
        OSError,
        sqlite3.Error,
        TypeError,
        ValueError,
        RuntimeError,
        KeyError,
    ) as error:
        db.execute(f"DROP TABLE IF EXISTS {_TABLE}")
        logger.warning(
            "%s: cannot reuse v0 index (%s); rebuilding",
            producer.key,
            public_cause(error),
        )
        logger.debug("legacy index error for %s: %s", producer.key, error)
        return None
