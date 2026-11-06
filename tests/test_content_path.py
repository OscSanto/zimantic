"""Shared content-path and request-target safety tests."""

from __future__ import annotations

import pytest

from zimantic.content.path import (
    has_encoded_separator,
    normalize_article_path,
    normalize_archive_path,
)
from zimantic.content.store import ArticleNotFoundError


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("page", "page"),
        ("docs/home/", "docs/home/"),
        ("mindtouch.page%23thumbnail", "mindtouch.page%23thumbnail"),
        ("%252e%252e/page", "%252e%252e/page"),
    ],
)
def test_valid_article_paths_are_preserved_without_second_decode(
    value, expected
):
    assert normalize_article_path(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "/absolute",
        "../escape",
        "a/../b",
        "a/./b",
        "a//b",
        "a\\b",
        "nul\x00byte",
    ],
)
def test_invalid_article_paths_fail_closed(value):
    with pytest.raises(ArticleNotFoundError):
        normalize_article_path(value)


def test_archive_href_normalization_allows_site_absolute_paths():
    assert normalize_archive_path("/docs/page") == "docs/page"
    assert normalize_archive_path("docs/../page") == "page"
    assert normalize_archive_path("../../page") is None
    assert normalize_archive_path("bad\x00path") is None


@pytest.mark.parametrize(
    ("value", "encoded"),
    [
        ("/content/files%3Amodules%3Aroot/a", False),
        ("/content/root/a%2Fb", True),
        ("/content/root/a%2fb", True),
        ("/content/root/a%5Cb", True),
        ("/content/root/a%5cb", True),
        ("/content/root/a%252Fb", False),
    ],
)
def test_encoded_separator_detection_uses_the_raw_target(value, encoded):
    assert has_encoded_separator(value) is encoded
