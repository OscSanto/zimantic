"""The generic build command builds configured Kolibri channels."""

from __future__ import annotations

import argparse
import json
import types
from pathlib import Path

from zimantic.__main__ import _run_configured_build
from zimantic.config_writer import render_collection_entry
from zimantic.indexing.locator import IndexLocator
from zimantic.settings import load_config

from .helpers import kolibri_config, make_two_channel_fixture, open_runtime


def test_configured_kolibri_build_uses_the_generic_pipeline(tmp_path):
    fixture = make_two_channel_fixture(tmp_path)
    config = kolibri_config(fixture)
    index_dir = tmp_path / "indexes"
    path = Path(tmp_path) / "config.toml"
    # json.dumps escapes backslashes the same way the config writer does, so
    # absolute Windows paths stay valid TOML basic strings.
    path.write_text(
        f"index_dir = {json.dumps(str(index_dir), ensure_ascii=False)}\n"
        + render_collection_entry(
            {
                "type": "kolibri",
                "name": "kolibri",
                "path": str(fixture.home),
                "granularity": "children",
                "max_sources": 64,
                "max_entries": 500000,
            }
        ),
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
