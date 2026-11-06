"""In-tree provider implementations and their shared contracts."""

from . import base, config, registry
from .base import (
    CollectionInfo,
    CollectionRuntime,
    CollectionSnapshot,
    DiscoveryBatch,
    ProviderContext,
    SourceProvider,
    SourceQuery,
)
from .config import (
    ConfigValidationError,
    FilesConfig,
    KiwixConfig,
    KolibriConfig,
    ProviderConfig,
    ProviderKind,
    RuntimeConfig,
    TraversalLimits,
    ZimConfig,
    parse_collections,
)

__all__ = [
    "CollectionInfo",
    "CollectionRuntime",
    "CollectionSnapshot",
    "ConfigValidationError",
    "DiscoveryBatch",
    "FilesConfig",
    "KiwixConfig",
    "KolibriConfig",
    "ProviderConfig",
    "ProviderContext",
    "ProviderKind",
    "RuntimeConfig",
    "SourceProvider",
    "SourceQuery",
    "TraversalLimits",
    "ZimConfig",
    "base",
    "config",
    "parse_collections",
    "registry",
]
