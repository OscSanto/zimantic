"""Canonical optional derived-text enrichment policy and shared stage.

Native extraction stays in the existing readers.  This module adds the
provider-neutral captions/OCR/ASR stage: independent opt-ins, local-only
engines, a digest-keyed private cache, provenance, bounded workers, and a fixed
composition order.  It is imported only by build/reporting code, never by the
serve search path.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import multiprocessing
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import unicodedata
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .types import DerivedTextInput, DerivedTextProducer

logger = logging.getLogger(__name__)

OPERATION_OFF = "off"
OPERATION_NOT_CHECKED = "not_checked"
OPERATION_READY = "ready"
OPERATION_PARTIAL = "partial"
OPERATION_UNAVAILABLE = "unavailable"
OPERATION_STATES = frozenset(
    {
        OPERATION_OFF,
        OPERATION_NOT_CHECKED,
        OPERATION_READY,
        OPERATION_PARTIAL,
        OPERATION_UNAVAILABLE,
    }
)

DERIVED_ORDER = ("captions", "ocr", "asr")
_CAPTION_MIMETYPES = frozenset(
    {
        "application/x-subrip",
        "application/srt",
        "text/srt",
        "text/vtt",
        "text/webvtt",
        "application/ass",
        "text/x-ass",
        "text/x-ssa",
    }
)
_WHITESPACE = re.compile(r"\s+")


def _cap_text(text: str, max_bytes: int) -> str:
    encoded = str(text).encode("utf-8", "ignore")
    limit = max(1, int(max_bytes))
    if len(encoded) <= limit:
        return str(text)
    return encoded[:limit].decode("utf-8", "ignore")


def normalize_for_equality(text: str) -> str:
    """NFKC, casefold, and collapse whitespace for duplicate detection only."""
    return _WHITESPACE.sub(" ", unicodedata.normalize("NFKC", str(text)).casefold()).strip()


def compose_body(
    native_text: str,
    contributions: Mapping[str, str] | Iterable[tuple[str, str]],
    max_body_text_bytes: int,
) -> tuple[str, bool, tuple[str, ...]]:
    """Retain native text, then append captions/OCR/ASR in fixed order.

    Distinct contributions are compared only by their normalized form; the
    original text is preserved when appended.  Repetition *within* one
    contribution is never removed.  The combined body cap is applied at a UTF-8
    character boundary.
    """
    items = contributions.items() if isinstance(contributions, Mapping) else contributions
    by_operation: dict[str, list[str]] = {name: [] for name in DERIVED_ORDER}
    for operation, text in items:
        if operation in by_operation and str(text).strip():
            by_operation[operation].append(str(text))
    pieces = [str(native_text)] if str(native_text).strip() else []
    seen = {normalize_for_equality(native_text)} if pieces else set()
    used: list[str] = []
    for operation in DERIVED_ORDER:
        appended = False
        for text in by_operation[operation]:
            normalized = normalize_for_equality(text)
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            pieces.append(text)
            appended = True
        if appended:
            used.append(operation)
    combined = "\n\n".join(pieces)
    encoded = combined.encode("utf-8", "ignore")
    limit = max(1, int(max_body_text_bytes))
    if len(encoded) <= limit:
        return combined, False, tuple(used)
    text = encoded[:limit].decode("utf-8", "ignore")
    return text, True, tuple(used)


_CAPTION_TAG = re.compile(r"<[^>]+>")


def parse_caption_bytes(data: bytes, **_options: Any) -> str:
    """Parse a SubRip or WebVTT sidecar into plain, normalized text.

    This is the built-in in-process captions engine.  It never decodes media,
    never runs a model, and is intentionally permissive: malformed cues are
    ignored rather than failing the whole build.
    """
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("utf-16", "ignore")
    lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        upper = line.upper()
        if upper.startswith("WEBVTT"):
            continue
        if "-->" in line:
            continue
        if line.isdigit():
            continue
        if upper.startswith(("NOTE ", "STYLE", "REGION")):
            continue
        line = _CAPTION_TAG.sub(" ", line)
        line = html.unescape(line)
        normalized = " ".join(line.split())
        if normalized:
            lines.append(normalized)
    return " ".join(" ".join(lines).split())


# -- configuration ---------------------------------------------------------


ASR_ENGINES = (
    "auto",
    "whisper.cpp",
    "faster-whisper",
    "openai-whisper",
)


@dataclass(frozen=True)
class DerivedTextConfig:
    max_caption_bytes: int = 1 << 20
    ocr_languages: tuple[str, ...] = ("eng",)
    max_ocr_pages: int = 20
    max_ocr_pixels: int = 50_000_000
    max_ocr_output_bytes: int = 256 << 10
    asr_engine: str = "auto"
    asr_model: str = "base.en"
    asr_threads: int = 0
    asr_batched: bool = False
    max_asr_seconds: int = 3600
    max_asr_bytes: int = 1 << 30
    max_asr_output_bytes: int = 1 << 20
    task_timeout_seconds: int = 7200
    max_spool_bytes: int = 2 << 30
    workers: int | str = "auto"
    cache: bool = True

    def __post_init__(self) -> None:
        for name in (
            "max_caption_bytes",
            "max_ocr_pages",
            "max_ocr_pixels",
            "max_ocr_output_bytes",
            "max_asr_seconds",
            "max_asr_bytes",
            "max_asr_output_bytes",
            "task_timeout_seconds",
            "max_spool_bytes",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.workers != "auto" and (
            isinstance(self.workers, bool)
            or not isinstance(self.workers, int)
            or self.workers < 1
        ):
            raise ValueError("workers must be 'auto' or a positive integer")
        if self.asr_engine not in ASR_ENGINES:
            raise ValueError(
                f"asr_engine must be one of {', '.join(ASR_ENGINES)}"
            )
        if (
            isinstance(self.asr_threads, bool)
            or not isinstance(self.asr_threads, int)
            or self.asr_threads < 0
        ):
            raise ValueError("asr_threads must be zero or a positive integer")
        if not isinstance(self.asr_batched, bool):
            raise ValueError("asr_batched must be true or false")
        if not self.asr_model:
            raise ValueError("asr_model must not be empty")
        if not self.ocr_languages:
            raise ValueError("ocr_languages must not be empty")


_DERIVED_KEYS = frozenset(DerivedTextConfig.__dataclass_fields__)


def parse_derived_text(data: Mapping[str, Any] | None) -> DerivedTextConfig:
    """Validate the normalized global ``[derived_text]`` schema."""
    if data is None:
        return DerivedTextConfig()
    if not isinstance(data, Mapping):
        raise ValueError("[derived_text] must be a table")
    values = dict(data)
    unknown = sorted(set(values) - _DERIVED_KEYS)
    if unknown:
        logger.warning(
            "zimantic: warning: unknown [derived_text] key(s) ignored: %s",
            ", ".join(unknown),
        )
        for key in unknown:
            values.pop(key, None)
    languages = values.get("ocr_languages")
    if languages is not None:
        if not isinstance(languages, (list, tuple)) or not all(
            isinstance(item, str) and item.strip() for item in languages
        ):
            raise ValueError("ocr_languages must be a non-empty string list")
        values["ocr_languages"] = tuple(str(item).strip() for item in languages)
    return DerivedTextConfig(**values)


@dataclass(frozen=True)
class SourceDerivedOperations:
    captions: bool = False
    ocr: str | None = None
    asr: str | None = None

    def state(self) -> dict[str, str]:
        return {
            "captions": "index" if self.captions else OPERATION_OFF,
            "ocr": self.ocr or OPERATION_OFF,
            "asr": self.asr or OPERATION_OFF,
        }

    @property
    def any_enabled(self) -> bool:
        return self.captions or bool(self.ocr) or bool(self.asr)


def parse_source_override(
    entry: Mapping[str, Any],
    *,
    collection_name: str | None = None,
) -> tuple[str, SourceDerivedOperations]:
    """Validate one exact-source ``[[collections.sources]]`` override."""
    if not isinstance(entry, Mapping):
        raise ValueError("source override must be a table")
    key = entry.get("key")
    if not isinstance(key, str) or not key.strip():
        raise ValueError("source override requires an exact non-empty key")
    if collection_name is not None and f":{collection_name}:" not in key:
        raise ValueError(
            f"source override {key!r} does not belong to collection "
            f"{collection_name!r}"
        )
    unknown = sorted(set(entry) - {"key", "captions", "ocr", "asr"})
    if unknown:
        raise ValueError(f"unknown source override key(s): {', '.join(unknown)}")
    captions = entry.get("captions", "off")
    if captions not in {"off", "index"}:
        raise ValueError("captions accepts only 'off' or 'index'")
    ocr = entry.get("ocr", "off")
    asr = entry.get("asr", "off")
    if ocr not in {"off", "tesseract"}:
        raise ValueError("ocr accepts only 'off' or 'tesseract'")
    if asr not in {"off", "whisper"}:
        raise ValueError("asr accepts only 'off' or 'whisper'")
    return str(key), SourceDerivedOperations(
        captions=captions == "index",
        ocr=None if ocr == "off" else "tesseract",
        asr=None if asr == "off" else "whisper",
    )


def parse_source_overrides(
    collections: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None,
    *,
    collection_names: Iterable[str] = (),
    warn: Callable[[str], None] | None = None,
) -> dict[str, SourceDerivedOperations]:
    """Normalize per-source overrides, rejecting conflicting duplicates.

    Stale-key diagnostics must be deferred until discovery has completed, so an
    unknown key is retained here rather than being reported as an error during
    config loading.
    """
    names = set(collection_names)
    result: dict[str, SourceDerivedOperations] = {}
    configured_keys: set[str] = set()
    if isinstance(collections, Mapping):
        for collection_name, values in collections.items():
            if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
                continue
            for entry in values:
                if not isinstance(entry, Mapping):
                    continue
                key, operations = parse_source_override(
                    entry, collection_name=str(collection_name)
                )
                if key in configured_keys:
                    raise ValueError(
                        f"conflicting duplicate source override for {key!r}"
                    )
                configured_keys.add(key)
                result[key] = operations
        return result
    entries: list[Mapping[str, Any]] = []
    if collections:
        entries.extend(item for item in collections if isinstance(item, Mapping))
    for entry in entries:
        key, operations = parse_source_override(entry)
        if key in configured_keys:
            raise ValueError(f"conflicting duplicate source override for {key!r}")
        configured_keys.add(key)
        result[key] = operations
    return result


# -- cache -----------------------------------------------------------------


class DerivedTextCache:
    """Private, digest-keyed cache for complete derived outputs.

    Entries are keyed by content digest, operation, engine/model/version, and
    every output-affecting option.  Partial/failed results are never cached.
    """

    def __init__(self, root: Path | str, max_bytes: int = 512 << 20) -> None:
        self.root = Path(root)
        self.max_bytes = max(0, int(max_bytes))
        self._bytes = 0
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError:
            pass

    def _key_path(
        self,
        digest: str,
        operation: str,
        engine: str,
        options: Mapping[str, Any],
    ) -> Path:
        material = json.dumps(
            {
                "digest": digest,
                "operation": operation,
                "engine": engine,
                "options": dict(options),
            },
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        key = hashlib.sha256(material).hexdigest()
        return self.root / key[:2] / f"{key}.txt"

    def get(
        self,
        digest: str,
        operation: str,
        engine: str,
        options: Mapping[str, Any],
    ) -> str | None:
        path = self._key_path(digest, operation, engine, options)
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None

    def put(
        self,
        digest: str,
        operation: str,
        engine: str,
        options: Mapping[str, Any],
        text: str,
    ) -> Path | None:
        if not text or self.max_bytes <= 0:
            return None
        data = text.encode("utf-8")
        if self._bytes + len(data) > self.max_bytes:
            return None
        path = self._key_path(digest, operation, engine, options)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except OSError:
            temporary.unlink(missing_ok=True)
            return None
        self._bytes += len(data)
        return path

    def prune(self, keep: Iterable[Path] = ()) -> int:
        keep_paths = {Path(path) for path in keep}
        removed = 0
        if not self.root.is_dir():
            return removed
        for path in self.root.rglob("*"):
            if path.is_file() and path not in keep_paths:
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    continue
        self._bytes = 0
        return removed


# -- cached preview reads --------------------------------------------------

_CACHE_FILE_NAME = re.compile(r"^[0-9a-f]{64}\.txt$")


def read_cached_text(cache_root: Path | str, cache_key: str) -> str:
    """Read one complete derived-text cache entry, or return an empty string."""

    name = str(cache_key or "")
    if not _CACHE_FILE_NAME.fullmatch(name):
        return ""
    path = Path(cache_root) / name[:2] / name
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""


def derived_text_preview(
    index: Any,
    cache_root: Path | str,
    path: str,
) -> str:
    """Return the best cached captions/OCR/ASR text for one indexed file path."""

    db = None
    if index is not None:
        reader = getattr(index, "db_for_thread", None)
        if callable(reader):
            try:
                db = reader()
            except Exception:
                db = None
        if db is None:
            db = getattr(index, "db", None)
    if db is None:
        return ""
    try:
        rows = db.execute(
            "SELECT r.cache_key FROM docs AS d "
            "JOIN derived_refs AS r ON r.rowid = d.rowid "
            "WHERE d.path = ? "
            "ORDER BY CASE r.operation "
            "WHEN 'captions' THEN 0 WHEN 'ocr' THEN 1 WHEN 'asr' THEN 2 "
            "ELSE 3 END, r.contribution_order",
            (path,),
        ).fetchall()
    except sqlite3.Error:
        return ""
    for row in rows:
        text = read_cached_text(cache_root, str(row[0] if row else ""))
        if text.strip():
            return text
    return ""


# -- worker resolution and spooling ---------------------------------------


def _available_memory_bytes() -> int | None:
    try:
        with open("/proc/meminfo", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError):
        pass
    try:
        pages = os.sysconf("SC_AVPHYS_PAGES")
        size = os.sysconf("SC_PAGE_SIZE")
        return int(pages) * int(size)
    except (AttributeError, OSError, ValueError):
        return None


def resolve_workers(
    configured: int | str,
    *,
    usable_cores: int | None = None,
    available_memory: int | None = None,
    per_worker_memory: int = 512 << 20,
    headroom: int = 1,
) -> tuple[int, str | None]:
    """Resolve ``workers = "auto"`` from CPU and currently available memory.

    Returns ``(0, None)`` when no worker fits; callers report the operation
    unavailable rather than starting it and risking OOM.
    """
    if isinstance(configured, int) and not isinstance(configured, bool):
        if configured < 1:
            return 0, None
        return configured, "explicit"
    cores = max(1, int(usable_cores or os.cpu_count() or 1))
    cpu_limit = max(1, cores - max(0, int(headroom)))
    memory = available_memory
    if memory is None:
        memory = _available_memory_bytes()
    if memory is None:
        return cpu_limit, "cpu"
    memory_limit = max(0, int(memory) // max(1, int(per_worker_memory)))
    if memory_limit <= 0:
        return 0, "memory"
    workers = min(cpu_limit, memory_limit)
    return workers, ("cpu" if cpu_limit <= memory_limit else "memory")


def spool_input(
    input: DerivedTextInput,
    *,
    max_bytes: int,
    directory: Path | str | None = None,
) -> tuple[Path, str, int]:
    """Copy one confined input to an owner-only bounded private spool.

    Returns ``(path, sha256, size)``.  Fails closed if private permissions cannot
    be established.
    """
    root = Path(directory) if directory is not None else None
    if root is not None:
        root.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(
        prefix="zimantic-derived-",
        suffix=".spool",
        dir=str(root) if root is not None else None,
    )
    path = Path(name)
    try:
        if os.name == "posix":
            mode = stat.S_IMODE(os.fstat(fd).st_mode)
            if mode & 0o077:
                os.fchmod(fd, mode & ~0o077)
        digest = hashlib.sha256()
        total = 0
        with os.fdopen(fd, "wb") as handle:
            source = input.open()
            try:
                while True:
                    chunk = source.read(1 << 16)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError("derived input exceeds its byte cap")
                    digest.update(chunk)
                    handle.write(chunk)
            finally:
                close = getattr(source, "close", None)
                if callable(close):
                    close()
        return path, digest.hexdigest(), total
    except BaseException:
        path.unlink(missing_ok=True)
        raise


class DerivedWorkerPool:
    """Lazy shared process pool for native OCR/ASR engines."""

    def __init__(self, workers: int, *, max_tasks_per_child: int | None = 1) -> None:
        if workers < 1:
            raise ValueError("a derived worker pool needs at least one worker")
        context = multiprocessing.get_context("spawn")
        self._executor = ProcessPoolExecutor(
            max_workers=workers,
            mp_context=context,
            max_tasks_per_child=max_tasks_per_child,
        )

    def submit(self, operation: str, engine: str, payload: bytes, options: Mapping[str, Any]):
        return self._executor.submit(
            _worker_extract, operation, engine, payload, dict(options)
        )

    def close(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=True)

    def terminate(self) -> None:
        processes = list(getattr(self._executor, "_processes", {}).values())
        self._executor.shutdown(wait=False, cancel_futures=True)
        for process in processes:
            try:
                process.terminate()
            except Exception:
                pass
        for process in processes:
            try:
                process.join(timeout=5)
            except Exception:
                pass


def _worker_extract(
    operation: str, engine: str, payload: bytes, options: Mapping[str, Any]
) -> str:
    """Spawn-safe worker entry point; engines are created inside the worker."""
    if operation == "ocr" and engine == "tesseract":
        try:
            import pymupdf  # noqa: F401
        except Exception as error:
            raise RuntimeError(f"OCR engine unavailable: {error}") from error
        raise RuntimeError("OCR engine is not configured for this build")
    if operation == "asr" and engine == "whisper":
        raise RuntimeError("ASR engine is not configured for this build")
    raise RuntimeError(f"unsupported derived operation {operation!r}")


# -- stage -----------------------------------------------------------------


@dataclass
class DerivedContribution:
    operation: str
    text: str
    cache_key: str | None = None


@dataclass
class DerivedResult:
    contributions: list[DerivedContribution] = field(default_factory=list)
    states: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def partial(self) -> bool:
        return bool(self.errors) or any(
            state in {OPERATION_PARTIAL, OPERATION_UNAVAILABLE}
            for state in self.states.values()
        )


class DerivedTextStage:
    """Shared optional enrichment stage used by every local provider."""

    def __init__(
        self,
        config: DerivedTextConfig | None = None,
        *,
        operations: SourceDerivedOperations | None = None,
        cache: DerivedTextCache | None = None,
        engines: Mapping[str, Callable[..., str]] | None = None,
        worker_pool: DerivedWorkerPool | None = None,
        spool_directory: Path | str | None = None,
    ) -> None:
        self.config = config or DerivedTextConfig()
        self.operations = operations or SourceDerivedOperations()
        self.cache = cache
        self.engines = dict(engines or {})
        # Standalone SubRip/WebVTT parsing is pure Python and needs no extra
        # dependency or external tool.
        self.engines.setdefault("captions", parse_caption_bytes)
        self.worker_pool = worker_pool
        self.spool_directory = spool_directory
        self._last_cache_key: str | None = None

    @property
    def enabled(self) -> bool:
        return self.operations.any_enabled

    def _initial_states(self) -> dict[str, str]:
        states = {}
        for operation in DERIVED_ORDER:
            if operation == "captions":
                enabled = self.operations.captions
            else:
                enabled = bool(getattr(self.operations, operation))
            if not enabled:
                states[operation] = OPERATION_OFF
            elif self.engines.get(operation) is None:
                states[operation] = OPERATION_UNAVAILABLE
            else:
                states[operation] = OPERATION_NOT_CHECKED
        return states

    def _options(self) -> dict[str, Any]:
        return {
            "max_caption_bytes": self.config.max_caption_bytes,
            "ocr_languages": list(self.config.ocr_languages),
            "max_ocr_pages": self.config.max_ocr_pages,
            "max_ocr_pixels": self.config.max_ocr_pixels,
            "max_ocr_output_bytes": self.config.max_ocr_output_bytes,
            "asr_engine": self.config.asr_engine,
            "asr_model": self.config.asr_model,
            "asr_threads": self.config.asr_threads,
            "asr_batched": self.config.asr_batched,
            "max_asr_seconds": self.config.max_asr_seconds,
            "max_asr_bytes": self.config.max_asr_bytes,
            "max_asr_output_bytes": self.config.max_asr_output_bytes,
        }

    def _failure_state(self, operation: str) -> str:
        return (
            OPERATION_UNAVAILABLE
            if self.engines.get(operation) is None
            else OPERATION_PARTIAL
        )

    def process(
        self,
        index: int,
        native_text: str,
        inputs: Iterable[DerivedTextInput] | None = None,
    ) -> DerivedResult:
        result = DerivedResult(states=self._initial_states())
        if not self.enabled:
            return result
        caption_parts: list[str] = []
        pending_asr_text = ""
        ordered_inputs = sorted(
            inputs or (),
            key=lambda candidate: {
                "captions": 0,
                "image": 1,
                "audio": 2,
                "video": 3,
            }.get(str(getattr(candidate, "kind", "")), 4),
        )
        for candidate in ordered_inputs:
            kind = str(getattr(candidate, "kind", ""))
            if kind == "captions":
                if not self.operations.captions:
                    continue
                text, error = self._extract(
                    candidate,
                    operation="captions",
                    engine="native",
                    cap=min(
                        self.config.max_caption_bytes,
                        int(getattr(candidate, "size", self.config.max_caption_bytes)),
                    ),
                )
                if error:
                    result.errors.append(error)
                    result.states["captions"] = self._failure_state("captions")
                elif text:
                    part = _cap_text(text, self.config.max_caption_bytes)
                    if part and part not in caption_parts:
                        caption_parts.append(part)
                    result.states["captions"] = OPERATION_READY
                else:
                    result.states["captions"] = OPERATION_READY
            elif kind == "image":
                if not self.operations.ocr:
                    continue
                # The provider/global input caps are enforced by the input
                # wrapper; OCR has page/pixel/output limits, not an input-byte
                # limit of its own.
                text, error = self._extract(
                    candidate,
                    operation="ocr",
                    engine=self.operations.ocr,
                    cap=max(1, int(getattr(candidate, "size", 1))),
                )
                if error:
                    result.errors.append(error)
                    result.states["ocr"] = self._failure_state("ocr")
                elif text:
                    result.contributions.append(
                        DerivedContribution(
                            "ocr",
                            _cap_text(text, self.config.max_ocr_output_bytes),
                            self._last_cache_key,
                        )
                    )
                    result.states["ocr"] = OPERATION_READY
            elif kind in {"audio", "video"}:
                if self.operations.captions and self.engines.get("ffmpeg") is not None:
                    # Embedded caption parsing is delegated to the provider's
                    # demuxer; a usable contribution suppresses ASR.  Without a
                    # local ffmpeg adapter, leave the operation not_checked
                    # rather than feeding media bytes to the sidecar parser.
                    text, error = self._extract(
                        candidate,
                        operation="captions",
                        engine="ffmpeg",
                        cap=max(1, int(getattr(candidate, "size", 1))),
                    )
                    if error:
                        result.errors.append(error)
                        result.states["captions"] = self._failure_state("captions")
                    elif text:
                        part = _cap_text(text, self.config.max_caption_bytes)
                        if part and part not in caption_parts:
                            caption_parts.append(part)
                        result.states["captions"] = OPERATION_READY
                if self.operations.asr and not caption_parts:
                    text, error = self._extract(
                        candidate,
                        operation="asr",
                        engine=self.operations.asr,
                        cap=self.config.max_asr_bytes,
                    )
                    if error:
                        result.errors.append(error)
                        result.states["asr"] = self._failure_state("asr")
                    elif text:
                        pending_asr_text = _cap_text(
                            text, self.config.max_asr_output_bytes
                        )
                        result.states["asr"] = OPERATION_READY
        caption_text = _cap_text(
            "\n\n".join(caption_parts), self.config.max_caption_bytes
        )
        if caption_text:
            cache_key = self._last_cache_key
            if self.cache is not None and len(caption_parts) > 1:
                digest = hashlib.sha256(
                    caption_text.encode("utf-8", "ignore")
                ).hexdigest()
                stored = self.cache.put(
                    digest,
                    "captions",
                    "combined",
                    self._options(),
                    caption_text,
                )
                if stored is not None:
                    cache_key = stored.name
            result.contributions.insert(
                0, DerivedContribution("captions", caption_text, cache_key)
            )
        if pending_asr_text:
            result.contributions.append(
                DerivedContribution("asr", pending_asr_text, self._last_cache_key)
            )
        return result

    def _extract(
        self,
        input: DerivedTextInput,
        *,
        operation: str,
        engine: str,
        cap: int,
    ) -> tuple[str, str | None]:
        self._last_cache_key = None
        engine_function = self.engines.get(operation)
        if operation == "captions" and engine == "ffmpeg":
            engine_function = self.engines.get("ffmpeg")
        if engine_function is None:
            return "", f"{operation} engine {engine!r} is unavailable"
        try:
            spool, digest, size = spool_input(
                input, max_bytes=max(1, int(cap)), directory=self.spool_directory
            )
        except Exception as error:
            return "", f"{operation} input could not be read safely: {error}"
        try:
            options = self._options()
            if self.cache is not None:
                cached = self.cache.get(digest, operation, engine, options)
                if cached is not None:
                    self._last_cache_key = self.cache._key_path(
                        digest, operation, engine, options
                    ).name
                    return cached, None
            data = spool.read_bytes()
            text = engine_function(data, **options)
            text = str(text or "")
            if self.cache is not None and text:
                stored = self.cache.put(digest, operation, engine, options, text)
                if stored is not None:
                    self._last_cache_key = stored.name
            return text, None
        except Exception as error:
            return "", f"{operation} failed: {error}"
        finally:
            spool.unlink(missing_ok=True)


__all__ = [
    "ASR_ENGINES",
    "DERIVED_ORDER",
    "DerivedContribution",
    "DerivedResult",
    "DerivedTextCache",
    "DerivedTextConfig",
    "DerivedTextStage",
    "DerivedWorkerPool",
    "OPERATION_NOT_CHECKED",
    "OPERATION_OFF",
    "OPERATION_PARTIAL",
    "OPERATION_READY",
    "OPERATION_STATES",
    "OPERATION_UNAVAILABLE",
    "SourceDerivedOperations",
    "compose_body",
    "derived_text_preview",
    "normalize_for_equality",
    "parse_caption_bytes",
    "parse_derived_text",
    "parse_source_override",
    "parse_source_overrides",
    "read_cached_text",
    "resolve_workers",
    "spool_input",
]
