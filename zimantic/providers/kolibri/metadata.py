"""Public resource metadata read from the pinned installation snapshot."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from ...contracts import ResourceMetadata
from .discovery import ChannelRow, SourceScope, _eligible_sql, _scope_sql
from .schema import (
    ASSESSMENT_TABLE,
    GRADE_LEVEL_LABELS,
    LANGUAGE_TABLE,
    LEARNING_ACTIVITY_LABELS,
    NODE_TABLE,
    NODE_TAG_TABLE,
    TAG_TABLE,
    _table_columns,
    _table_names,
)

BATCH_SIZE = 200
MAX_ANCESTORS = 64
MAX_LABELS = 64


def _text(value: Any, limit: int = 512) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _labels(value: Any, names: dict[str, str]) -> list[str]:
    text = _text(value, 16_384)
    if not text:
        return []
    if text.startswith("["):
        try:
            values = json.loads(text)
        except (ValueError, RecursionError):
            return []
        if not isinstance(values, list):
            return []
    else:
        values = text.split(",")
    labels = []
    for item in values[:MAX_LABELS]:
        label = _text(item, 256)
        label = names.get(label, label)
        if label and label not in labels:
            labels.append(label)
    return labels


def _count(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if 0 <= number <= 2**31 - 1 else None


@dataclass(frozen=True)
class NodeMetadata:
    breadcrumbs: tuple[str, ...]
    resource: ResourceMetadata

    def indexing_text(self) -> str:
        resource = self.resource
        parts = [
            resource.get("description", ""),
            " > ".join(self.breadcrumbs),
            resource.get("kind", ""),
            resource.get("author", ""),
            resource.get("language", ""),
            ", ".join(resource.get("grade_levels", [])),
            ", ".join(resource.get("tags", [])),
            ", ".join(resource.get("learning_activities", [])),
        ]
        if "duration" in resource:
            parts.append(f"Duration: {resource['duration']} seconds")
        if "assessment_count" in resource:
            parts.append(f"Questions: {resource['assessment_count']}")
        return "\n\n".join(part for part in parts if part)


class MetadataReader:
    """Batch bounded display metadata; never read lessons or learner records."""

    def __init__(self, db: Any, channel: ChannelRow) -> None:
        self.db = db
        self.channel = channel
        tables = _table_names(db)
        self.columns = {
            table: _table_columns(db, table)
            for table in (LANGUAGE_TABLE, TAG_TABLE, NODE_TAG_TABLE, ASSESSMENT_TABLE)
            if table in tables
        }

    def _has(self, table: str, *columns: str) -> bool:
        return set(columns) <= self.columns.get(table, frozenset())

    def load(
        self,
        scope: SourceScope,
        node_ids: Iterable[str],
        *,
        include_position: bool = True,
    ) -> dict[str, NodeMetadata]:
        ids = list(dict.fromkeys(node_ids))
        result = {}
        for start in range(0, len(ids), BATCH_SIZE):
            chunk = ids[start : start + BATCH_SIZE]
            result.update(self._load_batch(scope, chunk, include_position))
        return result

    def _load_batch(
        self, scope: SourceScope, ids: list[str], include_position: bool
    ) -> dict[str, NodeMetadata]:
        placeholders = ",".join("?" for _ in ids)
        where, params = _scope_sql(scope)
        rows = self.db.execute(
            f"SELECT n.* FROM {NODE_TABLE} n "
            f"WHERE n.id IN ({placeholders}) AND n.channel_id = ? "
            f"AND {where} AND {_eligible_sql()}",
            (*ids, self.channel.id, *params),
        ).fetchall()
        if not rows:
            return {}
        resources: dict[str, ResourceMetadata] = {}
        for row in rows:
            values = dict(row)
            resource: ResourceMetadata = {"kind": _text(values.get("kind"))}
            for key, limit in (("description", 65_536), ("author", 512)):
                text = _text(values.get(key), limit)
                if text:
                    resource[key] = text
            for key, names in (
                ("grade_levels", GRADE_LEVEL_LABELS),
                ("learning_activities", LEARNING_ACTIVITY_LABELS),
            ):
                labels = _labels(values.get(key), names)
                if labels:
                    resource[key] = labels
            duration = _count(values.get("duration"))
            if duration is not None and duration > 0:
                resource["duration"] = duration
            language = _text(values.get("lang_id"))
            if language:
                resource["language"] = language
            resources[str(row["id"])] = resource
        self._languages(resources)
        self._tags(resources)
        self._assessments(resources)
        if include_position:
            self._positions(rows, resources)
        ancestors = self._ancestors(rows)
        result = {}
        for row in rows:
            trail = []
            seen = {str(row["id"])}
            parent_id = row["parent_id"]
            for _ in range(MAX_ANCESTORS):
                if not parent_id or parent_id in seen:
                    break
                seen.add(parent_id)
                parent = ancestors.get(parent_id)
                if parent is None:
                    break
                if parent_id != self.channel.root_id and _text(parent["title"]):
                    trail.append(_text(parent["title"]))
                parent_id = parent["parent_id"]
            trail.reverse()
            breadcrumbs = [
                _text(self.channel.name),
                *trail,
                _text(row["title"]),
            ]
            result[str(row["id"])] = NodeMetadata(
                tuple(part for part in breadcrumbs if part),
                resources[str(row["id"])],
            )
        return result

    def _ancestors(self, rows: list[Any]) -> dict[str, Any]:
        ancestors: dict[str, Any] = {}
        pending = {row["parent_id"] for row in rows if row["parent_id"]}
        visited: set[str] = set()
        for _ in range(MAX_ANCESTORS):
            pending -= visited
            if not pending:
                break
            visited.update(pending)
            parents = []
            ids = sorted(pending)
            for start in range(0, len(ids), BATCH_SIZE):
                chunk = ids[start : start + BATCH_SIZE]
                placeholders = ",".join("?" for _ in chunk)
                parents.extend(self.db.execute(
                    f"SELECT id, parent_id, title FROM {NODE_TABLE} "
                    f"WHERE id IN ({placeholders}) AND channel_id = ? "
                    "AND kind = 'topic' AND COALESCE(coach_content, 0) = 0",
                    (*chunk, self.channel.id),
                ).fetchall())
            ancestors.update((str(row["id"]), row) for row in parents)
            pending = {row["parent_id"] for row in parents if row["parent_id"]}
        return ancestors

    def _languages(self, resources: dict[str, ResourceMetadata]) -> None:
        if not self._has(LANGUAGE_TABLE, "id", "lang_name"):
            return
        ids = sorted({item["language"] for item in resources.values() if item.get("language")})
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        names = {
            str(row["id"]): _text(row["lang_name"])
            for row in self.db.execute(
                f"SELECT id, lang_name FROM {LANGUAGE_TABLE} WHERE id IN ({placeholders})",
                ids,
            )
        }
        for resource in resources.values():
            language = resource.get("language")
            if language and names.get(language):
                resource["language"] = names[language]

    def _tags(self, resources: dict[str, ResourceMetadata]) -> None:
        if not (
            self._has(TAG_TABLE, "id", "tag_name")
            and self._has(NODE_TAG_TABLE, "contentnode_id", "contenttag_id")
        ):
            return
        placeholders = ",".join("?" for _ in resources)
        rows = self.db.execute(
            f"SELECT links.contentnode_id, tags.tag_name FROM {NODE_TAG_TABLE} links "
            f"JOIN {TAG_TABLE} tags ON tags.id = links.contenttag_id "
            f"WHERE links.contentnode_id IN ({placeholders}) ORDER BY tags.tag_name",
            tuple(resources),
        )
        for row in rows:
            tag = _text(row["tag_name"], 256)
            if not tag:
                continue
            tags = resources[str(row["contentnode_id"])].setdefault("tags", [])
            if len(tags) < MAX_LABELS and tag not in tags:
                tags.append(tag)

    def _assessments(self, resources: dict[str, ResourceMetadata]) -> None:
        if not self._has(ASSESSMENT_TABLE, "contentnode_id", "number_of_assessments"):
            return
        placeholders = ",".join("?" for _ in resources)
        rows = self.db.execute(
            f"SELECT contentnode_id, MAX(number_of_assessments) AS count "
            f"FROM {ASSESSMENT_TABLE} WHERE contentnode_id IN ({placeholders}) "
            "GROUP BY contentnode_id",
            tuple(resources),
        )
        for row in rows:
            count = _count(row["count"])
            if count is not None:
                resources[str(row["contentnode_id"])]["assessment_count"] = count

    def _positions(self, rows: list[Any], resources: dict[str, ResourceMetadata]) -> None:
        parents = sorted({row["parent_id"] for row in rows if row["parent_id"]})
        if not parents:
            return
        placeholders = ",".join("?" for _ in parents)
        targets = ",".join("?" for _ in resources)
        positions = self.db.execute(
            "SELECT * FROM (SELECT n.id, "
            "ROW_NUMBER() OVER (PARTITION BY n.parent_id ORDER BY n.lft, n.id) AS position, "
            "COUNT(*) OVER (PARTITION BY n.parent_id) AS total "
            f"FROM {NODE_TABLE} n WHERE n.parent_id IN ({placeholders}) "
            f"AND n.channel_id = ? AND {_eligible_sql()}) "
            f"WHERE id IN ({targets})",
            (*parents, self.channel.id, *resources),
        )
        for row in positions:
            resources[str(row["id"])]["topic_position"] = int(row["position"])
            resources[str(row["id"])]["topic_total"] = int(row["total"])
