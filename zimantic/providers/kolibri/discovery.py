"""Kolibri channel, source, node, and file discovery.

One installed channel is one collection.  Source membership is resolved from
the private consistent installation snapshot; channel export databases under
``content/databases`` are never consulted.
"""

from __future__ import annotations

import base64
import logging
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from ...contracts import SourceInfo
from ...media_types import ordered_media_types
from .schema import (
    CHANNEL_TABLE,
    EXERCISE_KIND,
    FILE_TABLE,
    LOCAL_FILE_TABLE,
    NODE_TABLE,
    FileRow,
    FileSelection,
    media_family,
    select_primary,
)

logger = logging.getLogger(__name__)

DEFAULT_MAX_SOURCES = 64
DEFAULT_MAX_ENTRIES = 500_000
ROOT_IDENTIFIER = "root"


class KolibriTraversalError(RuntimeError):
    """A Kolibri discovery/build traversal budget was exhausted."""
UNGROUPED_IDENTIFIER = "ungrouped"
COLLAPSED_IDENTIFIER = "collapsed"

_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,191}$")


def encode_component(value: str) -> str:
    """Encode an opaque channel/node id as one URL-safe path component.

    Ordinary UUIDs and release-derived identifiers are left readable.  Values
    containing separators, percent escapes, or other unsafe characters are
    base64url-encoded behind a ``~`` marker so they cannot be mistaken for path
    structure.  The mapping is reversible and collision-free.
    """
    text = str(value)
    if _SAFE_COMPONENT.fullmatch(text):
        return text
    encoded = base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")
    return "~" + encoded.rstrip("=")


def decode_component(value: str) -> str:
    text = str(value)
    if text.startswith("~"):
        data = text[1:]
        data += "=" * (-len(data) % 4)
        try:
            return base64.urlsafe_b64decode(data.encode("ascii")).decode("utf-8")
        except (ValueError, UnicodeError) as error:
            raise ValueError("invalid encoded identifier") from error
    if not _SAFE_COMPONENT.fullmatch(text):
        raise ValueError("invalid identifier")
    return text


def kolibri_collection_key(name: str, channel_id: str) -> str:
    return f"kolibri:{name}:{encode_component(channel_id)}"


def kolibri_source_key(
    name: str, channel_id: str, identifier: str
) -> str:
    return (
        f"{kolibri_collection_key(name, channel_id)}:"
        f"{encode_component(identifier)}"
    )


def kolibri_node_source_key(
    name: str, channel_id: str, node_id: str
) -> str:
    return f"{kolibri_collection_key(name, channel_id)}:node:{encode_component(node_id)}"


def kolibri_ungrouped_key(name: str, channel_id: str) -> str:
    return kolibri_source_key(name, channel_id, UNGROUPED_IDENTIFIER)


def kolibri_root_key(name: str, channel_id: str) -> str:
    return kolibri_source_key(name, channel_id, ROOT_IDENTIFIER)


def kolibri_collapsed_key(name: str, channel_id: str) -> str:
    return kolibri_source_key(name, channel_id, COLLAPSED_IDENTIFIER)


def kolibri_node_path(node_id: str) -> str:
    return f"node/{encode_component(node_id)}"


def parse_node_path(path: str) -> tuple[str, str]:
    """Return ``(node_id, asset_path)`` for a synthetic content path."""
    text = str(path)
    if not text.startswith("node/"):
        raise ValueError("not a node path")
    remainder = text[len("node/") :]
    component, _sep, asset = remainder.partition("/")
    if not component:
        raise ValueError("missing node identifier")
    return decode_component(component), asset


@dataclass(frozen=True)
class ChannelRow:
    id: str
    name: str
    description: str
    tagline: str
    author: str
    version: int
    thumbnail: str
    root_id: str
    public: bool | None = None
    order: int | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> ChannelRow:
        keys = set(row.keys())
        return cls(
            id=str(row["id"]),
            name=str(row["name"] or ""),
            description=str(row["description"] or ""),
            tagline=str(row["tagline"] or "") if "tagline" in keys else "",
            author=str(row["author"] or "") if "author" in keys else "",
            version=int(row["version"] or 0) if "version" in keys else 0,
            thumbnail=str(row["thumbnail"] or "") if "thumbnail" in keys else "",
            root_id=str(row["root_id"] or ""),
            public=(
                bool(row["public"]) if "public" in keys and row["public"] is not None else None
            ),
            order=(
                int(row["order"]) if "order" in keys and row["order"] is not None else None
            ),
        )


@dataclass(frozen=True)
class NodeRow:
    id: str
    parent_id: str | None
    tree_id: int
    level: int
    lft: int
    rght: int
    title: str
    kind: str
    content_id: str
    channel_id: str
    description: str
    available: bool
    coach_content: bool
    sort_order: float | None
    lang_id: str | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> NodeRow:
        keys = set(row.keys())
        return cls(
            id=str(row["id"]),
            parent_id=(
                str(row["parent_id"]) if row["parent_id"] is not None else None
            ),
            tree_id=int(row["tree_id"] or 0),
            level=int(row["level"] or 0),
            lft=int(row["lft"] or 0),
            rght=int(row["rght"] or 0),
            title=str(row["title"] or ""),
            kind=str(row["kind"] or ""),
            content_id=str(row["content_id"] or ""),
            channel_id=str(row["channel_id"] or ""),
            description=str(row["description"] or ""),
            available=bool(row["available"]),
            coach_content=bool(row["coach_content"]),
            sort_order=(
                float(row["sort_order"])
                if row["sort_order"] is not None
                else None
            ),
            lang_id=(
                str(row["lang_id"])
                if "lang_id" in keys and row["lang_id"] is not None
                else None
            ),
        )


@dataclass(frozen=True)
class SourceScope:
    """One source's membership inside a channel."""

    kind: str  # topic | ungrouped | root | collapsed
    node_id: str | None = None
    parent_id: str | None = None
    tree_id: int | None = None
    lft: int | None = None
    rght: int | None = None
    title: str = ""

    @property
    def is_topic(self) -> bool:
        return self.kind == "topic"


@dataclass(frozen=True)
class KolibriSource:
    info: SourceInfo
    channel: ChannelRow
    scope: SourceScope
    icon_file: FileRow | None = None

    @property
    def key(self) -> str:
        return self.info.key


@dataclass(frozen=True)
class ChannelDiscovery:
    channel: ChannelRow
    sources: tuple[KolibriSource, ...]
    error: str | None = None


def _row_limit(config: Any, name: str, default: int) -> int:
    value = getattr(config, name, None)
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return default


def load_channels(db) -> list[ChannelRow]:
    rows = db.execute(
        f"SELECT * FROM {CHANNEL_TABLE} ORDER BY "
        "COALESCE(\"order\", 0), name, id"
    ).fetchall()
    channels: list[ChannelRow] = []
    for row in rows:
        try:
            channels.append(ChannelRow.from_row(row))
        except (TypeError, ValueError) as error:
            logger.warning(
                "zimantic: ignoring malformed Kolibri channel metadata (%s)",
                type(error).__name__,
            )
            logger.debug("malformed Kolibri channel metadata: %s", error)
    return channels


def load_channel(db, channel_id: str) -> ChannelRow | None:
    row = db.execute(
        f"SELECT * FROM {CHANNEL_TABLE} WHERE id = ?", (channel_id,)
    ).fetchone()
    if row is None:
        return None
    try:
        return ChannelRow.from_row(row)
    except (TypeError, ValueError):
        return None


def load_node(db, node_id: str, channel_id: str) -> NodeRow | None:
    row = db.execute(
        f"SELECT * FROM {NODE_TABLE} WHERE id = ? AND channel_id = ?",
        (node_id, channel_id),
    ).fetchone()
    return NodeRow.from_row(row) if row is not None else None


def load_root_node(db, channel: ChannelRow) -> NodeRow | None:
    if not channel.root_id:
        return None
    node = load_node(db, channel.root_id, channel.id)
    if node is None or node.kind != "topic":
        return None
    return node


def _scope_sql(scope: SourceScope, alias: str = "n") -> tuple[str, tuple[Any, ...]]:
    prefix = f"{alias}." if alias else ""
    if scope.kind == "topic":
        if scope.tree_id is None or scope.lft is None or scope.rght is None:
            raise ValueError("topic source is missing its tree bounds")
        return (
            f"({prefix}tree_id = ? AND {prefix}lft >= ? AND {prefix}rght <= ?)",
            (scope.tree_id, scope.lft, scope.rght),
        )
    if scope.kind == "ungrouped":
        return (f"{prefix}parent_id = ?", (scope.parent_id,))
    if scope.kind in {"root", "collapsed"}:
        return (f"{prefix}channel_id = ?", (scope.node_id,))
    raise ValueError("unknown Kolibri source scope")


def _eligible_sql(alias: str = "n") -> str:
    prefix = f"{alias}." if alias else ""
    return (
        f"{prefix}kind <> 'topic' AND COALESCE({prefix}available, 0) = 1 "
        f"AND COALESCE({prefix}coach_content, 0) = 0"
    )


def _node_has_item_sql(alias: str = "n") -> str:
    prefix = f"{alias}." if alias else ""
    return (
        "("
        f"({prefix}kind = '{EXERCISE_KIND}' AND TRIM(COALESCE({prefix}description, '')) <> '') "
        "OR EXISTS ("
        f"SELECT 1 FROM {FILE_TABLE} f JOIN {LOCAL_FILE_TABLE} lf "
        "ON lf.id = f.local_file_id "
        f"WHERE f.contentnode_id = {prefix}id AND lf.available = 1 "
        "AND COALESCE(f.thumbnail, 0) = 0 "
        "AND COALESCE(f.supplementary, 0) = 0 "
        "AND COALESCE(f.preset, '') <> 'video_subtitle' "
        "AND COALESCE(f.preset, '') NOT LIKE '%_thumbnail' "
        "AND COALESCE(f.preset, '') NOT LIKE '%_dependency' "
        "AND COALESCE(f.preset, '') NOT IN "
        "('exercise_image', 'exercise_graphie') "
        "AND LOWER(COALESCE(lf.extension, '')) NOT IN "
        "('vtt', 'srt', 'ass', 'ssa')"
        ")"
        ")"
    )


def load_files_for_nodes(db, node_ids: Iterable[str]) -> dict[str, tuple[FileRow, ...]]:
    ids = list(dict.fromkeys(str(node_id) for node_id in node_ids))
    by_node: dict[str, list[FileRow]] = {node_id: [] for node_id in ids}
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        if not chunk:
            continue
        placeholders = ",".join("?" for _ in chunk)
        rows = db.execute(
            f"SELECT f.id AS file_id, f.contentnode_id, f.local_file_id, "
            "f.preset, f.priority, f.supplementary, f.thumbnail, f.lang_id, "
            "lf.id AS local_id, lf.extension, lf.available, lf.file_size "
            f"FROM {FILE_TABLE} f JOIN {LOCAL_FILE_TABLE} lf "
            "ON lf.id = f.local_file_id "
            f"WHERE f.contentnode_id IN ({placeholders})",
            tuple(chunk),
        ).fetchall()
        for row in rows:
            file = FileRow(
                id=str(row["file_id"]),
                node_id=str(row["contentnode_id"]),
                local_file_id=str(row["local_file_id"]),
                extension=str(row["extension"] or ""),
                available=bool(row["available"]),
                file_size=(
                    int(row["file_size"]) if row["file_size"] is not None else None
                ),
                preset=str(row["preset"] or ""),
                priority=(
                    int(row["priority"]) if row["priority"] is not None else None
                ),
                supplementary=bool(row["supplementary"]),
                thumbnail=bool(row["thumbnail"]),
                lang_id=str(row["lang_id"]) if row["lang_id"] is not None else None,
                checksum=str(row["local_id"]),
            )
            by_node.setdefault(file.node_id, []).append(file)
    return {node_id: tuple(files) for node_id, files in by_node.items()}


def files_for_node(db, node_id: str) -> tuple[FileRow, ...]:
    return load_files_for_nodes(db, (node_id,)).get(str(node_id), ())


def select_node_files(files: Sequence[FileRow], kind: str) -> FileSelection:
    return select_primary(files, kind)


def candidate_nodes(
    db,
    channel: ChannelRow,
    scope: SourceScope,
    *,
    limit: int | None = None,
) -> tuple[list[NodeRow], bool]:
    """Return eligible leaf nodes in deterministic order and a truncation flag."""
    where, params = _scope_sql(scope)
    cap = None if limit is None else max(1, int(limit) + 1)
    sql = (
        f"SELECT * FROM {NODE_TABLE} n WHERE {where} "
        f"AND {_eligible_sql('n')} AND {_node_has_item_sql('n')} "
        "ORDER BY n.lft, n.id"
    )
    if cap is not None:
        sql += " LIMIT ?"
        params = (*params, cap)
    rows = db.execute(sql, params).fetchall()
    truncated = cap is not None and len(rows) > cap - 1
    if truncated:
        rows = rows[: cap - 1]
    return [NodeRow.from_row(row) for row in rows], truncated


def count_items(db, channel: ChannelRow, scope: SourceScope) -> int:
    where, params = _scope_sql(scope)
    row = db.execute(
        f"SELECT COUNT(*) FROM {NODE_TABLE} n WHERE {where} "
        f"AND {_eligible_sql('n')} AND {_node_has_item_sql('n')}",
        params,
    ).fetchone()
    return int(row[0] if row else 0)


def _scope_family(node: NodeRow, selection: FileSelection) -> str | None:
    if selection.primary is not None:
        return media_family(selection.primary.extension, node.kind)
    if node.kind == EXERCISE_KIND and node.description.strip():
        return "text"
    if node.kind == "document":
        return "text"
    if node.kind == "html5":
        return "text"
    if node.kind == "video":
        return "video"
    if node.kind == "audio":
        return "audio"
    return None


def classify_scope(
    db,
    channel: ChannelRow,
    scope: SourceScope,
    *,
    max_visited: int = 1_000_000,
    timeout_seconds: float = 300.0,
) -> tuple[str, ...]:
    """Observed media families, independent of the indexed manifest cap."""
    started = time.monotonic()
    nodes, truncated = candidate_nodes(db, channel, scope, limit=max_visited)
    if truncated:
        raise KolibriTraversalError(
            "Kolibri discovery exceeded its traversal entry budget while "
            "classifying a source"
        )
    if time.monotonic() - started > max(0.001, float(timeout_seconds)):
        raise KolibriTraversalError(
            "Kolibri discovery exceeded its traversal time budget while "
            "classifying a source"
        )
    files = load_files_for_nodes(db, (node.id for node in nodes))
    families: set[str] = set()
    for node in nodes:
        selection = select_primary(
            files.get(node.id, ()), node.kind, node.lang_id
        )
        family = _scope_family(node, selection)
        if family:
            families.add(family)
    return ordered_media_types(families)


def first_browsable_path(db, channel: ChannelRow, scope: SourceScope) -> str | None:
    nodes, _ = candidate_nodes(db, channel, scope, limit=256)
    files = load_files_for_nodes(db, (node.id for node in nodes))
    for node in nodes:
        selection = select_primary(
            files.get(node.id, ()), node.kind, node.lang_id
        )
        if selection.ambiguous:
            continue
        if selection.primary is not None or (
            node.kind == EXERCISE_KIND and node.description.strip()
        ):
            return kolibri_node_path(node.id)
    return None


def _thumbnail_row(db, preset: str, node_ids: Sequence[str]) -> FileRow | None:
    if not node_ids:
        return None
    placeholders = ",".join("?" for _ in node_ids)
    row = db.execute(
        f"SELECT f.id AS file_id, f.contentnode_id, f.local_file_id, "
        "f.preset, f.priority, f.supplementary, f.thumbnail, f.lang_id, "
        "lf.id AS local_id, lf.extension, lf.available, lf.file_size "
        f"FROM {FILE_TABLE} f JOIN {LOCAL_FILE_TABLE} lf "
        "ON lf.id = f.local_file_id "
        f"WHERE f.preset = ? AND lf.available = 1 "
        f"AND f.contentnode_id IN ({placeholders}) "
        "ORDER BY f.id LIMIT 1",
        (preset, *node_ids),
    ).fetchone()
    if row is None:
        return None
    return FileRow(
        id=str(row["file_id"]),
        node_id=str(row["contentnode_id"]),
        local_file_id=str(row["local_file_id"]),
        extension=str(row["extension"] or ""),
        available=True,
        file_size=(
            int(row["file_size"]) if row["file_size"] is not None else None
        ),
        preset=str(row["preset"] or ""),
        priority=(
            int(row["priority"]) if row["priority"] is not None else None
        ),
        supplementary=bool(row["supplementary"]),
        thumbnail=bool(row["thumbnail"]),
        lang_id=str(row["lang_id"]) if row["lang_id"] is not None else None,
        checksum=str(row["local_id"]),
    )


def channel_thumbnail(db, channel: ChannelRow) -> FileRow | None:
    """Return the channel's validated DB-mapped thumbnail file, if any."""
    nodes = [channel.root_id] if channel.root_id else []
    return _thumbnail_row(db, "channel_thumbnail", nodes)


def icon_file(db, channel: ChannelRow, scope: SourceScope) -> FileRow | None:
    """Return a validated local thumbnail file for a source or its channel."""
    preset_order = ("topic_thumbnail", "channel_thumbnail")
    candidate_nodes: list[str] = []
    if scope.kind == "topic" and scope.node_id:
        candidate_nodes.append(scope.node_id)
    if channel.root_id:
        candidate_nodes.append(channel.root_id)
    for preset in preset_order:
        image = _thumbnail_row(db, preset, candidate_nodes)
        if image is not None:
            return image
    return None


def direct_leaf_present(db, channel: ChannelRow, root: NodeRow) -> bool:
    row = db.execute(
        f"SELECT 1 FROM {NODE_TABLE} n WHERE n.parent_id = ? "
        f"AND {_eligible_sql('n')} AND {_node_has_item_sql('n')} LIMIT 1",
        (root.id,),
    ).fetchone()
    return row is not None


def _source_info(
    config: Any,
    *,
    collection_key: str,
    channel: ChannelRow,
    key: str,
    title: str,
    homepage: str | None,
    media_types: Sequence[str],
    item_count: int,
    has_icon: bool,
    web_base_path: str,
) -> SourceInfo:
    icon_url = None
    if has_icon:
        icon_url = (
            f"{web_base_path}/source-icon/{quote(key, safe='')}"
        )
    return SourceInfo(
        key=key,
        provider="kolibri",
        title=title or channel.name,
        collection_key=collection_key,
        collection_label=channel.name,
        searchable=False,
        homepage=homepage,
        icon_url=icon_url,
        media_types=tuple(media_types),
        item_count=item_count,
        description=channel.description or None,
        creator=channel.author or None,
    )


def discover_channels(
    db,
    config: Any,
    *,
    web_base_path: str = "",
) -> tuple[ChannelDiscovery, ...]:
    """Discover every installed channel and its bounded source set."""
    return tuple(
        discover_channel_sources(
            db, config, channel, web_base_path=web_base_path
        )
        for channel in load_channels(db)
    )


def discover_sources(
    db,
    config: Any,
    channel: ChannelRow,
    *,
    web_base_path: str = "",
) -> ChannelDiscovery:
    """Alias for one channel's source discovery."""
    return discover_channel_sources(
        db, config, channel, web_base_path=web_base_path
    )


def discover_channel_sources(
    db,
    config: Any,
    channel: ChannelRow,
    *,
    web_base_path: str = "",
) -> ChannelDiscovery:
    """Discover the bounded source set for one installed channel."""
    collection_key = kolibri_collection_key(config.name, channel.id)
    label = channel.name or config.name
    root = load_root_node(db, channel)
    if root is None:
        return ChannelDiscovery(
            channel,
            (),
            error=(
                f"channel {label!r} has a missing or mismatched root node; "
                "the channel is unavailable"
            ),
        )
    granularity = str(getattr(config, "granularity", "children"))
    max_sources = _row_limit(config, "max_sources", DEFAULT_MAX_SOURCES)
    traversal = getattr(config, "traversal", None)
    max_visited = int(getattr(traversal, "max_visited_entries", 1_000_000))
    timeout_seconds = float(
        getattr(traversal, "traversal_timeout_seconds", 300)
    )

    scopes: list[SourceScope] = []
    if granularity == "collection":
        scopes = [
            SourceScope("root", node_id=channel.id, title=label)
        ]
    else:
        topics = db.execute(
            f"SELECT * FROM {NODE_TABLE} WHERE parent_id = ? AND kind = 'topic' "
            "ORDER BY lft, id",
            (root.id,),
        ).fetchall()
        topic_nodes = [NodeRow.from_row(row) for row in topics]
        has_ungrouped = direct_leaf_present(db, channel, root)
        candidate_count = len(topic_nodes) + int(has_ungrouped)
        if not topic_nodes:
            scopes = [
                SourceScope("root", node_id=channel.id, title=label)
            ]
        elif candidate_count > max_sources:
            logger.warning(
                "zimantic: Kolibri channel %s has more than %d sources; "
                "collapsing it to one channel source (keys change; rebuild required)",
                label,
                max_sources,
            )
            scopes = [
                SourceScope("collapsed", node_id=channel.id, title=label)
            ]
        else:
            scopes.extend(
                SourceScope(
                    "topic",
                    node_id=node.id,
                    tree_id=node.tree_id,
                    lft=node.lft,
                    rght=node.rght,
                    title=node.title or label,
                )
                for node in topic_nodes
            )
            if has_ungrouped:
                scopes.append(
                    SourceScope(
                        "ungrouped",
                        parent_id=root.id,
                        title="Ungrouped",
                    )
                )

    sources: list[KolibriSource] = []
    for scope in scopes:
        if scope.kind == "topic":
            identifier = f"node:{encode_component(scope.node_id or '')}"
        elif scope.kind == "ungrouped":
            identifier = UNGROUPED_IDENTIFIER
        elif scope.kind == "collapsed":
            identifier = COLLAPSED_IDENTIFIER
        else:
            identifier = ROOT_IDENTIFIER
        key = f"{collection_key}:{identifier}"
        image_file = icon_file(db, channel, scope)
        info = _source_info(
            config,
            collection_key=collection_key,
            channel=channel,
            key=key,
            title=scope.title or label,
            homepage=first_browsable_path(db, channel, scope),
            media_types=classify_scope(
                db,
                channel,
                scope,
                max_visited=max_visited,
                timeout_seconds=timeout_seconds,
            ),
            item_count=count_items(db, channel, scope),
            has_icon=image_file is not None,
            web_base_path=web_base_path,
        )
        sources.append(KolibriSource(info, channel, scope, image_file))
    return ChannelDiscovery(channel, tuple(sources))


__all__ = [
    "COLLAPSED_IDENTIFIER",
    "DEFAULT_MAX_ENTRIES",
    "DEFAULT_MAX_SOURCES",
    "ROOT_IDENTIFIER",
    "UNGROUPED_IDENTIFIER",
    "ChannelDiscovery",
    "ChannelRow",
    "FileRow",
    "KolibriSource",
    "KolibriTraversalError",
    "NodeRow",
    "SourceScope",
    "candidate_nodes",
    "channel_thumbnail",
    "classify_scope",
    "count_items",
    "decode_component",
    "direct_leaf_present",
    "discover_channel_sources",
    "discover_channels",
    "discover_sources",
    "encode_component",
    "files_for_node",
    "first_browsable_path",
    "icon_file",
    "kolibri_collapsed_key",
    "kolibri_collection_key",
    "kolibri_node_path",
    "kolibri_node_source_key",
    "kolibri_root_key",
    "kolibri_source_key",
    "kolibri_ungrouped_key",
    "load_channel",
    "load_channels",
    "load_files_for_nodes",
    "load_node",
    "load_root_node",
    "parse_node_path",
    "select_node_files",
]
