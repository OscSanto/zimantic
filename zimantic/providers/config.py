"""Normalized provider configuration and the minimal ``[[collections]]`` parser.

The parser in this module is the only collection parser.  It turns the TOML
array-of-tables shape into typed provider entries and keeps provider-specific
validation (and provider modules) out of the core settings loader.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal
from urllib.parse import urlsplit

from ..scoped_settings import FILTER_KEYS, SCOPED_KEYS, parse_scoped_settings
from ..settings import KNOWN_KEYS

ProviderKind = Literal["zim", "kiwix", "files", "kolibri"]
Granularity = Literal["collection", "children"]
DerivedOperation = Literal["off", "index", "tesseract", "whisper"]
WorkerCount = Literal["auto"] | int

SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
RESERVED_ZIM_NAMES = frozenset({"open"})

BUILTIN_PROVIDERS: tuple[ProviderKind, ...] = ("zim", "kiwix", "files", "kolibri")


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


@dataclass(frozen=True)
class CollectionDerivedTextConfig:
    captions: Literal["off", "index"] = "off"
    ocr: Literal["off", "tesseract"] = "off"
    asr: Literal["off", "whisper"] = "off"
    options: Mapping[str, Any] = field(default_factory=dict)


def _derived_text(value: Any) -> CollectionDerivedTextConfig | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ConfigValidationError("derived_text must be a table")
    from ..indexing.derived import DERIVED_TEXT_KEYS, parse_derived_options

    unknown = sorted(set(value) - {"captions", "ocr", "asr"} - DERIVED_TEXT_KEYS)
    if unknown:
        raise ConfigValidationError(
            "unknown derived_text key(s): " + ", ".join(unknown)
        )
    captions = value.get("captions", "off")
    ocr = value.get("ocr", "off")
    asr = value.get("asr", "off")
    if not isinstance(captions, str) or captions not in {"off", "index"}:
        raise ConfigValidationError("derived_text.captions accepts 'off' or 'index'")
    if not isinstance(ocr, str) or ocr not in {"off", "tesseract"}:
        raise ConfigValidationError("derived_text.ocr accepts 'off' or 'tesseract'")
    if not isinstance(asr, str) or asr not in {"off", "whisper"}:
        raise ConfigValidationError("derived_text.asr accepts 'off' or 'whisper'")
    try:
        options = parse_derived_options(
            {name: item for name, item in value.items() if name in DERIVED_TEXT_KEYS}
        )
    except (TypeError, ValueError) as error:
        raise ConfigValidationError(f"derived_text: {error}") from error
    return CollectionDerivedTextConfig(
        captions=captions, ocr=ocr, asr=asr, options=options
    )


def _traversal(entry: Mapping[str, Any], name: str) -> TraversalLimits:
    table = entry.get("traversal", {})
    if not isinstance(table, Mapping):
        raise ConfigValidationError(f"collections[{name!r}].traversal must be a table")
    unknown = sorted(set(table) - {"max_visited_entries", "traversal_timeout_seconds"})
    if unknown:
        raise ConfigValidationError(
            "unknown traversal key(s): " + ", ".join(unknown)
        )
    return TraversalLimits(**{
        key: _positive_int(value, f"{name}.traversal.{key}")
        for key, value in table.items()
    })


@dataclass(frozen=True)
class ZimConfig:
    name: str
    path: str
    label: str | None = None
    settings: Mapping[str, Any] = field(default_factory=dict)
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
    settings: Mapping[str, Any] = field(default_factory=dict)
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
    settings: Mapping[str, Any] = field(default_factory=dict)
    derived_text: CollectionDerivedTextConfig | None = None
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
    settings: Mapping[str, Any] = field(default_factory=dict)
    derived_text: CollectionDerivedTextConfig | None = None
    granularity: Granularity = "children"
    database_path: str | None = None
    storage: str | None = None
    server_url: str | None = None
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
        if not self.path:
            raise ConfigValidationError("kolibri path must not be empty")
        if self.server_url is not None:
            parsed = urlsplit(self.server_url)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise ConfigValidationError(
                    "kolibri server_url must be an http(s) URL"
                )
        if self.granularity not in ("collection", "children"):
            raise ConfigValidationError("granularity must be collection or children")
        for name in (
            "max_snapshot_bytes",
            "snapshot_disk_budget_bytes",
            "snapshot_timeout_seconds",
            "max_html5_zip_entries",
            "max_html5_zip_directory_bytes",
            "max_html5_zip_member_bytes",
            "max_html5_zip_total_bytes",
            "html5_zip_timeout_seconds",
        ):
            _positive_int(getattr(self, name), name)
        if self.snapshot_disk_budget_bytes < self.max_snapshot_bytes:
            raise ConfigValidationError(
                "snapshot_disk_budget_bytes must be at least max_snapshot_bytes"
            )
        for name in ("max_sources", "max_entries"):
            value = getattr(self, name)
            if value is not None:
                _positive_int(value, name)
        for name in ("path", "database_path", "storage"):
            value = getattr(self, name)
            if value is None:
                continue
            if not isinstance(value, str) or not value.strip():
                raise ConfigValidationError(f"{name} must be a non-empty string")
            if (
                re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", value) or value.startswith(("//", "\\\\"))
            ):
                raise ConfigValidationError(
                    f"{name} must be a local filesystem path"
                )


ProviderConfig = ZimConfig | KiwixConfig | FilesConfig | KolibriConfig

# Provider-owned defaults.  A ``None`` value in a typed entry means "use the
# provider default"; exposing the values here keeps ``sources`` diagnostics and
# the CLI from inventing a second set of defaults.
PROVIDER_DEFAULTS: Mapping[ProviderKind, Mapping[str, Any]] = MappingProxyType(
    {
        "zim": MappingProxyType(
            {
                "max_entries": None,
                "max_visited_entries": 1_000_000,
                "traversal_timeout_seconds": 300,
            }
        ),
        "kiwix": MappingProxyType(
            {
                "catalog_limit": 1_000,
                "max_catalog_entries": 50_000,
                "max_response_bytes": 64 << 20,
                "max_remote_sources": 100,
            }
        ),
        "files": MappingProxyType(
            {
                "granularity": "collection",
                "max_sources": 64,
                "max_entries": 100_000,
                "max_file_bytes": 256 << 20,
                "max_visited_entries": 1_000_000,
                "traversal_timeout_seconds": 300,
            }
        ),
        "kolibri": MappingProxyType(
            {
                "granularity": "children",
                "max_sources": 64,
                "max_entries": 500_000,
                "max_snapshot_bytes": 1_073_741_824,
                "snapshot_disk_budget_bytes": 3_221_225_472,
                "snapshot_timeout_seconds": 10,
                "max_html5_zip_entries": 10_000,
                "max_html5_zip_directory_bytes": 8_388_608,
                "max_html5_zip_member_bytes": 268_435_456,
                "max_html5_zip_total_bytes": 1_073_741_824,
                "html5_zip_timeout_seconds": 10,
            }
        ),
    }
)


@dataclass(frozen=True)
class ConfigIssue:
    """A provider entry that could not be normalized.

    ``namespace`` is the collection key that can be formed for the entry, or
    ``None`` when the entry is missing a usable type/name.  Prune uses
    ``provider_wide`` to retain every potentially affected index namespace
    instead of treating an invalid entry as if it configured nothing.
    """

    index: int
    message: str
    kind: str | None = None
    name: str | None = None
    namespace: str | None = None
    provider_wide: bool = False


@dataclass(frozen=True)
class RuntimeConfig:
    """Global typed settings plus normalized configured provider entries."""

    settings: Mapping[str, Any] = field(default_factory=dict)
    collections: tuple[ProviderConfig, ...] = ()
    config_path: Path | None = None
    config_dir: Path = field(default_factory=Path.cwd)
    search_timeout_seconds: float = 45.0
    page_size: int = 10
    envelope: Mapping[str, Any] = field(default_factory=dict)
    diagnostics: tuple[str, ...] = ()
    issues: tuple[ConfigIssue, ...] = ()
    config_present: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "settings", MappingProxyType(dict(self.settings)))
        object.__setattr__(
            self, "envelope", MappingProxyType(dict(self.envelope))
        )

    def get(self, key: str, default: Any = None) -> Any:
        return self.settings.get(key, default)


@dataclass(frozen=True)
class ParsedCollections:
    collections: tuple[ProviderConfig, ...]
    diagnostics: tuple[str, ...]
    envelope: Mapping[str, Any]
    issues: tuple[ConfigIssue, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "envelope", MappingProxyType(dict(self.envelope))
        )


def _resolve_path(value: Any, name: str, base: Path) -> str:
    raw = _string(value, name)
    assert raw is not None
    if (
        re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", raw) or raw.startswith(("//", "\\\\"))
    ):
        raise ConfigValidationError(f"{name} must be a local filesystem path")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = base / path
    # Normalize once at discovery/config time so every later operation sees the
    # same resolved provider path (relative segments, symlinked configs, and
    # saved ad-hoc selections included).
    try:
        return str(path.resolve())
    except OSError:
        return str(path.absolute())


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
    try:
        settings = parse_scoped_settings(entry)
        if entry.get("type") != "files" and settings.keys() & FILTER_KEYS:
            raise ValueError("index_include/index_exclude are supported only by Files")
    except ValueError as error:
        raise ConfigValidationError(str(error)) from error
    return {"name": name, "label": label, "traversal": traversal, "settings": settings}


def _parse_zim(entry: Mapping[str, Any], base: Path) -> ZimConfig:
    if "derived_text" in entry:
        raise ConfigValidationError(
            "ZIM collections do not support derived_text"
        )
    common = _common(entry)
    return ZimConfig(
        path=_resolve_path(entry.get("path"), "path", base),
        max_entries=_optional_positive_int(entry.get("max_entries"), "max_entries"),
        **common,
    )


def _parse_kiwix(entry: Mapping[str, Any], base: Path) -> KiwixConfig:
    if "derived_text" in entry:
        raise ConfigValidationError(
            "Kiwix collections do not support derived_text"
        )
    common = _common(entry)
    defaults = PROVIDER_DEFAULTS["kiwix"]
    return KiwixConfig(
        server=_server(entry.get("server"), "server"),
        catalog_path=_catalog_path(entry.get("catalog_path"), "catalog_path"),
        catalog_limit=_positive_int(
            entry.get("catalog_limit", defaults["catalog_limit"]), "catalog_limit"
        ),
        max_catalog_entries=_positive_int(
            entry.get("max_catalog_entries", defaults["max_catalog_entries"]),
            "max_catalog_entries",
        ),
        max_response_bytes=_positive_int(
            entry.get("max_response_bytes", defaults["max_response_bytes"]),
            "max_response_bytes",
        ),
        max_remote_sources=_positive_int(
            entry.get("max_remote_sources", defaults["max_remote_sources"]),
            "max_remote_sources",
            allow_zero=True,
        ),
        **common,
    )


_COMMON_ENTRY_KEYS = SCOPED_KEYS | frozenset(
    {
        "type",
        "name",
        "label",
        "derived_text",
        "traversal",
        "sources",
    }
)

_ZIM_KEYS = _COMMON_ENTRY_KEYS | {"path", "max_entries"}

_KIWIX_KEYS = _COMMON_ENTRY_KEYS | {
    "server",
    "catalog_path",
    "catalog_limit",
    "max_catalog_entries",
    "max_response_bytes",
    "max_remote_sources",
}

_FILES_KEYS = SCOPED_KEYS | frozenset(
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
        "derived_text",
        "traversal",
        "sources",
    }
)


def _parse_files(entry: Mapping[str, Any], base: Path) -> FilesConfig:
    common = _common(entry)
    defaults = PROVIDER_DEFAULTS["files"]
    granularity = entry.get("granularity", defaults["granularity"])
    if granularity not in ("collection", "children"):
        raise ConfigValidationError("granularity must be collection or children")
    return FilesConfig(
        path=_resolve_path(entry.get("path"), "path", base),
        granularity=granularity,
        include=_pattern_tuple(entry.get("include"), "include"),
        exclude=_pattern_tuple(entry.get("exclude"), "exclude"),
        max_sources=_positive_int(
            entry.get("max_sources", defaults["max_sources"]), "max_sources"
        ),
        max_entries=_positive_int(
            entry.get("max_entries", defaults["max_entries"]), "max_entries"
        ),
        max_file_bytes=_positive_int(
            entry.get("max_file_bytes", defaults["max_file_bytes"]),
            "max_file_bytes",
        ),
        derived_text=_derived_text(entry.get("derived_text")),
        **common,
    )


_KOLIBRI_KEYS = SCOPED_KEYS | frozenset(
    {
        "type",
        "name",
        "label",
        "path",
        "granularity",
        "database_path",
        "storage",
        "server_url",
        "max_sources",
        "max_entries",
        "derived_text",
        "max_snapshot_bytes",
        "snapshot_disk_budget_bytes",
        "snapshot_timeout_seconds",
        "max_html5_zip_entries",
        "max_html5_zip_directory_bytes",
        "max_html5_zip_member_bytes",
        "max_html5_zip_total_bytes",
        "html5_zip_timeout_seconds",
        "traversal",
        "sources",
    }
)


def _optional_path(value: Any, name: str, base: Path) -> str | None:
    if value is None:
        return None
    return _resolve_path(value, name, base)


def _parse_kolibri(entry: Mapping[str, Any], base: Path) -> KolibriConfig:
    common = _common(entry)
    defaults = PROVIDER_DEFAULTS["kolibri"]
    granularity = entry.get("granularity", defaults["granularity"])
    if granularity not in ("collection", "children"):
        raise ConfigValidationError("granularity must be collection or children")
    return KolibriConfig(
        path=_resolve_path(entry.get("path"), "path", base),
        granularity=granularity,
        database_path=_optional_path(
            entry.get("database_path"), "database_path", base
        ),
        storage=_optional_path(entry.get("storage"), "storage", base),
        server_url=_string(entry.get("server_url"), "server_url", optional=True),
        max_sources=_positive_int(
            entry.get("max_sources", defaults["max_sources"]), "max_sources"
        ),
        max_entries=_positive_int(
            entry.get("max_entries", defaults["max_entries"]), "max_entries"
        ),
        max_snapshot_bytes=_positive_int(
            entry.get("max_snapshot_bytes", defaults["max_snapshot_bytes"]),
            "max_snapshot_bytes",
        ),
        snapshot_disk_budget_bytes=_positive_int(
            entry.get(
                "snapshot_disk_budget_bytes",
                defaults["snapshot_disk_budget_bytes"],
            ),
            "snapshot_disk_budget_bytes",
        ),
        snapshot_timeout_seconds=_positive_int(
            entry.get("snapshot_timeout_seconds", defaults["snapshot_timeout_seconds"]),
            "snapshot_timeout_seconds",
        ),
        max_html5_zip_entries=_positive_int(
            entry.get("max_html5_zip_entries", defaults["max_html5_zip_entries"]),
            "max_html5_zip_entries",
        ),
        max_html5_zip_directory_bytes=_positive_int(
            entry.get(
                "max_html5_zip_directory_bytes",
                defaults["max_html5_zip_directory_bytes"],
            ),
            "max_html5_zip_directory_bytes",
        ),
        max_html5_zip_member_bytes=_positive_int(
            entry.get(
                "max_html5_zip_member_bytes",
                defaults["max_html5_zip_member_bytes"],
            ),
            "max_html5_zip_member_bytes",
        ),
        max_html5_zip_total_bytes=_positive_int(
            entry.get(
                "max_html5_zip_total_bytes",
                defaults["max_html5_zip_total_bytes"],
            ),
            "max_html5_zip_total_bytes",
        ),
        html5_zip_timeout_seconds=_positive_int(
            entry.get(
                "html5_zip_timeout_seconds",
                defaults["html5_zip_timeout_seconds"],
            ),
            "html5_zip_timeout_seconds",
        ),
        derived_text=_derived_text(entry.get("derived_text")),
        **common,
    )


@dataclass(frozen=True)
class ProviderConfigSpec:
    """One in-tree provider's configuration contract.

    Generic validation (``type``, ``name``, duplicate names) lives in
    :func:`parse_collections`; the provider-owned parser and key set live here
    so adding a provider does not add a branch to the generic loader.
    """

    kind: ProviderKind
    parse: Callable[[Mapping[str, Any], Path], ProviderConfig]
    allowed_keys: frozenset[str]
    defaults: Mapping[str, Any]


_PROVIDER_CONFIG_SPECS: Mapping[ProviderKind, ProviderConfigSpec] = MappingProxyType(
    {
        "zim": ProviderConfigSpec(
            kind="zim",
            parse=_parse_zim,
            allowed_keys=_ZIM_KEYS,
            defaults=PROVIDER_DEFAULTS["zim"],
        ),
        "kiwix": ProviderConfigSpec(
            kind="kiwix",
            parse=_parse_kiwix,
            allowed_keys=_KIWIX_KEYS,
            defaults=PROVIDER_DEFAULTS["kiwix"],
        ),
        "files": ProviderConfigSpec(
            kind="files",
            parse=_parse_files,
            allowed_keys=_FILES_KEYS,
            defaults=PROVIDER_DEFAULTS["files"],
        ),
        "kolibri": ProviderConfigSpec(
            kind="kolibri",
            parse=_parse_kolibri,
            allowed_keys=_KOLIBRI_KEYS,
            defaults=PROVIDER_DEFAULTS["kolibri"],
        ),
    }
)


PROVIDER_CONFIG_SPECS: Mapping[ProviderKind, ProviderConfigSpec] = (
    _PROVIDER_CONFIG_SPECS
)


def provider_config_specs() -> Mapping[ProviderKind, ProviderConfigSpec]:
    """Return the per-provider configuration registry in stable order."""
    return _PROVIDER_CONFIG_SPECS


def get_provider_config_spec(kind: str) -> ProviderConfigSpec:
    try:
        return _PROVIDER_CONFIG_SPECS[kind]  # type: ignore[index]
    except KeyError as error:
        raise ConfigValidationError(
            f"unknown provider type {kind!r}; expected one of "
            f"{', '.join(_PROVIDER_CONFIG_SPECS)}"
        ) from error


def provider_config_defaults(kind: str) -> Mapping[str, Any]:
    return get_provider_config_spec(kind).defaults


def _entry_namespace(kind: str | None, name: str | None) -> str | None:
    if not kind or not name:
        return None
    return f"{kind}:{name}"


def parse_collections(
    data: Mapping[str, Any],
    *,
    config_dir: Path | str,
    warn=None,
) -> ParsedCollections:
    base = Path(config_dir)
    unknown = sorted(set(data) - KNOWN_KEYS - {"collections", "derived_text"})
    if unknown:
        raise ConfigValidationError("unknown config key(s): " + ", ".join(unknown))

    raw = data.get("collections", [])
    if raw is None:
        raw = []
    if not isinstance(raw, (list, tuple)):
        raise ConfigValidationError("collections must be an array of tables")

    diagnostics: list[str] = []
    issues: list[ConfigIssue] = []
    names: dict[str, int] = {}
    normalized: list[tuple[int, ProviderConfig]] = []
    disabled: set[int] = set()
    envelope: dict[str, Any] = {}

    for index, entry in enumerate(raw):
        if not isinstance(entry, Mapping):
            message = f"collection #{index + 1} is not a table"
            diagnostics.append(message)
            issues.append(
                ConfigIssue(index=index, message=message, provider_wide=True)
            )
            continue
        kind = entry.get("type")
        if kind not in BUILTIN_PROVIDERS:
            message = (
                f"collection #{index + 1} has invalid type {kind!r}; expected one "
                f"of {', '.join(BUILTIN_PROVIDERS)}"
            )
            diagnostics.append(message)
            issues.append(
                ConfigIssue(
                    index=index,
                    message=message,
                    kind=str(kind) if isinstance(kind, str) else None,
                    provider_wide=True,
                )
            )
            continue
        spec = get_provider_config_spec(str(kind))
        try:
            name = validate_name(entry.get("name"))
        except ConfigValidationError as error:
            message = f"collection #{index + 1}: {error}"
            diagnostics.append(message)
            issues.append(
                ConfigIssue(
                    index=index,
                    message=message,
                    kind=str(kind),
                    provider_wide=True,
                )
            )
            continue
        names[name] = names.get(name, 0) + 1
        try:
            unknown = sorted(set(entry) - spec.allowed_keys)
            if unknown:
                raise ConfigValidationError(
                    "unknown key(s): " + ", ".join(unknown)
                )
            parsed = spec.parse(entry, base)
            if "sources" in entry and spec.kind in {"zim", "kiwix"}:
                from ..indexing.derived import DERIVED_ORDER, DERIVED_TEXT_KEYS

                derived_keys = set(DERIVED_ORDER) | DERIVED_TEXT_KEYS
                for override in entry["sources"]:
                    if (
                        isinstance(override, Mapping)
                        and set(override) & derived_keys
                    ):
                        raise ConfigValidationError(
                            f"{spec.kind} collections do not support "
                            "derived-text source overrides"
                        )
        except ConfigValidationError as error:
            message = f"collection {name!r}: {error}"
            diagnostics.append(message)
            issues.append(
                ConfigIssue(
                    index=index,
                    message=message,
                    kind=str(kind),
                    name=name,
                    namespace=_entry_namespace(str(kind), name),
                )
            )
            continue
        if "sources" in entry:
            envelope.setdefault("sources", {})[name] = entry["sources"]
        normalized.append((index, parsed))

    duplicate_names = {name for name, count in names.items() if count > 1}
    if duplicate_names:
        for name in sorted(duplicate_names):
            message = (
                f"collection name {name!r} is configured more than once; "
                "all entries with that name are disabled"
            )
            diagnostics.append(message)
            matching = [
                (index, entry)
                for index, entry in normalized
                if entry.name == name
            ]
            if not matching:
                issues.append(
                    ConfigIssue(
                        index=-1,
                        message=message,
                        name=name,
                        provider_wide=True,
                    )
                )
            for index, entry in matching:
                issues.append(
                    ConfigIssue(
                        index=index,
                        message=message,
                        kind=entry.kind,
                        name=name,
                        namespace=_entry_namespace(entry.kind, name),
                    )
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
        issues=tuple(issues),
    )


__all__ = [
    "BUILTIN_PROVIDERS",
    "PROVIDER_CONFIG_SPECS",
    "PROVIDER_DEFAULTS",
    "SAFE_NAME",
    "CollectionDerivedTextConfig",
    "ConfigIssue",
    "ConfigValidationError",
    "DerivedOperation",
    "FilesConfig",
    "Granularity",
    "KiwixConfig",
    "KolibriConfig",
    "ParsedCollections",
    "ProviderConfig",
    "ProviderConfigSpec",
    "ProviderKind",
    "RuntimeConfig",
    "TraversalLimits",
    "WorkerCount",
    "ZimConfig",
    "get_provider_config_spec",
    "parse_collections",
    "provider_config_defaults",
    "provider_config_specs",
    "validate_name",
]
