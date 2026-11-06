"""One-time, allowlisted migration of recognized pre-redesign index files.

Migration never modifies the legacy file.  It inspects actual table/column
definitions and storage classes, preserves canonical rowids and compatible
lexical data, and only adopts artifacts whose build-time provenance can be
established independently of the current catalog or filename.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from . import schema
from .locator import IndexLocator


class MigrationRejected(RuntimeError):
    """The artifact is not a recognized, provenance-backed legacy index."""


@dataclass(frozen=True)
class MigrationResult:
    adopted: bool
    sqlite_path: Path | None
    reason: str
    rows_reused: int = 0
    channels: tuple[str, ...] = ()
    done: str | None = None


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, str):
        return value
    return str(value)


def _canonical_done(value: Any) -> str:
    """Map recognized legacy completion markers; reject everything else."""
    if isinstance(value, bool):
        raise MigrationRejected("boolean completion marker is not recognized")
    if isinstance(value, int):
        return schema.DONE_FULL if value == 1 else _reject_done(value)
    text = _text(value).strip()
    if text == "1":
        return schema.DONE_FULL
    if text == "fast":
        return schema.DONE_FAST
    return _reject_done(value)


def _reject_done(value: Any) -> str:
    raise MigrationRejected(f"unrecognized legacy completion marker {value!r}")


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {
            str(row[1])
            for row in connection.execute(f"PRAGMA table_info({table})")
        }
    except sqlite3.Error:
        return set()


def _provenance_matches(
    meta: Mapping[str, Any],
    *,
    provenance: Mapping[str, Any] | None,
    source_size: int | None,
    source_uuid: str | None,
) -> bool:
    """Require recognized build-time identity evidence.

    A size-only legacy row is explicitly not enough: a freshly rebuilt catalog
    or an unrelated archive with the same size must never supply provenance.
    """
    candidates: list[Mapping[str, Any]] = []
    if provenance:
        candidates.append(provenance)
    raw = meta.get("source_provenance") or meta.get("provenance")
    if isinstance(raw, str) and raw:
        import json

        try:
            parsed = json.loads(raw)
        except ValueError:
            return False
        if isinstance(parsed, dict):
            candidates.append(parsed)
    for candidate in candidates:
        recorded_size = candidate.get("source_size", candidate.get("size"))
        recorded_uuid = candidate.get("source_uuid", candidate.get("uuid"))
        if recorded_size is None or recorded_uuid is None:
            continue
        try:
            matches_size = source_size is None or int(recorded_size) == int(source_size)
        except (TypeError, ValueError):
            continue
        if not matches_size:
            continue
        if source_uuid is None:
            return False
        if str(recorded_uuid).casefold() == str(source_uuid).casefold():
            return True
    # Recognized phase-00/01 generation metadata can carry explicit identity.
    recorded_size = meta.get("source_size")
    recorded_uuid = meta.get("source_uuid") or meta.get("uuid")
    if recorded_size is not None and recorded_uuid is not None:
        try:
            matches_size = (
                source_size is None or int(recorded_size) == int(source_size)
            )
        except (TypeError, ValueError):
            return False
        return bool(
            matches_size
            and source_uuid is not None
            and str(recorded_uuid).casefold() == str(source_uuid).casefold()
        )
    return False


def _resolve_targets(
    connection: sqlite3.Connection, rows: list[tuple[int, str, str, int | None, str]]
) -> dict[int, tuple[int | None, str]]:
    """Resolve redirect chains to one final row id/path or reject cycles."""
    by_id = {row[0]: row for row in rows}
    resolved: dict[int, tuple[int | None, str]] = {}
    for rowid, _title, path, target, _excerpt in rows:
        current = rowid
        seen: set[int] = set()
        final_target = target
        final_id = target
        final_path = path
        while final_target is not None:
            if current in seen or len(seen) > 32:
                raise MigrationRejected("cyclic redirect chain")
            seen.add(current)
            target_row = by_id.get(int(final_target))
            if target_row is None:
                raise MigrationRejected(
                    f"redirect target row {final_target} is not present"
                )
            final_id = int(final_target)
            final_path = target_row[2]
            final_target = target_row[3]
            current = final_id
        resolved[rowid] = (
            None if target is None else final_id,
            final_path,
        )
    return resolved


def migrate_legacy_index(
    legacy_sqlite: Path | str,
    destination: Path | str | None = None,
    *,
    source_size: int | None = None,
    source_uuid: str | None = None,
    provenance: Mapping[str, Any] | None = None,
    provider: str = "zim",
    collection_key: str = "",
    source_key: str = "",
    provider_id: str | None = None,
) -> MigrationResult:
    """Adopt one recognized legacy SQLite index into the canonical schema.

    Returns a result whose ``sqlite_path`` is a new staged file.  The legacy
    file and any legacy FAISS sidecar are left untouched regardless of outcome.
    """
    source_path = Path(legacy_sqlite)
    if not source_path.is_file():
        return MigrationResult(False, None, "legacy index does not exist")
    try:
        connection = sqlite3.connect(source_path)
    except sqlite3.Error as error:
        return MigrationResult(False, None, f"cannot open legacy index: {error}")
    try:
        meta = dict(connection.execute("SELECT key, value FROM meta").fetchall())
        if not meta:
            return MigrationResult(False, None, "legacy index has no metadata")
        if "done" not in meta:
            return MigrationResult(
                False, None, "legacy index has no completion marker"
            )
        try:
            done = _canonical_done(meta["done"])
        except MigrationRejected as error:
            return MigrationResult(False, None, str(error))
        columns = _columns(connection, "docs")
        if not {"title", "path", "target"}.issubset(columns):
            return MigrationResult(
                False, None, "legacy docs table is not a recognized layout"
            )
        if not _provenance_matches(
            meta,
            provenance=provenance,
            source_size=source_size,
            source_uuid=source_uuid,
        ):
            return MigrationResult(
                False,
                None,
                "legacy index does not carry independently verifiable "
                "build-time source provenance",
            )
        excerpt_select = ", excerpt" if "excerpt" in columns else ", ''"
        rows: list[tuple[int, str, str, int | None, str]] = []
        try:
            for rowid, title, path, target, excerpt in connection.execute(
                "SELECT rowid, title, path, target" + excerpt_select + " FROM docs"
            ):
                if int(rowid) < 0:
                    return MigrationResult(
                        False, None, "legacy index contains a negative row id"
                    )
                rows.append(
                    (
                        int(rowid),
                        _text(title),
                        _text(path),
                        None if target is None else int(target),
                        _text(excerpt) if excerpt is not None else "",
                    )
                )
        except (sqlite3.Error, TypeError, ValueError) as error:
            return MigrationResult(
                False, None, f"legacy docs rows are unreadable: {error}"
            )
        try:
            resolved = _resolve_targets(connection, rows)
        except MigrationRejected as error:
            return MigrationResult(False, None, str(error))
        rowids = [row[0] for row in rows]
        if len(set(rowids)) != len(rowids):
            return MigrationResult(False, None, "legacy row ids collide")
    finally:
        connection.close()

    target_path = (
        Path(destination)
        if destination is not None
        else source_path.with_name(f"{source_path.stem}.migrated.sqlite")
    )
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.unlink(missing_ok=True)
    destination_connection = sqlite3.connect(target_path)
    try:
        schema.ensure_schema(destination_connection)
        channels = [schema.CHANNEL_TITLE]
        with destination_connection:
            for rowid, title, path, target, excerpt in rows:
                final_target, final_path = resolved[rowid]
                destination_connection.execute(
                    "INSERT INTO docs(rowid, title, path, target) VALUES (?, ?, ?, ?)",
                    (rowid, title, final_path, final_target),
                )
                if excerpt:
                    destination_connection.execute(
                        "INSERT INTO body(rowid, text) VALUES (?, ?)",
                        (rowid, excerpt),
                    )
            if any(row[4] for row in rows):
                channels.append(schema.CHANNEL_BODY)
            values = {
                schema.DONE: done,
                schema.INDEXED_COUNT: schema.encode_int(len(rows)),
                schema.SCHEMA_VERSION: schema.encode_int(
                    schema.CURRENT_SCHEMA_VERSION
                ),
                schema.BUILD_GENERATION: str(uuid.uuid4()),
                schema.PROVIDER: provider,
                schema.COLLECTION_KEY: collection_key,
                schema.SOURCE_KEY: source_key,
                schema.INDEXED_CHANNELS: schema.encode_channels(channels),
                schema.TRUNCATED: schema.encode_bool(False),
            }
            if source_size is not None:
                values[schema.FILESIZE] = schema.encode_int(source_size)
            if provider_id:
                values[schema.PROVIDER_ID] = provider_id
            schema.write_meta(destination_connection, values)
        destination_connection.execute("VACUUM")
        destination_connection.commit()
        schema.validate_schema(destination_connection)
    except Exception as error:
        destination_connection.close()
        target_path.unlink(missing_ok=True)
        return MigrationResult(False, None, f"migration failed: {error}")
    finally:
        try:
            destination_connection.close()
        except sqlite3.Error:
            pass
    return MigrationResult(
        adopted=True,
        sqlite_path=target_path,
        reason="recognized legacy schema with valid provenance",
        rows_reused=len(rows),
        channels=tuple(channels),
        done=done,
    )


def adopt_legacy_generation(
    legacy_sqlite: Path | str,
    locator: IndexLocator,
    source: Any,
    *,
    source_size: int | None = None,
    source_uuid: str | None = None,
    provenance: Mapping[str, Any] | None = None,
) -> MigrationResult:
    """Migrate and publish a recognized legacy index under the canonical locator."""
    source_key = str(getattr(source, "key", "") or "")
    provider = str(getattr(source, "provider", "") or "")
    collection_key = str(getattr(source, "collection_key", "") or "")
    result = migrate_legacy_index(
        legacy_sqlite,
        source_size=source_size,
        source_uuid=source_uuid,
        provenance=provenance,
        provider=provider,
        collection_key=collection_key,
        source_key=source_key,
        provider_id=getattr(source, "provider_id", None),
    )
    if not result.adopted or result.sqlite_path is None:
        return result
    lease = locator.publish(
        source,
        result.sqlite_path,
        None,
        metadata={
            "schema_version": schema.CURRENT_SCHEMA_VERSION,
            "migrated_from": Path(legacy_sqlite).name,
        },
    )
    lease.close()
    result.sqlite_path.unlink(missing_ok=True)
    return result


__all__ = [
    "MigrationRejected",
    "MigrationResult",
    "adopt_legacy_generation",
    "migrate_legacy_index",
]
