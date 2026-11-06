import sqlite3
from pathlib import Path

from zimantic.indexes import IndexLocator


class _Source:
    provider = "zim"
    collection_key = "zim:library"
    key = "zim:library:book-0123456789abcdef"


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
    finally:
        lease.close()

    pinned = locator.paths(source)
    assert pinned is not None
    try:
        assert pinned.generation == lease.generation
        assert pinned.sqlite_digest == lease.sqlite_digest
    finally:
        pinned.close()


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
