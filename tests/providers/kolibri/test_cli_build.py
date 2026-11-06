"""The generic build command builds configured Kolibri channels."""

from __future__ import annotations

from types import SimpleNamespace

from zimantic.__main__ import _build_kolibri_collections
from zimantic.indexes.locator import IndexLocator

from .helpers import kolibri_config, make_two_channel_fixture, runtime_config


def test_build_kolibri_collections_uses_the_generic_pipeline(tmp_path):
    fixture = make_two_channel_fixture(tmp_path)
    config = kolibri_config(fixture)
    index_dir = tmp_path / "indexes"
    runtime_config_value = runtime_config(index_dir, config)
    cfg = {
        "index_dir": str(index_dir),
        "collections": (config,),
        "runtime_config": runtime_config_value,
        "web_base_path": "",
    }
    failures: list = []

    _build_kolibri_collections(
        cfg,
        SimpleNamespace(fast=True, force=False),
        None,
        None,
        failures,
    )

    assert failures == []
    locator = IndexLocator(index_dir)
    from zimantic.providers.kolibri import KolibriProvider
    from .helpers import context

    batch = KolibriProvider().discover(
        config, context(index_dir, runtime_config_value)
    )
    try:
        for collection in batch.collections:
            runtime = batch.open(collection)
            try:
                for source in runtime.library_sources():
                    lease = locator.paths(source)
                    assert lease is not None
                    lease.close()
            finally:
                runtime.close()
    finally:
        batch.close()
