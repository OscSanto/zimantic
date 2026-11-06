"""Normalized provider configuration and the minimal ``[[collections]]`` parser.

The parser in this module is the only collection parser.  It turns the TOML
array-of-tables shape into typed provider entries and keeps provider-specific
validation (and provider modules) out of the core settings loader.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Mapping

ProviderKind = Literal["zim", "kiwix", "files", "kolibri"]
Granularity = Literal["collection", "children"]
DerivedOperation = Literal["off", "index", "tesseract", "whisper"]
WorkerCount = Literal["auto"] | int

SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
RESERVED_ZIM_NAMES = frozenset({"open"})

BUILTIN_PROVIDERS: tuple[ProviderKind, ...] = ("zim", "kiwix", "files", "kolibri")

# Flat provider keys from the pre-collections schema.  They are fatal so an old
# configuration cannot silently start an empty library.
REMOVED_KEYS: dict[str, str] = {
    "zim_dir": (
        "the flat zim_dir setting was removed; declare a [[collections]] "
        'entry with type = "zim" and path = "..."'
    ),
    "kiwix_server": (
        "the flat kiwix_server setting was removed; declare a [[collections]] "
        'entry with type = "kiwix" and server = "..."'
    ),
    "kiwix_catalog_path": (
        "kiwix_catalog_path is now catalog_path on a Kiwix [[collections]] entry"
    ),
    "kiwix_catalog_limit": (
        "kiwix_catalog_limit is now catalog_limit on a Kiwix [[collections]] entry"
    ),
    "max_kiwix_response_bytes": (
        "max_kiwix_response_bytes is now max_response_bytes on a Kiwix "
        "[[collections]] entry"
    ),
    "max_remote_sources": (
        "the global max_remote_sources setting was removed; set "
        "max_remote_sources per Kiwix [[collections]] entry"
    ),
}

# Keys that belong to the normalized settings envelope rather than a provider.
ENVELOPE_KEYS = frozenset({"derived_text", "sources"})


class ConfigValidationError(ValueError):
    """A configured value cannot be normalized safely."""


def _require_plain_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigValidationError(f"{name} must be an integer")
    return value


def _positive_int(value: Any, name: str, *, allow_zero: bool = False) -> int:
    number = _require_plain_int(value, name)
    minimum = 0 if allow_zero else 1
    if number < minimum:
        raise ConfigValidationError(f"{name} must be at least {minimum}")
    return number


def _optional_positive_int(value: Any, name: str, *, allow_zero: bool = False) -> int | None:
    if value is None:
        return None
    return _positive_int(value, name, allow_zero=allow_zero)


def _string(value: Any, name: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigValidationError(f"{name} must be a non-empty string")
    return value.strip()


def _string_tuple(value: Any, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ConfigValidationError(f"{name} must be a list of strings")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ConfigValidationError(f"{name} must be a list of strings")
        result.append(item)
    return tuple(result)


def _pattern_tuple(value: Any, name: str) -> tuple[str, ...]:
    patterns = _string_tuple(value, name)
    if any(not pattern.strip() for pattern in patterns):
        raise ConfigValidationError(f"{name} patterns must not be empty")
    return tuple(pattern.strip() for pattern in patterns)


def validate_name(name: Any) -> str:
    if not isinstance(name, str) or not SAFE_NAME.fullmatch(name):
        raise ConfigValidationError(
            "name must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}"
        )
    return name


@dataclass(frozen=True)
class TraversalLimits:
    max_visited_entries: int = 1_000_000
    traversal_timeout_seconds: int = 300

    def __post_init__(self) -> None:
        _positive_int(self.max_visited_entries, "max_visited_entries")
        _positive_int(self.traversal_timeout_seconds, "traversal_timeout_seconds")


def _traversal(entry: Mapping[str, Any], name: str) -> TraversalLimits:
    table = entry.get("traversal")
    if table is not None and not isinstance(table, Mapping):
        raise ConfigValidationError(f"collections[{name!r}].traversal must be a table")
    source = dict(table or {})
    # Flat bounds normalize into the traversal object.
    for key in ("max_visited_entries", "traversal_timeout_seconds"):
        if key in entry:
            if key in source:
                raise ConfigValidationError(
                    f"collections[{name!r}] sets {key} both flat and in traversal"
                )
            source[key] = entry[key]
    values: dict[str, int] = {}
    for key in ("max_visited_entries", "traversal_timeout_seconds"):
        if key in source:
            values[key] = _positive_int(source[key], f"{name}.{key}")
    return TraversalLimits(**values)


@dataclass(frozen=True)
class ZimConfig:
    name: str
    path: str
    label: str | None = None
    max_entries: int | None = None
    traversal: TraversalLimits = field(default_factory=TraversalLimits)
    kind: Literal["zim"] = field(default="zim", init=False)

    def __post_init__(self) -> None:
        validate_name(self.name)
        if self.name in RESERVED_ZIM_NAMES:
            raise ConfigValidationError(
                f"the ZIM collection name {self.name!r} is reserved"
            )
        if not self.path:
            raise ConfigValidationError("zim path must not be empty")


@dataclass(frozen=True)
class KiwixConfig:
    name: str
    server: str
    label: str | None = None
    catalog_path: str | None = None
    catalog_limit: int | None = None
    max_catalog_entries: int | None = None
    max_response_bytes: int | None = None
    max_remote_sources: int | None = None
    traversal: TraversalLimits = field(default_factory=TraversalLimits)
    kind: Literal["kiwix"] = field(default="kiwix", init=False)

    def __post_init__(self) -> None:
        validate_name(self.name)
        if not self.server:
            raise ConfigValidationError("kiwix server must not be empty")


@dataclass(frozen=True)
class FilesConfig:
    name: str
    path: str
    label: str | None = None
    granularity: Granularity = "collection"
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    max_sources: int | None = None
    max_entries: int | None = None
    max_file_bytes: int | None = None
    traversal: TraversalLimits = field(default_factory=TraversalLimits)
    kind: Literal["files"] = field(default="files", init=False)

    def __post_init__(self) -> None:
        validate_name(self.name)
        if not self.path:
            raise ConfigValidationError("files path must not be empty")
        if self.granularity not in ("collection", "children"):
            raise ConfigValidationError("granularity must be collection or children")
        patterns = (*self.include, *self.exclude)
        if any(
            not isinstance(pattern, str) or not pattern.strip()
            for pattern in patterns
        ):
            raise ConfigValidationError(
                "files include/exclude patterns must be non-empty strings"
            )


@dataclass(frozen=True)
class KolibriConfig:
    name: str
    path: str
    label: str | None = None
    granularity: Granularity = "children"
    database_path: str | None = None
    storage: str | None = None
    max_sources: int | None = None
    max_entries: int | None = None
    max_snapshot_bytes: int = 1_073_741_824
    snapshot_disk_budget_bytes: int = 3_221_225_472
    snapshot_timeout_seconds: int = 10
    max_html5_zip_entries: int = 10_000
    max_html5_zip_directory_bytes: int = 8_388_608
    max_html5_zip_member_bytes: int = 268_435_456
    max_html5_zip_total_bytes: int = 1_073_741_824
    html5_zip_timeout_seconds: int = 10
    traversal: TraversalLimits = field(default_factory=TraversalLimits)
    kind: Literal["kolibri"] = field(default="kolibri", init=False)

    def __post_init__(self) -> None:
        validate_name(self.name)
        if self.granularity not in ("collection", "children"):
            raise ConfigValidationError("granularity must be collection or children")


ProviderConfig = ZimConfig | KiwixConfig | FilesConfig | KolibriConfig


@dataclass(frozen=True)
class RuntimeConfig:
    """Global typed settings plus normalized configured provider entries."""

    settings: Mapping[str, Any] = field(default_factory=dict)
    collections: tuple[ProviderConfig, ...] = ()
    config_path: Path | None = None
    config_dir: Path = field(default_factory=Path.cwd)
    search_timeout_seconds: float = 30.0
    page_size: int = 10
    envelope: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "settings", MappingProxyType(dict(self.settings))
        )
        object.__setattr__(
            self, "envelope", MappingProxyType(dict(self.envelope))
        )

    def get(self, key: str, default: Any = None) -> Any:
        return self.settings.get(key, default)

    def collections_of(self, kind: ProviderKind) -> tuple[ProviderConfig, ...]:
        return tuple(entry for entry in self.collections if entry.kind == kind)


@dataclass(frozen=True)
class ParsedCollections:
    collections: tuple[ProviderConfig, ...]
    diagnostics: tuple[str, ...]
    envelope: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "envelope", MappingProxyType(dict(self.envelope))
        )


def _resolve_path(value: Any, name: str, base: Path) -> str:
    raw = _string(value, name)
    assert raw is not None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = base / path
    return str(path)


def _catalog_path(value: Any, name: str) -> str | None:
    if value is None:
        return None
    raw = _string(value, name)
    assert raw is not None
    # The path is appended to the configured server base; it must not supply a
    # scheme, authority, or fragment.
    if (
        re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", raw)
        or raw.startswith("//")
        or "#" in raw
    ):
        raise ConfigValidationError(
            f"{name} must be a relative or root-relative path without a "
            "scheme, authority, or fragment"
        )
    return raw


def _server(value: Any, name: str) -> str:
    raw = _string(value, name)
    assert raw is not None
    from urllib.parse import urlsplit

    parts = urlsplit(raw)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ConfigValidationError(f"{name} must be an absolute HTTP(S) URL")
    if parts.username or parts.password:
        raise ConfigValidationError(f"{name} must not contain credentials")
    if parts.query or parts.fragment:
        raise ConfigValidationError(f"{name} must not contain a query or fragment")
    return raw.rstrip("/")


def _common(entry: Mapping[str, Any]) -> dict[str, Any]:
    name = validate_name(entry.get("name"))
    label = _string(entry.get("label"), "label", optional=True)
    traversal = _traversal(entry, name)
    return {"name": name, "label": label, "traversal": traversal}


def _parse_zim(entry: Mapping[str, Any], base: Path) -> ZimConfig:
    common = _common(entry)
    return ZimConfig(
        path=_resolve_path(entry.get("path"), "path", base),
        max_entries=_optional_positive_int(entry.get("max_entries"), "max_entries"),
        **common,
    )


def _parse_kiwix(entry: Mapping[str, Any], base: Path) -> KiwixConfig:
    common = _common(entry)
    return KiwixConfig(
        server=_server(entry.get("server"), "server"),
        catalog_path=_catalog_path(entry.get("catalog_path"), "catalog_path"),
        catalog_limit=_optional_positive_int(entry.get("catalog_limit"), "catalog_limit"),
        max_catalog_entries=_optional_positive_int(
            entry.get("max_catalog_entries"), "max_catalog_entries"
        ),
        max_response_bytes=_optional_positive_int(
            entry.get("max_response_bytes"), "max_response_bytes"
        ),
        max_remote_sources=_optional_positive_int(
            entry.get("max_remote_sources"), "max_remote_sources", allow_zero=True
        ),
        **common,
    )


_FILES_KEYS = frozenset(
    {
        "type",
        "name",
        "label",
        "path",
        "granularity",
        "include",
        "exclude",
        "max_sources",
        "max_entries",
        "max_file_bytes",
        "traversal",
        "max_visited_entries",
        "traversal_timeout_seconds",
        "sources",
    }
)


def _parse_files(entry: Mapping[str, Any], base: Path) -> FilesConfig:
    common = _common(entry)
    granularity = entry.get("granularity", "collection")
    if granularity not in ("collection", "children"):
        raise ConfigValidationError("granularity must be collection or children")
    return FilesConfig(
        path=_resolve_path(entry.get("path"), "path", base),
        granularity=granularity,
        include=_pattern_tuple(entry.get("include"), "include"),
        exclude=_pattern_tuple(entry.get("exclude"), "exclude"),
        max_sources=_optional_positive_int(entry.get("max_sources"), "max_sources"),
        max_entries=_optional_positive_int(entry.get("max_entries"), "max_entries"),
        max_file_bytes=_optional_positive_int(
            entry.get("max_file_bytes"), "max_file_bytes"
        ),
        **common,
    )


def parse_collections(
    data: Mapping[str, Any],
    *,
    config_dir: Path | str,
    warn=None,
    allow_disabled_providers: bool = True,
) -> ParsedCollections:
    base = Path(config_dir)
    for key in REMOVED_KEYS:
        if key in data:
            raise ConfigValidationError(f"{key}: {REMOVED_KEYS[key]}")

    raw = data.get("collections", [])
    if raw is None:
        raw = []
    if not isinstance(raw, (list, tuple)):
        raise ConfigValidationError("collections must be an array of tables")

    diagnostics: list[str] = []
    names: dict[str, int] = {}
    normalized: list[tuple[int, ProviderConfig]] = []
    disabled: set[int] = set()
    envelope: dict[str, Any] = {}

    for index, entry in enumerate(raw):
        if not isinstance(entry, Mapping):
            diagnostics.append(f"collection #{index + 1} is not a table")
            continue
        kind = entry.get("type")
        if kind not in BUILTIN_PROVIDERS:
            diagnostics.append(
                f"collection #{index + 1} has invalid type {kind!r}; expected one "
                f"of {', '.join(BUILTIN_PROVIDERS)}"
            )
            continue
        try:
            name = validate_name(entry.get("name"))
        except ConfigValidationError as error:
            diagnostics.append(f"collection #{index + 1}: {error}")
            continue
        names[name] = names.get(name, 0) + 1
        if kind == "kolibri":
            if allow_disabled_providers:
                diagnostics.append(
                    f"collection {name!r}: provider {kind!r} is not supported yet"
                )
            continue
        try:
            if kind == "zim":
                parsed = _parse_zim(entry, base)
            elif kind == "files":
                unknown = sorted(set(entry) - _FILES_KEYS)
                if unknown:
                    diagnostics.append(
                        f"collection {name!r}: unknown key(s) ignored: "
                        + ", ".join(unknown)
                    )
                parsed = _parse_files(entry, base)
            else:
                parsed = _parse_kiwix(entry, base)
        except ConfigValidationError as error:
            diagnostics.append(f"collection {name!r}: {error}")
            continue
        if "sources" in entry:
            envelope.setdefault("sources", {})[name] = entry["sources"]
        normalized.append((index, parsed))

    duplicate_names = {name for name, count in names.items() if count > 1}
    if duplicate_names:
        for name in sorted(duplicate_names):
            diagnostics.append(
                f"collection name {name!r} is configured more than once; "
                "all entries with that name are disabled"
            )
        disabled.update(
            index for index, entry in normalized if entry.name in duplicate_names
        )

    if "derived_text" in data:
        envelope["derived_text"] = data["derived_text"]

    if warn is not None:
        for message in diagnostics:
            warn(f"zimantic: warning: {message}")

    collections = tuple(
        entry for index, entry in normalized if index not in disabled
    )
    return ParsedCollections(
        collections=collections,
        diagnostics=tuple(diagnostics),
        envelope=envelope,
    )


# Convenience aliases for callers that speak in terms of the normalized entry
# set rather than the TOML table name.
parse_provider_config = parse_collections
normalize_collections = parse_collections


__all__ = [
    "BUILTIN_PROVIDERS",
    "ConfigValidationError",
    "DerivedOperation",
    "FilesConfig",
    "Granularity",
    "KiwixConfig",
    "KolibriConfig",
    "ParsedCollections",
    "ProviderConfig",
    "ProviderKind",
    "REMOVED_KEYS",
    "RuntimeConfig",
    "SAFE_NAME",
    "TraversalLimits",
    "WorkerCount",
    "ZimConfig",
    "normalize_collections",
    "parse_collections",
    "parse_provider_config",
    "validate_name",
]
