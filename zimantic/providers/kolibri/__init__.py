"""Kolibri provider: one collection per installed channel."""

from .discovery import (
    ChannelDiscovery,
    ChannelRow,
    FileRow,
    KolibriSource,
    NodeRow,
    SourceScope,
    discover_channel_sources,
    discover_channels,
    discover_sources,
    kolibri_collapsed_key,
    kolibri_collection_key,
    kolibri_node_path,
    kolibri_node_source_key,
    kolibri_root_key,
    kolibri_source_key,
    kolibri_ungrouped_key,
    load_channels,
)
from .entries import KolibriEntryProducer, row_id_for
from .provider import KolibriDiscoveryBatch, KolibriProvider, KolibriRuntime
from .schema import SUPPORTED_KOLIBRI_VERSION, KolibriSchemaError, detect_schema
from .snapshot import KolibriSnapshot, SnapshotError, SnapshotManager
from .store import KolibriContentStore

__all__ = [
    "SUPPORTED_KOLIBRI_VERSION",
    "ChannelDiscovery",
    "ChannelRow",
    "FileRow",
    "KolibriContentStore",
    "KolibriDiscoveryBatch",
    "KolibriEntryProducer",
    "KolibriProvider",
    "KolibriRuntime",
    "KolibriSchemaError",
    "KolibriSnapshot",
    "KolibriSource",
    "NodeRow",
    "SnapshotError",
    "SnapshotManager",
    "SourceScope",
    "detect_schema",
    "discover_channel_sources",
    "discover_channels",
    "discover_sources",
    "kolibri_collapsed_key",
    "kolibri_collection_key",
    "kolibri_node_path",
    "kolibri_node_source_key",
    "kolibri_root_key",
    "kolibri_source_key",
    "kolibri_ungrouped_key",
    "load_channels",
    "row_id_for",
]
