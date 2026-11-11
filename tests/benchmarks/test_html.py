import importlib

import pytest

benchmark = importlib.import_module("run_html")


@pytest.fixture(scope="module", autouse=True)
def optional_parsers():
    for package in ("lxml", "selectolax", "bs4"):
        pytest.importorskip(package)


@pytest.mark.parametrize("engine", benchmark.ENGINES)
@pytest.mark.parametrize("mode", benchmark.MODES)
@pytest.mark.parametrize(
    "html, expected",
    [
        (b"", ""),
        (
            (
                b'<?xml version="1.0" encoding="UTF-8"?><html><body>'
                b"<p>This paragraph has useful readable text and is long enough for indexing.</p>"
                b"</body></html>"
            ),
            "This paragraph has useful readable text and is long enough for indexing.",
        ),
        (
            (
                b"<div hidden><p>Hidden contents must never appear in readable extraction.</p></div>"
                b"<!-- A comment must not become article prose. -->"
                b"<p>This paragraph explains A &amp; B with <b>inline formatting</b><br>and line breaks.</p>"
            ),
            "This paragraph explains A & B with inline formatting and line breaks.",
        ),
    ],
)
def test_adapters_preserve_basic_extraction(engine, mode, html, expected):
    assert (
        benchmark.extract(engine, benchmark.Page("test", "page.html", html), mode)
        == expected
    )


@pytest.mark.parametrize("engine", benchmark.ENGINES)
def test_adapters_preserve_utf8_across_feed_boundary_and_preview_cap(engine):
    lead = "word " * 1636 + "a"
    html = (
        "<div><p>" + lead + "\U0001f4a1" + "</p><p>" + "tail " * 400 + "</p></div>"
    ).encode()
    page = benchmark.Page("test", "page.html", html)
    preview = benchmark.extract(engine, page, "preview")
    assert preview == "word " * 199 + "word"
    body = benchmark.extract(engine, page, "body")
    assert "\U0001f4a1" in body
    assert body.endswith("tail")
