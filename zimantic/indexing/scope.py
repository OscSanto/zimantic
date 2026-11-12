"""Cumulative, source-relative coverage for positional builds."""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path

from . import schema


def _covers(scope: dict, relative: str) -> bool:
    path = scope["path"]
    return relative == path or (
        scope["recursive"] and (not path or relative.startswith(path + "/"))
    )


def _compact(scopes: list[dict]) -> list[dict]:
    result: list[dict] = []
    exact: set[str] = set()
    recursive: set[str] = set()
    for scope in sorted(scopes, key=lambda value: (value["path"], not value["recursive"])):
        if not _covered_path(scope["path"], exact, recursive):
            result.append(scope)
            exact.add(scope["path"])
            if scope["recursive"]:
                recursive.add(scope["path"])
    return result


def _covered_path(relative: str, exact: set[str], recursive: set[str]) -> bool:
    if relative in exact or "" in recursive:
        return True
    parts = relative.split("/")
    return any("/".join(parts[:end]) in recursive for end in range(1, len(parts) + 1))


def requested_scope(producer, paths: list[Path]) -> list[dict]:
    root = producer.path_scope_root.resolve()
    scopes = []
    for path in paths:
        target = path.expanduser().resolve()
        if target in getattr(producer, "path_scope_aliases", ()):
            return [{"path": "", "recursive": True}]
        if target == root or root.is_relative_to(target):
            return [{"path": "", "recursive": True}]
        try:
            relative = target.relative_to(root).as_posix()
        except ValueError:
            continue
        scopes.append({"path": relative, "recursive": target.is_dir()})
    return _compact(scopes)


def matching_indices(producer, scopes: list[dict]) -> list[int]:
    exact = {scope["path"] for scope in scopes}
    recursive = {scope["path"] for scope in scopes if scope["recursive"]}
    return [
        index for index in range(producer.count)
        if any(
            _covered_path(relative, exact, recursive)
            for relative in producer.entry_paths(index)
        )
    ]


def scope_matches_source(producer, scopes: list[dict]) -> bool:
    return any(
        _covers(scope, producer.path_scope_membership)
        for scope in scopes
    ) or bool(matching_indices(producer, scopes))


class ScopedEntryProducer:
    """Rebuild the covered union, leaving the previous generation immutable."""

    def __init__(self, producer, scopes: list[dict]) -> None:
        self._producer = producer
        self._indices = matching_indices(producer, scopes)
        self.count = len(self._indices)
        partial = self.count != producer.count or (
            producer.truncated
            and not any(
                _covers(scope, producer.path_scope_membership) for scope in scopes
            )
        )
        self.build_scope = {"paths": scopes, "partial": partial}
        self.truncated = bool(producer.truncated or partial)
        self.fingerprint = hashlib.sha256(
            (producer.fingerprint + schema.encode_json(self.build_scope)).encode("utf-8")
        ).hexdigest()

    def __getattr__(self, name):
        return getattr(self._producer, name)

    def entry_at(self, index: int):
        if index < 0 or index >= self.count:
            return None
        return self._producer.entry_at(self._indices[index])

    def entry_id_at(self, index: int) -> int:
        return self._producer.entry_id_at(self._indices[index])

    def parallel_reader(
        self, *, workers: int, start: int = 0, chunk_size: int = 16
    ):
        factory = getattr(self._producer, "parallel_reader", None)
        if not callable(factory):
            return None
        return factory(
            workers=workers, start=start, chunk_size=chunk_size, view=self
        )

    def derived_text_inputs(self, index: int):
        if index < 0 or index >= self.count:
            return ()
        return self._producer.derived_text_inputs(self._indices[index])


def restrict_producer(producer, paths: list[Path], locator):
    scopes = requested_scope(producer, paths)
    matches = scope_matches_source(producer, scopes)
    if any(_covers(scope, producer.path_scope_membership) for scope in scopes):
        return producer

    lease = locator.paths(producer.source)
    if lease is not None:
        try:
            with closing(sqlite3.connect(
                f"{lease.sqlite_path.as_uri()}?mode=ro&immutable=1", uri=True
            )) as db:
                meta = schema.read_index_meta(db, completed=True)
                previous = meta.get(schema.BUILD_SCOPE)
                if previous is None:
                    if not matches and producer.provider == "files":
                        matches = any(
                            _covers(scope, row[0])
                            for row in db.execute("SELECT path FROM docs")
                            for scope in scopes
                        )
                    if matches:
                        # Never replace a complete source with a subset.
                        return producer
                else:
                    covered = schema.decode_json(previous)["paths"]
                    matches = matches or any(
                        _covers(old, new["path"]) or _covers(new, old["path"])
                        for old in covered for new in scopes
                    )
                    scopes = _compact([*covered, *scopes])
        except (schema.SchemaError, sqlite3.Error):
            pass
        finally:
            lease.close()
    if not matches:
        producer.close()
        return None
    return ScopedEntryProducer(producer, scopes)
