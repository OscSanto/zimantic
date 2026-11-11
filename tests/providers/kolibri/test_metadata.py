"""Optional display metadata and bounded hierarchy traversal."""

import sqlite3

from zimantic.providers.kolibri.discovery import SourceScope, load_channel
from zimantic.providers.kolibri.metadata import MetadataReader

from .helpers import make_metadata_fixture


def test_missing_optional_tables_and_columns_leave_core_metadata_available(tmp_path):
    fixture = make_metadata_fixture(tmp_path)
    db = fixture.db
    db.row_factory = sqlite3.Row
    try:
        db.executescript(
            "DROP TABLE content_contenttag;"
            "DROP TABLE content_contentnode_tags;"
            "DROP TABLE content_assessmentmetadata;"
            "DROP TABLE content_language;"
            "ALTER TABLE content_contentnode DROP COLUMN author;"
            "ALTER TABLE content_contentnode DROP COLUMN duration;"
            "ALTER TABLE content_contentnode DROP COLUMN lang_id;"
            "ALTER TABLE content_contentnode DROP COLUMN grade_levels;"
            "ALTER TABLE content_contentnode DROP COLUMN learning_activities;"
        )
        reader = MetadataReader(db, load_channel(db, "math"))
        details = reader.load(SourceScope("root", node_id="math"), ["count"])["count"]
        assert details.resource == {
            "kind": "exercise",
            "description": "Practice counting small groups of objects.",
            "topic_position": 2,
            "topic_total": 2,
        }
        assert details.breadcrumbs == (
            "Math", "Unit 1", "Counting", "Count with small numbers",
        )
    finally:
        fixture.dispose()


def test_malformed_labels_and_cyclic_or_foreign_ancestors_are_bounded(tmp_path):
    fixture = make_metadata_fixture(tmp_path)
    db = fixture.db
    db.row_factory = sqlite3.Row
    try:
        db.execute(
            "UPDATE content_contentnode SET grade_levels = '[broken', "
            "learning_activities = '[null, 123, \"VwRCom7G\", \"VwRCom7G\"]', "
            "duration = -1 WHERE id = 'count'"
        )
        db.execute("UPDATE content_contentnode SET parent_id = 'topic' WHERE id = 'unit'")
        reader = MetadataReader(db, load_channel(db, "math"))
        scope = SourceScope("root", node_id="math")
        details = reader.load(scope, ["count"])["count"]
        assert "grade_levels" not in details.resource
        assert "duration" not in details.resource
        assert details.resource["learning_activities"] == ["Practice"]
        assert details.breadcrumbs == (
            "Math", "Unit 1", "Counting", "Count with small numbers",
        )
        fixture.add_node("foreign", "other-channel", "Foreign private topic", kind="topic")
        db.execute("UPDATE content_contentnode SET parent_id = 'foreign' WHERE id = 'topic'")
        details = reader.load(scope, ["count"])["count"]
        assert details.breadcrumbs == ("Math", "Counting", "Count with small numbers")
        db.execute("UPDATE content_contentnode SET coach_content = 1 WHERE id = 'topic'")
        details = reader.load(scope, ["count"])["count"]
        assert details.breadcrumbs == ("Math", "Count with small numbers")
    finally:
        fixture.dispose()
