"""Path-addressed content store for one Kolibri source.

Paths address opaque node ids (``node/<id>``) and HTML5 assets inside the
validated bundle.  The storage checksum layout is never exposed.  Membership is
rechecked against the runtime's pinned private snapshot on every request, so a
valid node id from another source or channel cannot be served through this
source.
"""

from __future__ import annotations

import logging
import os
import posixpath
import secrets
import stat as stat_module
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ...content.serving import HEAD_READ_BYTES, MAX_ICON_BYTES
from ...content.store import (
    ArticleData,
    ArticleNotFoundError,
    ArticleTooLargeError,
    ArticleUnavailableError,
    ContentEntry,
    ContentRange,
)
from ..files.common import FilesConfinedError, open_confined
from .discovery import (
    ChannelRow,
    KolibriSource,
    SourceScope,
    channel_thumbnail,
    encode_component,
    files_for_node,
    icon_file,
    load_node,
    parse_node_path,
)
from .html5 import Html5Archive
from .schema import (
    EXERCISE_KIND,
    HTML5_PRESETS,
    SUPPORTED_HTML5_PRESETS,
    FileRow,
    FileSelection,
    display_title,
    select_primary,
    storage_filename,
    storage_relative_path,
)

logger = logging.getLogger(__name__)

_RANDOM_SAMPLE_LIMIT = 256


def storage_path(storage_root: Path | str, file: FileRow) -> tuple[str, Path]:
    """Return ``(relative, absolute)`` for a validated DB-mapped file."""
    relative = storage_relative_path(file)
    root = Path(storage_root)
    return relative, root / Path(*relative.split("/"))


def _title_for_asset(name: str, fallback: str) -> str:
    return display_title(name) or fallback


@dataclass(frozen=True)
class _Target:
    kind: str  # file | metadata | html5
    node_id: str | None
    node_title: str
    path: str
    title: str
    mimetype: str
    file: FileRow | None = None
    archive: Html5Archive | None = None
    asset: str = ""
    metadata: bytes = b""
    size: int | None = None
    redirect_path: str | None = None


class KolibriContentStore:
    """Read one Kolibri source from its pinned installation snapshot."""

    def __init__(
        self,
        source: KolibriSource | Any,
        snapshot: Any,
        *,
        storage_root: Path | str,
        channel: ChannelRow | None = None,
        scope: SourceScope | None = None,
        config: Any | None = None,
        max_article_bytes: int = 1 << 30,
        max_icon_bytes: int = MAX_ICON_BYTES,
        index: Any | None = None,
    ) -> None:
        self.source = source
        self.info = getattr(source, "info", source)
        self.channel = channel or getattr(source, "channel", None)
        self.scope = scope or getattr(source, "scope", None)
        if self.channel is None or self.scope is None:
            raise ArticleUnavailableError("Kolibri source metadata is unavailable")
        self.snapshot = snapshot
        self.storage_root = Path(os.path.realpath(Path(storage_root)))
        self.config = config
        self.max_article_bytes = max(0, int(max_article_bytes))
        self.max_icon_bytes = max(0, int(max_icon_bytes))
        self.index = index
        self._closed = False
        self._max_entries = max(
            1, int(getattr(config, "max_html5_zip_entries", 10_000))
        )
        self._max_zip_directory = max(
            1, int(getattr(config, "max_html5_zip_directory_bytes", 8_388_608))
        )
        self._max_zip_member = max(
            1, int(getattr(config, "max_html5_zip_member_bytes", 268_435_456))
        )
        self._max_zip_total = max(
            1, int(getattr(config, "max_html5_zip_total_bytes", 1_073_741_824))
        )
        self._zip_timeout = max(
            0.001, float(getattr(config, "html5_zip_timeout_seconds", 10))
        )
        try:
            info = os.stat(self.storage_root, follow_symlinks=False)
        except OSError as error:
            raise ArticleUnavailableError(
                "Kolibri storage directory is unavailable"
            ) from error
        if not stat_module.S_ISDIR(info.st_mode):
            raise ArticleUnavailableError(
                "Kolibri storage path is not a directory"
            )
        # Keep the pinned snapshot alive for the whole request even if a
        # concurrent reload swaps and retires the runtime's current snapshot.
        self._snapshot_retained = False
        retain = getattr(self.snapshot, "retain", None)
        if callable(retain):
            retain()
            self._snapshot_retained = True

    # -- path handling -----------------------------------------------------

    @staticmethod
    def _normalize_path(path: str) -> str:
        text = str(path)
        if (
            not text
            or text.startswith("/")
            or "\\" in text
            or "\x00" in text
        ):
            raise ArticleNotFoundError("article not found")
        parts = text.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise ArticleNotFoundError("article not found")
        normalized = posixpath.normpath(text)
        if normalized != text:
            raise ArticleNotFoundError("article not found")
        return normalized

    def _node(self, node_id: str) -> Any:
        try:
            db = self.snapshot.connect()
        except Exception as error:
            raise ArticleUnavailableError(
                "Kolibri snapshot is unavailable"
            ) from error
        try:
            node = load_node(db, node_id, self.channel.id)
            if node is None or not node.available or node.coach_content:
                raise ArticleNotFoundError("article not found")
            if not self._in_scope(node):
                raise ArticleNotFoundError("article not found")
            return node, db
        except Exception:
            db.close()
            raise

    def _in_scope(self, node: Any) -> bool:
        scope = self.scope
        if node.channel_id != self.channel.id:
            return False
        if scope.kind == "topic":
            return (
                node.tree_id == scope.tree_id
                and scope.lft is not None
                and scope.rght is not None
                and scope.lft <= node.lft <= scope.rght
            )
        if scope.kind == "ungrouped":
            return node.parent_id == scope.parent_id and node.kind != "topic"
        if scope.kind in {"root", "collapsed"}:
            return True
        return False

    def _selection(self, db, node: Any) -> tuple[FileSelection, tuple[FileRow, ...]]:
        files = files_for_node(db, node.id)
        return select_primary(files, node.kind, node.lang_id), files

    def _archive(self, file: FileRow) -> Html5Archive:
        relative = storage_relative_path(file)
        return Html5Archive(
            self.storage_root,
            relative,
            max_entries=self._max_entries,
            max_directory_bytes=self._max_zip_directory,
            max_member_bytes=self._max_zip_member,
            max_total_bytes=self._max_zip_total,
            timeout_seconds=self._zip_timeout,
        )

    def _storage_exists(self, file: FileRow) -> bool:
        try:
            relative, _absolute = storage_path(self.storage_root, file)
        except ValueError:
            return False
        try:
            opened = open_confined(self.storage_root, relative)
        except FilesConfinedError:
            return False
        try:
            return stat_module.S_ISREG(opened.stat.st_mode)
        finally:
            close = getattr(opened.handle, "close", None)
            if callable(close):
                close()

    def _storage_info(self, file: FileRow) -> os.stat_result | None:
        try:
            relative, _absolute = storage_path(self.storage_root, file)
        except ValueError:
            return None
        try:
            opened = open_confined(self.storage_root, relative)
        except FilesConfinedError:
            return None
        try:
            return opened.stat
        finally:
            close = getattr(opened.handle, "close", None)
            if callable(close):
                close()

    def _metadata_page(self, node: Any) -> bytes:
        description = str(node.description or "").strip()
        text = f"{node.title}\n\n{description}\n"
        return text.encode("utf-8", "ignore")

    def _node_target(
        self,
        node_id: str,
        asset: str,
        requested: str,
    ) -> _Target:
        node, db = self._node(node_id)
        try:
            selection, _files = self._selection(db, node)
        finally:
            db.close()
        node_path = f"node/{encode_component(node_id)}"
        if selection.ambiguous:
            raise ArticleUnavailableError(
                "content node has an ambiguous primary representation: "
                + "; ".join(selection.ambiguous)
            )
        primary = selection.primary
        if primary is None:
            if (
                node.kind == EXERCISE_KIND
                and str(node.description or "").strip()
                and not asset
            ):
                metadata = self._metadata_page(node)
                return _Target(
                    "metadata",
                    node_id,
                    node.title,
                    requested,
                    node.title,
                    "text/plain",
                    metadata=metadata,
                    size=len(metadata),
                )
            raise ArticleNotFoundError("article not found")
        if primary.preset in HTML5_PRESETS:
            if primary.preset not in SUPPORTED_HTML5_PRESETS:
                raise ArticleUnavailableError(
                    f"unsupported Kolibri HTML5 preset {primary.preset!r}"
                )
            archive = self._archive(primary)
            if asset:
                member = archive.member(asset)
                final = f"{node_path}/{asset}"
                return _Target(
                    "html5",
                    node_id,
                    node.title,
                    final,
                    _title_for_asset(asset, node.title),
                    archive.mimetype(asset),
                    file=primary,
                    archive=archive,
                    asset=asset,
                    size=member.file_size,
                )
            index_path = archive.index_path
            member = archive.member(index_path)
            return _Target(
                "html5",
                node_id,
                node.title,
                f"{node_path}/{index_path}",
                node.title,
                archive.mimetype(index_path),
                file=primary,
                archive=archive,
                asset=index_path,
                size=member.file_size,
                redirect_path=requested,
            )
        if asset:
            raise ArticleNotFoundError("article not found")
        info = self._storage_info(primary)
        if info is None or not stat_module.S_ISREG(info.st_mode):
            raise ArticleNotFoundError("article not found")
        return _Target(
            "file",
            node_id,
            node.title,
            requested,
            node.title,
            primary.mimetype,
            file=primary,
            size=int(info.st_size),
        )

    def _channel_target(self, parts: list[str]) -> _Target:
        if len(parts) != 3 or parts[0] != "channel":
            raise ArticleNotFoundError("article not found")
        from .discovery import decode_component

        try:
            channel_id = decode_component(parts[1])
        except ValueError as error:
            raise ArticleNotFoundError("article not found") from error
        if channel_id != self.channel.id:
            raise ArticleNotFoundError("article not found")
        try:
            db = self.snapshot.connect()
        except Exception as error:
            raise ArticleUnavailableError(
                "Kolibri snapshot is unavailable"
            ) from error
        try:
            image = channel_thumbnail(db, self.channel)
        finally:
            db.close()
        if image is None:
            raise ArticleNotFoundError("article not found")
        try:
            expected = storage_filename(image)
        except ValueError as error:
            raise ArticleNotFoundError("article not found") from error
        if parts[2] not in {expected, "icon"}:
            raise ArticleNotFoundError("article not found")
        info = self._storage_info(image)
        if info is None or not stat_module.S_ISREG(info.st_mode):
            raise ArticleNotFoundError("article not found")
        return _Target(
            "file",
            None,
            self.channel.name,
            f"channel/{encode_component(channel_id)}/{expected}",
            self.channel.name,
            image.mimetype,
            file=image,
            size=int(info.st_size),
        )

    def _target(self, path: str | None) -> _Target:
        if path is None or path == "":
            main = self.main_path()
            if main is None:
                raise ArticleNotFoundError("source has no homepage")
            return self._target(main)
        raw = str(path)
        raw = raw.removesuffix("/")
        requested = self._normalize_path(raw)
        parts = requested.split("/")
        if parts[0] == "channel":
            return self._channel_target(parts)
        if parts[0] != "node" or len(parts) < 2:
            raise ArticleNotFoundError("article not found")
        try:
            node_id, asset = parse_node_path(requested)
        except ValueError as error:
            raise ArticleNotFoundError("article not found") from error
        return self._node_target(node_id, asset, requested)

    # -- contract ----------------------------------------------------------

    def main_path(self) -> str | None:
        return getattr(self.info, "homepage", None)

    def resolve(self, path: str | None) -> ContentEntry:
        target = self._target(path)
        return ContentEntry(
            path=target.path,
            title=target.title,
            mimetype=target.mimetype,
            size=target.size,
            redirect_path=target.redirect_path,
        )

    def exists(self, path: str) -> bool:
        try:
            self.resolve(path)
            return True
        except (
            ArticleNotFoundError,
            ArticleTooLargeError,
            ArticleUnavailableError,
            ValueError,
        ):
            return False

    def _read_file(
        self, target: _Target, *, head_only: bool, max_bytes: int
    ) -> ArticleData:
        file = target.file
        if file is None:
            raise ArticleNotFoundError("article not found")
        try:
            relative, _absolute = storage_path(self.storage_root, file)
        except ValueError as error:
            raise ArticleNotFoundError("article not found") from error
        try:
            opened = open_confined(self.storage_root, relative)
        except FilesConfinedError as error:
            raise ArticleNotFoundError("article not found") from error
        try:
            size = int(opened.stat.st_size)
            limit = max(0, int(max_bytes))
            if head_only:
                content = opened.handle.read(min(limit, HEAD_READ_BYTES))
                total = size
                truncated = len(content) < total
            else:
                if size > limit:
                    raise ArticleTooLargeError(
                        f"article exceeds the {limit} byte serving limit"
                    )
                content = opened.handle.read(limit + 1)
                if len(content) > limit:
                    raise ArticleTooLargeError(
                        f"article exceeds the {limit} byte serving limit"
                    )
                total = size
                truncated = len(content) < total
        except OSError as error:
            raise ArticleUnavailableError("article content is unavailable") from error
        finally:
            close = getattr(opened.handle, "close", None)
            if callable(close):
                close()
        return ArticleData(
            source_key=self.info.key,
            path=target.path,
            title=target.title,
            mimetype=target.mimetype,
            content=content,
            total=total,
            truncated=truncated,
        )

    def read(
        self, path: str | None, *, head_only: bool, max_bytes: int
    ) -> ArticleData:
        target = self._target(path)
        if target.kind == "file":
            return self._read_file(target, head_only=head_only, max_bytes=max_bytes)
        limit = max(0, int(max_bytes))
        if target.kind == "metadata":
            total = len(target.metadata)
            content = target.metadata[: min(limit, HEAD_READ_BYTES)] if head_only else target.metadata
            if not head_only and total > limit:
                raise ArticleTooLargeError(
                    f"article exceeds the {limit} byte serving limit"
                )
            return ArticleData(
                source_key=self.info.key,
                path=target.path,
                title=target.title,
                mimetype=target.mimetype,
                content=content,
                total=total,
                truncated=head_only and len(content) < total,
            )
        assert target.archive is not None
        content, total, truncated, mimetype = target.archive.read(
            target.asset,
            head_only=head_only,
            max_bytes=limit,
        )
        return ArticleData(
            source_key=self.info.key,
            path=target.path,
            title=target.title,
            mimetype=mimetype,
            content=content,
            total=total,
            truncated=truncated,
        )

    def read_range(
        self, path: str, *, start: int, end: int, max_bytes: int
    ) -> ContentRange:
        target = self._target(path)
        file = target.file
        if target.kind == "metadata":
            data = target.metadata
            start = int(start)
            end = int(end)
            if start < 0 or end < start or start >= len(data):
                raise ValueError("invalid content range")
            end = min(end, len(data) - 1)
            limit = max(0, int(max_bytes))
            if end - start + 1 > limit:
                end = start + limit - 1
            content = data[start : end + 1]
            return ContentRange(
                source_key=self.info.key,
                path=target.path,
                title=target.title,
                mimetype=target.mimetype,
                content=content,
                start=start,
                end=start + len(content) - 1,
                total=len(data),
            )
        if target.kind == "html5":
            assert target.archive is not None
            content, actual_start, actual_end, total, mimetype = (
                target.archive.read_range(
                    target.asset,
                    start=int(start),
                    end=int(end),
                    max_bytes=max(0, int(max_bytes)),
                )
            )
            return ContentRange(
                source_key=self.info.key,
                path=target.path,
                title=target.title,
                mimetype=mimetype,
                content=content,
                start=actual_start,
                end=actual_end,
                total=total,
            )
        if file is None:
            raise ArticleNotFoundError("article not found")
        try:
            relative, _absolute = storage_path(self.storage_root, file)
        except ValueError as error:
            raise ArticleNotFoundError("article not found") from error
        try:
            opened = open_confined(self.storage_root, relative)
        except FilesConfinedError as error:
            raise ArticleNotFoundError("article not found") from error
        try:
            size = int(opened.stat.st_size)
            start = int(start)
            end = int(end)
            if start < 0 or end < start:
                raise ValueError("invalid content range")
            if start >= size:
                raise ValueError("content range starts past the representation")
            end = min(end, size - 1)
            limit = max(0, int(max_bytes))
            if limit <= 0:
                raise ArticleTooLargeError(
                    "article range exceeds the serving limit"
                )
            if end - start + 1 > limit:
                end = start + limit - 1
            opened.handle.seek(start)
            content = opened.handle.read(end - start + 1)
        except OSError as error:
            raise ArticleUnavailableError("article content is unavailable") from error
        finally:
            close = getattr(opened.handle, "close", None)
            if callable(close):
                close()
        if not content:
            raise ArticleNotFoundError("article not found")
        return ContentRange(
            source_key=self.info.key,
            path=target.path,
            title=target.title,
            mimetype=target.mimetype,
            content=content,
            start=start,
            end=start + len(content) - 1,
            total=size,
        )

    def random_path(self) -> str | None:
        if self.index is not None:
            acquired = False
            try:
                self.index.acquire()
                acquired = True
            except RuntimeError:
                acquired = False
            if acquired:
                try:
                    path = self._random_indexed_path()
                finally:
                    self.index.release()
                if path:
                    return path
        return self._random_node_path()

    def _random_indexed_path(self) -> str | None:
        try:
            db = self.index.db_for_thread()
        except Exception:
            return None
        if db is None:
            return None
        try:
            count = int(
                db.execute(
                    "SELECT COUNT(*) FROM docs WHERE target IS NULL"
                ).fetchone()[0]
            )
            if count <= 0:
                return None
            row = db.execute(
                "SELECT path FROM docs WHERE target IS NULL "
                "ORDER BY rowid LIMIT 1 OFFSET ?",
                (secrets.randbelow(count),),
            ).fetchone()
            if row and row[0]:
                return self._normalize_path(str(row[0]))
        except Exception:
            return None
        return None

    def _random_node_path(self) -> str | None:
        from .discovery import candidate_nodes

        try:
            db = self.snapshot.connect()
        except Exception:
            return None
        try:
            nodes, _truncated = candidate_nodes(
                db, self.channel, self.scope, limit=_RANDOM_SAMPLE_LIMIT
            )
        except Exception:
            return None
        finally:
            db.close()
        if not nodes:
            return None
        return f"node/{encode_component(secrets.choice(nodes).id)}"

    def icon(self, max_bytes: int) -> ArticleData | None:
        limit = max(0, min(int(max_bytes), self.max_icon_bytes))
        try:
            db = self.snapshot.connect()
        except Exception:
            return None
        try:
            image = icon_file(db, self.channel, self.scope)
        finally:
            db.close()
        if image is None:
            return None
        try:
            relative, _absolute = storage_path(self.storage_root, image)
        except ValueError:
            return None
        try:
            opened = open_confined(self.storage_root, relative)
        except FilesConfinedError:
            return None
        try:
            if not stat_module.S_ISREG(opened.stat.st_mode):
                return None
            content = opened.handle.read(limit + 1)
        except OSError:
            return None
        finally:
            close = getattr(opened.handle, "close", None)
            if callable(close):
                close()
        if len(content) > limit:
            content = content[:limit]
        return ArticleData(
            source_key=self.info.key,
            path=f"channel/{encode_component(self.channel.id)}/icon",
            title=self.channel.name,
            mimetype=image.mimetype,
            content=content,
            total=len(content),
            truncated=False,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._snapshot_retained:
            self._snapshot_retained = False
            try:
                self.snapshot.release()
            except Exception:
                pass


__all__ = ["KolibriContentStore", "storage_path"]
