"""The generic build command builds configured Kolibri channels."""

from __future__ import annotations

import argparse
import types
from pathlib import Path

from zimantic.__main__ import _run_configured_build
from zimantic.indexing.locator import IndexLocator
from zimantic.settings import load_config

from .helpers import kolibri_config, make_two_channel_fixture, open_runtime


def test_configured_kolibri_build_uses_the_generic_pipeline(tmp_path):
    fixture = make_two_channel_fixture(tmp_path)
    config = kolibri_config(fixture)
    index_dir = tmp_path / "indexes"
    path = Path(tmp_path) / "config.toml"
    path.write_text(
        f'index_dir = "{index_dir}"\n'
        "[[collections]]\n"
        'type = "kolibri"\n'
        'name = "kolibri"\n'
        f'path = "{fixture.home}"\n'
        'granularity = "children"\n'
        "max_sources = 64\n"
        "max_entries = 500000\n",
        encoding="utf-8",
    )
    cfg = load_config(path, warn=lambda _: None)
    args = types.SimpleNamespace(
        provider=["kolibri"],
        collection=[],
        source=[],
        list=False,
        wait_stable=False,
        fast=True,
        force=False,
        workers=1,
    )

    _run_configured_build(cfg, args, argparse.ArgumentParser())

    locator = IndexLocator(index_dir)
    runtime, batch, _runtime_config = open_runtime(index_dir, config)
    try:
        sources = list(runtime.library_sources())
        assert sources
        for source in sources:
            lease = locator.paths(source)
            assert lease is not None
            lease.close()
    finally:
        runtime.close()
        batch.close()
