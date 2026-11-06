"""In-tree provider registry.

Only providers shipped in this repository are registered.  There is no
third-party plugin discovery and no fallback provider.
"""

from __future__ import annotations

from .base import SourceProvider
from .config import ConfigValidationError, ProviderKind

_REGISTRY: dict[str, SourceProvider] | None = None


def _default_providers() -> dict[str, SourceProvider]:
    from .kiwix import KiwixProvider
    from .zim import ZimProvider

    return {
        "zim": ZimProvider(),
        "kiwix": KiwixProvider(),
    }


def _registry() -> dict[str, SourceProvider]:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _default_providers()
    return _REGISTRY


def builtin_providers() -> list[SourceProvider]:
    """Return the in-tree providers in deterministic order."""
    registry = _registry()
    return [registry[kind] for kind in ("zim", "kiwix") if kind in registry]


def get_provider(kind: ProviderKind) -> SourceProvider:
    """Return one registered provider or raise a clear configuration error."""
    provider = _registry().get(str(kind))
    if provider is None:
        raise ConfigValidationError(
            f"unknown provider {kind!r}; configured providers are "
            f"{', '.join(sorted(_registry())) or '(none)'}"
        )
    return provider


def register(provider: SourceProvider) -> None:
    """Register a test provider under its ``kind``; tests only."""
    kind = getattr(provider, "kind", None)
    if not isinstance(kind, str) or not kind:
        raise ConfigValidationError("a provider must declare a non-empty kind")
    _registry()[kind] = provider


def reset_registry() -> None:
    """Restore the in-tree registry (tests only)."""
    global _REGISTRY
    _REGISTRY = None


__all__ = [
    "builtin_providers",
    "get_provider",
    "register",
    "reset_registry",
]
