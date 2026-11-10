"""SQLite schema, metadata keys, and the canonical metadata codec.

New indexes use a type-stable ``meta`` table (``value TEXT NOT NULL``) and
``STRICT`` ordinary tables.  FTS5 virtual tables cannot be declared ``STRICT``,
so only ordinary tables carry that modifier.  The body channel is a compact
contentless FTS5 table: it stores document postings without positions, token
counts, or raw derived/native text.

Absent keys mean "not known/not applicable"; no key stores SQL ``NULL``.
Booleans, enums, integers, JSON, fingerprints, and binary probes have exactly
one representation in a new index.
"""

from __future__ import annotations

import base64
import json
import re
import sqlite3
from collections.abc import Iterable, Mapping
from typing import Any

# -- metadata keys ---------------------------------------------------------

DONE = "done"
NEXT = "next"
ENTRY_ORDER = "entry_order"
ENTRY_ORDER_FINGERPRINT = "entry_order_fingerprint"
FILESIZE = "filesize"
BUILD_FILESIZE = "build_filesize"
INDEXED_COUNT = "indexed_count"
BODY_TRUNCATED_COUNT = "body_truncated_count"
SEMANTIC_VECTOR_COUNT = "semantic_vector_count"
SCHEMA_VERSION = "schema_version"
TRUNCATED = "truncated"
BUILD_MODE = "build_mode"
PROVIDER = "provider"
COLLECTION_KEY = "collection_key"
SOURCE_KEY = "source_key"
PROVIDER_ID = "provider_id"
SOURCE_FINGERPRINT = "source_fingerprint"
MODEL_FINGERPRINT = "model_fingerprint"
INDEXED_CHANNELS = "indexed_channels"
MODEL_PROBE_TEXTS = "model_probe_texts"
MODEL_PROBE_SHAPE = "model_probe_shape"
MODEL_PROBE_VECTORS = "model_probe_vectors"
FAISS_DIGEST = "faiss_digest"
BUILD_GENERATION = "build_generation"
BUILD_SCOPE = "build_scope"
DERIVED_TEXT = "derived_text"

CURRENT_SCHEMA_VERSION = 4

META_KEYS = frozenset(
    {
        DONE,
        NEXT,
        ENTRY_ORDER,
        ENTRY_ORDER_FINGERPRINT,
        FILESIZE,
        BUILD_FILESIZE,
        INDEXED_COUNT,
        BODY_TRUNCATED_COUNT,
        SEMANTIC_VECTOR_COUNT,
        SCHEMA_VERSION,
        TRUNCATED,
        BUILD_MODE,
        PROVIDER,
        COLLECTION_KEY,
        SOURCE_KEY,
        PROVIDER_ID,
        SOURCE_FINGERPRINT,
        MODEL_FINGERPRINT,
        INDEXED_CHANNELS,
        MODEL_PROBE_TEXTS,
        MODEL_PROBE_SHAPE,
        MODEL_PROBE_VECTORS,
        FAISS_DIGEST,
        BUILD_GENERATION,
        BUILD_SCOPE,
        DERIVED_TEXT,
    }
)

CHANNEL_TITLE = "title"
CHANNEL_BODY = "body"
CHANNEL_SEMANTIC = "semantic"
INDEXED_CHANNELS_ORDER = (CHANNEL_TITLE, CHANNEL_BODY, CHANNEL_SEMANTIC)

DONE_FAST = "fast"
DONE_FULL = "full"
DONE_VALUES = frozenset({DONE_FAST, DONE_FULL})

DOCS_TOKENIZER = "unicode61 remove_diacritics 2"

SCHEMA = f"""
CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5(
    title, path UNINDEXED, target UNINDEXED, modified UNINDEXED,
    tokenize='{DOCS_TOKENIZER}');
-- docs keeps only title/path/final target.  Excerpts and raw body text are
-- intentionally not stored in the new index.
CREATE VIRTUAL TABLE IF NOT EXISTS body USING fts5(
    text, content='', detail=none, columnsize=0, tokenize='{DOCS_TOKENIZER}');
-- body is contentless: it can add postings, never update/delete a row.
CREATE TABLE IF NOT EXISTS vecs(
    id INTEGER PRIMARY KEY,
    v BLOB NOT NULL
) STRICT;
-- vecs stores float16 vectors and is dropped/emptied once FAISS is written.
CREATE TABLE IF NOT EXISTS meta(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS derived_refs(
    rowid INTEGER NOT NULL,
    operation TEXT NOT NULL,
    contribution_order INTEGER NOT NULL,
    cache_key TEXT NOT NULL,
    PRIMARY KEY (rowid, operation, contribution_order)
) STRICT;
-- derived_refs points at private digest-keyed preview artifacts.  It never
-- stores raw derived text in the searchable database.
"""

# Every table a recognized generation must expose.  ``vecs`` may be empty but
# the table itself is always present so fast and full generations share one
# schema.
EXPECTED_TABLES = frozenset({"docs", "body", "vecs", "meta", "derived_refs"})


class SchemaError(RuntimeError):
    """A published/staged index does not have the recognized schema."""


class Fts5UnavailableError(SchemaError):
    """This SQLite build does not include FTS5."""


# -- canonical codec -------------------------------------------------------


def encode_int(value: int) -> str:
    """Encode a non-negative count/offset as decimal ASCII."""
    number = int(value)
    if isinstance(value, bool) or number < 0:
        raise ValueError("metadata counts must be non-negative integers")
    return str(number)


def decode_int(value: Any) -> int:
    """Decode a canonical decimal integer, rejecting other storage classes."""
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
        raise ValueError(f"not a canonical integer: {value!r}")
    number = int(value)
    if str(number) != value:
        raise ValueError(f"not a canonical integer: {value!r}")
    return number


def encode_bool(value: bool) -> str:
    return "1" if bool(value) else "0"


def decode_bool(value: Any) -> bool:
    if value == "1":
        return True
    if value == "0":
        return False
    raise ValueError(f"not a canonical boolean: {value!r}")


def encode_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def decode_json(value: Any) -> Any:
    if not isinstance(value, str):
        raise TypeError("metadata JSON values must be text")
    return json.loads(value)


def encode_channels(channels: Iterable[str]) -> str:
    selected = {str(channel) for channel in channels}
    unknown = selected - set(INDEXED_CHANNELS_ORDER)
    if unknown:
        raise ValueError(f"unknown index channel(s): {sorted(unknown)}")
    return encode_json(
        [channel for channel in INDEXED_CHANNELS_ORDER if channel in selected]
    )


def decode_channels(value: Any) -> tuple[str, ...]:
    data = decode_json(value)
    if not isinstance(data, list) or any(not isinstance(item, str) for item in data):
        raise ValueError("indexed_channels must be a JSON string array")
    if any(item not in INDEXED_CHANNELS_ORDER for item in data):
        raise ValueError("indexed_channels contains an unknown channel")
    if encode_channels(data) != value:
        raise ValueError("indexed_channels is not canonical")
    return tuple(data)


def encode_bytes(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def decode_bytes(value: Any) -> bytes:
    if not isinstance(value, str):
        raise TypeError("binary metadata values must be text")
    return base64.b64decode(value.encode("ascii"), validate=True)


def encode_fingerprint(value: str) -> str:
    text = str(value)
    if not text or text.lower() != text or any(
        character not in "0123456789abcdef" for character in text
    ):
        raise ValueError("fingerprints are lower-case hexadecimal strings")
    return text


# -- schema helpers --------------------------------------------------------


def _create_probe(connection: sqlite3.Connection, sql: str) -> None:
    connection.execute(sql)
    connection.execute("DROP TABLE IF EXISTS temp.__zimantic_fts_probe")


def ensure_fts5(connection: sqlite3.Connection) -> None:
    """Raise an actionable error when this SQLite build lacks FTS5."""
    try:
        connection.execute(
            "CREATE VIRTUAL TABLE temp.__zimantic_fts_probe USING fts5(x)"
        )
        connection.execute("DROP TABLE temp.__zimantic_fts_probe")
    except sqlite3.Error as error:
        raise Fts5UnavailableError(
            "SQLite FTS5 support is required to build or search an index; "
            "this Python/SQLite build does not include it. Install a SQLite "
            "build with FTS5 enabled and retry."
        ) from error


def ensure_schema(connection: sqlite3.Connection) -> None:
    """Create the canonical schema after probing the required FTS5 feature."""
    ensure_fts5(connection)
    try:
        connection.executescript(SCHEMA)
    except sqlite3.Error as error:
        raise SchemaError(f"could not create the index schema: {error}") from error


def table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        )
    }


def validate_schema(connection: sqlite3.Connection) -> None:
    """Validate a published/staged database before use or publication."""
    tables = table_names(connection)
    missing = EXPECTED_TABLES - tables
    if missing:
        raise SchemaError(
            f"index is missing required table(s): {', '.join(sorted(missing))}"
        )
    definitions = dict(connection.execute(
        "SELECT name, sql FROM sqlite_master WHERE type = 'table'"
    ))

    def normalize(sql: str) -> str:
        return re.sub(r"\s+", "", sql).casefold().replace("ifnotexists", "")

    for statement in SCHEMA.split(";"):
        match = re.search(r"CREATE (?:VIRTUAL )?TABLE IF NOT EXISTS (\w+)", statement)
        if match is None:
            continue
        name = match[1]
        expected = statement[match.start():]
        actual = definitions.get(name, "")
        if normalize(actual) != normalize(expected):
            raise SchemaError(f"index table {name!r} has an unrecognized layout")


def read_meta(connection: sqlite3.Connection) -> dict[str, str]:
    rows = connection.execute("SELECT key, value FROM meta").fetchall()
    result: dict[str, str] = {}
    for key, value in rows:
        if not isinstance(key, str) or not isinstance(value, str):
            raise SchemaError("index metadata contains a non-text value")
        result[key] = value
    return result


def write_meta(connection: sqlite3.Connection, values: Mapping[str, str]) -> None:
    if any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in values.items()
    ):
        raise SchemaError("index metadata keys and values must be text")
    connection.executemany(
        "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
        list(values.items()),
    )


def read_index_meta(
    connection: sqlite3.Connection, *, completed: bool = False
) -> dict[str, str]:
    """Read only the current layout and canonical build/publication metadata."""
    validate_schema(connection)
    meta = read_meta(connection)
    try:
        if decode_int(meta[SCHEMA_VERSION]) != CURRENT_SCHEMA_VERSION:
            raise SchemaError("index schema version is unsupported; rebuild it")
        required = {
            PROVIDER, COLLECTION_KEY, SOURCE_KEY, SOURCE_FINGERPRINT,
            BUILD_GENERATION,
        }
        if completed or DONE in meta:
            required.update({DONE, FILESIZE, INDEXED_COUNT, TRUNCATED, INDEXED_CHANNELS})
            if meta.get(DONE) not in DONE_VALUES:
                raise SchemaError("index is not complete")
        else:
            required.update({BUILD_MODE, BUILD_FILESIZE})
            if meta.get(BUILD_MODE) not in DONE_VALUES:
                raise SchemaError("index build mode is unrecognized")
        missing = required - meta.keys()
        if missing:
            raise SchemaError(
                f"index is missing required metadata: {', '.join(sorted(missing))}"
            )
        if any(not meta[key] for key in (
            PROVIDER, SOURCE_KEY, SOURCE_FINGERPRINT, BUILD_GENERATION
        )):
            raise SchemaError("index identity and fingerprint must not be empty")
        for key in (
            SCHEMA_VERSION, FILESIZE, BUILD_FILESIZE, INDEXED_COUNT, NEXT,
            BODY_TRUNCATED_COUNT, SEMANTIC_VECTOR_COUNT,
        ):
            if key in meta:
                decode_int(meta[key])
        if TRUNCATED in meta:
            decode_bool(meta[TRUNCATED])
        if BUILD_SCOPE in meta:
            coverage = decode_json(meta[BUILD_SCOPE])
            if (
                not isinstance(coverage, dict)
                or set(coverage) != {"paths", "partial"}
                or not isinstance(coverage["partial"], bool)
                or not isinstance(coverage["paths"], list)
                or not coverage["paths"]
                or encode_json(coverage) != meta[BUILD_SCOPE]
            ):
                raise SchemaError("invalid build scope metadata")
            for scope in coverage["paths"]:
                if (
                    not isinstance(scope, dict)
                    or set(scope) != {"path", "recursive"}
                    or not isinstance(scope["path"], str)
                    or not isinstance(scope["recursive"], bool)
                    or "\\" in scope["path"]
                    or scope["path"].startswith("/")
                    or (
                        scope["path"]
                        and any(part in {"", ".", ".."} for part in scope["path"].split("/"))
                    )
                    or (not scope["path"] and not scope["recursive"])
                ):
                    raise SchemaError("invalid build scope path")
            if coverage["partial"] and meta.get(TRUNCATED) != encode_bool(True):
                raise SchemaError("partial build scope must be marked truncated")
        if INDEXED_CHANNELS in meta:
            channels = decode_channels(meta[INDEXED_CHANNELS])
            if DONE in meta and CHANNEL_SEMANTIC in channels:
                decode_int(meta[SEMANTIC_VECTOR_COUNT])
            if DONE in meta and CHANNEL_BODY in channels:
                decode_int(meta[BODY_TRUNCATED_COUNT])
        if DERIVED_TEXT in meta:
            derived = decode_json(meta[DERIVED_TEXT])
            if not isinstance(derived, dict) or encode_json(derived) != meta[DERIVED_TEXT]:
                raise SchemaError("derived metadata must be a canonical JSON object")
        if MODEL_FINGERPRINT in meta and not meta[MODEL_FINGERPRINT]:
            raise SchemaError("model fingerprint must not be empty")
        if decode_int(meta.get(SEMANTIC_VECTOR_COUNT, "0")) and (
            not meta.get(MODEL_FINGERPRINT) or not meta.get(FAISS_DIGEST)
        ):
            raise SchemaError("semantic vectors require a model fingerprint and digest")
        if FAISS_DIGEST in meta:
            encode_fingerprint(meta[FAISS_DIGEST])
        probe_keys = {MODEL_PROBE_TEXTS, MODEL_PROBE_SHAPE, MODEL_PROBE_VECTORS}
        if probe_keys & meta.keys():
            texts = decode_json(meta[MODEL_PROBE_TEXTS])
            shape = re.fullmatch(r"([1-9][0-9]*)x([1-9][0-9]*)", meta[MODEL_PROBE_SHAPE])
            vectors = decode_bytes(meta[MODEL_PROBE_VECTORS])
            if (
                not isinstance(texts, list)
                or any(not isinstance(text, str) for text in texts)
                or encode_json(texts) != meta[MODEL_PROBE_TEXTS]
                or shape is None
                or len(texts) != int(shape[1])
                or len(vectors) != int(shape[1]) * int(shape[2]) * 4
            ):
                raise SchemaError("invalid model probe metadata")
    except (KeyError, TypeError, ValueError) as error:
        raise SchemaError(f"invalid index metadata: {error}") from error
    return meta


def delete_meta(connection: sqlite3.Connection, keys: Iterable[str]) -> None:
    connection.executemany(
        "DELETE FROM meta WHERE key = ?", [(str(key),) for key in keys]
    )


def schema_version(connection: sqlite3.Connection) -> int | None:
    row = connection.execute(
        "SELECT value FROM meta WHERE key = ?", (SCHEMA_VERSION,)
    ).fetchone()
    return decode_int(row[0]) if row is not None else None


def done_mode(connection: sqlite3.Connection) -> str | None:
    row = connection.execute(
        "SELECT value FROM meta WHERE key = ?", (DONE,)
    ).fetchone()
    if row is None:
        return None
    value = row[0]
    if value not in DONE_VALUES:
        raise SchemaError(f"unrecognized completion marker: {value!r}")
    return str(value)


__all__ = [
    "BODY_TRUNCATED_COUNT",
    "BUILD_FILESIZE",
    "BUILD_GENERATION",
    "BUILD_MODE",
    "BUILD_SCOPE",
    "CHANNEL_BODY",
    "CHANNEL_SEMANTIC",
    "CHANNEL_TITLE",
    "COLLECTION_KEY",
    "CURRENT_SCHEMA_VERSION",
    "DERIVED_TEXT",
    "DONE",
    "DONE_FAST",
    "DONE_FULL",
    "DONE_VALUES",
    "ENTRY_ORDER",
    "ENTRY_ORDER_FINGERPRINT",
    "EXPECTED_TABLES",
    "FAISS_DIGEST",
    "FILESIZE",
    "INDEXED_CHANNELS",
    "INDEXED_CHANNELS_ORDER",
    "INDEXED_COUNT",
    "META_KEYS",
    "MODEL_FINGERPRINT",
    "MODEL_PROBE_SHAPE",
    "MODEL_PROBE_TEXTS",
    "MODEL_PROBE_VECTORS",
    "NEXT",
    "PROVIDER",
    "PROVIDER_ID",
    "SCHEMA",
    "SCHEMA_VERSION",
    "SEMANTIC_VECTOR_COUNT",
    "SOURCE_FINGERPRINT",
    "SOURCE_KEY",
    "TRUNCATED",
    "Fts5UnavailableError",
    "SchemaError",
    "decode_bool",
    "decode_bytes",
    "decode_channels",
    "decode_int",
    "decode_json",
    "delete_meta",
    "done_mode",
    "encode_bool",
    "encode_bytes",
    "encode_channels",
    "encode_fingerprint",
    "encode_int",
    "encode_json",
    "ensure_fts5",
    "ensure_schema",
    "read_index_meta",
    "read_meta",
    "schema_version",
    "table_names",
    "validate_schema",
    "write_meta",
]
