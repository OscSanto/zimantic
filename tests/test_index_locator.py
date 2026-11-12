import sqlite3
from pathlib import Path

import pytest

from zimantic.indexing import locator as locator_module
from zimantic.indexing.locator import IndexLocator


class _Source:
    provider = "zim"
    collection_key = "zim:library"
    key = "zim:library:book-01234567"


def _database(path: Path, value: int) -> Path:
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    connection.execute("INSERT INTO meta VALUES ('done', ?)", (str(value),))
    connection.commit()
    connection.close()
    return path


def test_publish_and_pin_an_immutable_generation(tmp_path):
    source = _Source()
    locator = IndexLocator(tmp_path)
    sqlite_path = _database(tmp_path / "source.sqlite", 1)

    lease = locator.publish(source, sqlite_path, metadata={"build": 1})
    try:
        assert lease.generation
        assert lease.sqlite_path.is_file()
        assert lease.directory.parent.name == "generations"
        assert (lease.directory / "lease").is_file()
        # The layout is derived from logical identity, never from the source
        # path or absolute root.
        directory = locator.source_directory(
            source.provider, source.collection_key, source.key
        )
        assert directory.is_relative_to(tmp_path)
        assert "tmp" not in str(directory.relative_to(tmp_path))
        assert len(directory.name.rsplit("-", 1)[1]) == 8
        assert len(directory.parent.name.rsplit("-", 1)[1]) == 8
    finally:
        lease.close()

    pinned = locator.paths(source)
    assert pinned is not None
    try:
        assert pinned.generation == lease.generation
        assert pinned.sqlite_digest == lease.sqlite_digest
    finally:
        pinned.close()


def test_multiple_shared_generation_leases_can_coexist(tmp_path):
    source = _Source()
    locator = IndexLocator(tmp_path)
    published = locator.publish(source, _database(tmp_path / "source.sqlite", 1))
    published.close()

    first = locator.paths(source)
    second = locator.paths(source)
    assert first is not None
    assert second is not None
    try:
        assert first.generation == second.generation
    finally:
        first.close()
        second.close()


def test_cleanup_keeps_the_current_generation(tmp_path):
    source = _Source()
    locator = IndexLocator(tmp_path)
    first = locator.publish(source, _database(tmp_path / "one.sqlite", 1))
    first_id = first.generation
    first.close()
    second = locator.publish(source, _database(tmp_path / "two.sqlite", 2))
    second_id = second.generation
    second.close()
    assert first_id != second_id

    locator.cleanup(source)
    directory = locator.source_directory(
        source.provider, source.collection_key, source.key
    )
    remaining = {path.name for path in (directory / "generations").iterdir()}
    assert remaining == {second_id}


def test_missing_generation_returns_none(tmp_path):
    assert IndexLocator(tmp_path).paths(_Source()) is None


def test_repeated_pins_reuse_a_verified_digest_receipt(tmp_path, monkeypatch):
    source = _Source()
    locator = IndexLocator(tmp_path)
    published = locator.publish(source, _database(tmp_path / "source.sqlite", 1))
    published.close()

    calls = []
    actual = locator_module._file_digest

    def counting(path):
        calls.append(path)
        return actual(path)

    monkeypatch.setattr(locator_module, "_file_digest", counting)
    for _ in range(2):
        lease = locator.paths(source)
        assert lease is not None
        lease.close()
    # The publish receipt already covers the unchanged files.
    assert calls == []


def test_digest_receipt_is_invalidated_when_the_file_changes(tmp_path):
    source = _Source()
    locator = IndexLocator(tmp_path)
    published = locator.publish(source, _database(tmp_path / "source.sqlite", 1))
    published.close()

    with sqlite3.connect(published.sqlite_path) as db:
        db.execute("UPDATE meta SET value = '2' WHERE key = 'done'")
    with pytest.raises(
        locator_module.IndexLocatorError, match="could not pin a stable index generation"
    ):
        locator.paths(source)
