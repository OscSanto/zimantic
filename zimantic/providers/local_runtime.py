"""Shared lifecycle helpers for locally indexed providers.

Files and Kolibri open the same published index format and expose the same
health and FAISS-reopen behavior. Keeping that code here prevents the two
providers from drifting on capability reports or index reuse.
"""

from __future__ import annotations

import secrets
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..content.store import ArticleNotFoundError
from ..diagnostics import public_cause
from ..indexing import schema
from ..indexing.locator import IndexLocator
from ..model_meta import (
    MODEL_PROBE_SHAPE_KEY,
    MODEL_PROBE_TEXTS_KEY,
    MODEL_PROBE_VECTORS_KEY,
)
from .base import IndexSummary, SourceInfo
from .files.discovery import FilesIndexOptions, open_files_index
from .metadata import apply_index_metadata


@dataclass(frozen=True)
class PreparedLocalIndexes:
    """One provider refresh's opened indexes and public source records."""

    indexes: dict[str, Any]
    searchable: list[Any]
    library: list[Any]
    discarded: list[Any]


def prepare_local_indexes(
    sources: list[Any],
    *,
    existing: Mapping[str, Any],
    index_dir: Path,
    options: FilesIndexOptions,
    metadata_only: bool,
    can_reuse: Callable[[Any, SourceInfo], bool],
    provider_label: str,
    serving_label: str,
    logger,
) -> PreparedLocalIndexes:
    """Open or reuse every source index for one refresh.

    A source without an index stays in ``library`` but not in ``searchable``;
    an open failure is logged and degrades that source to lexical/native search
    rather than failing the collection.
    """
    new_indexes: dict[str, Any] = {}
    searchable: list[Any] = []
    library: list[Any] = []

    for source in sources:
        info = source.info
        current = existing.get(info.key)
        index = None
        if (
            not metadata_only
            and current is not None
            and can_reuse(current, info)
        ):
            index = current
        else:
            try:
                index = open_files_index(
                    info, index_dir=index_dir, options=options
                )
            except Exception as error:
                logger.warning(
                    "%s: index open failed (%s); serving %s without an index "
                    "until it is rebuilt",
                    info.key,
                    public_cause(error),
                    serving_label,
                )
                logger.debug(
                    "%s index open error for %s: %s",
                    provider_label,
                    info.key,
                    error,
                )
                index = None

        if index is not None:
            object.__setattr__(info, "searchable", True)
            apply_index_metadata(info, index.index_meta)
            if metadata_only:
                new_indexes[info.key] = IndexSummary(
                    done=str(getattr(index, "done", "") or ""),
                    index_meta=dict(getattr(index, "index_meta", {}) or {}),
                    generation=getattr(index, "generation", None),
                )
                try:
                    index.close()
                except Exception:
                    pass
            else:
                new_indexes[info.key] = index
            searchable.append(source)
        else:
            object.__setattr__(info, "searchable", False)
        library.append(source)

    discarded = [
        old
        for key, old in existing.items()
        if new_indexes.get(key) is not old
    ]
    return PreparedLocalIndexes(
        indexes=new_indexes,
        searchable=searchable,
        library=library,
        discarded=discarded,
    )


class LocalIndexedRuntimeMixin:
    """Common index lifecycle, health, and compatibility behavior.

    Subclasses provide the runtime attributes initialized by FilesRuntime and
    KolibriRuntime and set ``_provider_label``/``_serving_label`` for messages.
    """

    _provider_label = "local"
    _serving_label = "content"

    def start_faiss(self) -> None:
        # bind_search(defer_faiss=False) runs before this, so do not gate on it.
        if not self._semantic:
            return
        try:
            import faiss  # noqa: F401
        except Exception as error:
            self._logger.warning(
                "zimantic: semantic search disabled: FAISS unavailable (%s); "
                "falling back to lexical search",
                public_cause(error),
            )
            self._logger.debug(
                "%s FAISS load error: %s", self._provider_label, error
            )
            return
        getter = self._model_fingerprint_getter
        if getter is not None:
            try:
                self._model_fingerprint = getter()
            except Exception as error:
                self._logger.warning(
                    "zimantic: could not fingerprint the embedding model (%s); "
                    "vectors will not be checked against it",
                    public_cause(error),
                )
                self._logger.debug(
                    "%s model fingerprint error: %s",
                    self._provider_label,
                    error,
                )
        with self._lock:
            self._defer_faiss = False
        try:
            self._reopen_indexes()
        except Exception as error:
            self._logger.warning(
                "zimantic: semantic search disabled for %s (%s); falling back "
                "to lexical search",
                self.collection.label,
                public_cause(error),
            )
            self._logger.debug(
                "%s FAISS reopen error: %s", self._provider_label, error
            )

    def _probe_compatible(
        self, db: sqlite3.Connection, db_path: Path
    ) -> tuple[bool, str]:
        rows = dict(
            db.execute(
                "SELECT key, value FROM meta WHERE key IN (?, ?, ?)",
                (
                    MODEL_PROBE_TEXTS_KEY,
                    MODEL_PROBE_VECTORS_KEY,
                    MODEL_PROBE_SHAPE_KEY,
                ),
            ).fetchall()
        )
        if not rows:
            return False, "no compatibility probes"
        ready_getter = self._embedder_ready_getter
        if ready_getter is not None:
            state = ready_getter()
            if (
                isinstance(state, tuple)
                and len(state) == 2
                and state[1]
                and state[0] is not None
            ):
                if not state[0].wait(timeout=60):
                    return False, "timed out waiting for the embedding model"
        getter = self._embedder_getter
        embedder = getter() if getter is not None else None
        if embedder is None:
            return False, "embedding model is unavailable"
        from ..embed import embed_probe_texts, load_model_probes, model_probes_match

        probes = load_model_probes(rows)
        if probes is None:
            return False, "unreadable compatibility probes"
        texts, stored_vectors = probes
        key = tuple(texts)
        with self._probe_cache_lock:
            current = self._probe_cache.get(key)
            if current is None:
                try:
                    current = embed_probe_texts(embedder, texts)
                except Exception as error:
                    return False, f"probe embedding failed: {error}"
                self._probe_cache[key] = current
        try:
            compatible, mean_cosine, min_cosine = model_probes_match(
                texts, stored_vectors, current=current
            )
        except Exception as error:
            return False, f"probe comparison failed: {error}"
        return compatible, (
            f"mean cosine {mean_cosine:.4f}, minimum {min_cosine:.4f}"
        )

    def _can_reuse_index(self, index: Any, source: SourceInfo) -> bool:
        if getattr(index, "generation", None) is None:
            return False
        try:
            lease = IndexLocator(self.index_dir).paths(
                source, verify_faiss=False
            )
        except Exception:
            return False
        if lease is None:
            return False
        try:
            return lease.generation == index.generation
        finally:
            lease.close()

    def _reopen_indexes(self) -> None:
        with self._lock:
            old = dict(self.indexes)
            self.indexes = {}
        try:
            self._refresh_once(wait=True)
        finally:
            with self._lock:
                live = {id(index) for index in self.indexes.values()}
            for index in old.values():
                if id(index) not in live:
                    try:
                        index.close()
                    except Exception:
                        pass

    def random_source(self) -> SourceInfo:
        with self._lock:
            candidates = [
                source.info for source in self._library if source.info.homepage
            ]
        if not candidates:
            raise ArticleNotFoundError(
                f"no {self._provider_label.lower()} source has a homepage"
            )
        return secrets.choice(candidates)

    def health(self) -> list[dict[str, Any]]:
        with self._lock:
            entries = sorted(self.indexes.items())
            sources = {source.info.key: source.info for source in self._library}
        reports: list[dict[str, Any]] = []
        for key, index in entries:
            meta = getattr(index, "index_meta", None) or {}
            done = str(getattr(index, "done", "") or "")
            report: dict[str, Any] = {
                "name": key,
                "status": "ok",
                "index_file": index.db_path is not None,
                "archive": True,
                "done": done,
                "fast": done == "fast",
                "capabilities": {
                    "title": index.db_path is not None,
                    "fulltext": index.db_path is not None and done != "fast",
                    "semantic": index.faiss_index is not None,
                },
            }
            source = sources.get(key)
            if source is not None:
                report["homepage"] = source.homepage
                report["item_count"] = source.item_count
            try:
                for field, decoder in (
                    (schema.INDEXED_COUNT, schema.decode_int),
                    (schema.TRUNCATED, schema.decode_bool),
                    (schema.BODY_TRUNCATED_COUNT, schema.decode_int),
                ):
                    if field in meta:
                        report[field] = decoder(meta[field])
                if schema.DERIVED_TEXT in meta:
                    report["derived_text"] = schema.decode_json(
                        meta[schema.DERIVED_TEXT]
                    )
            except (KeyError, TypeError, ValueError):
                pass
            reports.append(report)
        return reports

    def index_names(self) -> list[str]:
        with self._lock:
            return sorted(self.indexes)

    def _report_stale_overrides(self) -> None:
        operations = self._cfg.get("derived_source_operations") or {}
        prefix = f"{self._provider_label.lower()}:{self.config.name}:"
        with self._lock:
            known = set(self._by_key)
        for key in operations:
            if not str(key).startswith(prefix) or key in known:
                continue
            if key in self._stale_logged:
                continue
            self._stale_logged.add(key)
            self._logger.warning(
                "zimantic: %s collection %s has a stale source override %r; "
                "it was not applied (source keys change after collapse or "
                "recovery; rebuild and update the key)",
                self._provider_label,
                self.collection.label,
                key,
            )


__all__ = [
    "LocalIndexedRuntimeMixin",
    "PreparedLocalIndexes",
    "prepare_local_indexes",
]
