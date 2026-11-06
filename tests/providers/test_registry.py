import pytest

from zimantic.providers import registry


class _Provider:
    kind = "test"


def test_builtin_registry_contains_zim_and_kiwix():
    kinds = [provider.kind for provider in registry.builtin_providers()]
    assert kinds == ["zim", "kiwix"]


def test_unknown_provider_is_rejected_without_a_fallback():
    with pytest.raises(Exception) as error:
        registry.get_provider("nope")
    assert "unknown provider" in str(error.value)


def test_register_adds_a_test_provider():
    registry.register(_Provider())
    try:
        assert registry.get_provider("test").kind == "test"
    finally:
        registry.reset_registry()
