#!/usr/bin/env python3
"""Compare HTML backends while retaining Zimantic's text-selection rules.

Run with the project dependencies plus lxml, selectolax and beautifulsoup4:

    python tests/benchmarks/run_html.py --zim archive.zim --output /tmp/html-benchmark

Inputs are preloaded, so timings exclude archive decompression and file I/O.
The report includes actual capped body/preview extraction, not just DOM parsing.
Native DOM backends parse the entire input before traversal; lxml-target feeds
incrementally. Adapters are prototypes, not optimized production replacements.
Review samples are blinded; inspect review.json before opening review-key.json.
"""

from __future__ import annotations

import argparse
import codecs
import gc
import hashlib
import importlib
import importlib.metadata
import json
import platform
import random
import statistics
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

reader = importlib.import_module("zimantic.content.reader")


ENGINES = ("stdlib", "lxml-target", "lxml-dom", "lexbor", "bs4-lxml")
CHUNK = 8192
MODES = {
    "body": reader.DEFAULT_MAX_BODY_TEXT_BYTES,
    "preview": reader.DEFAULT_PREVIEW_CHARS,
}


@dataclass
class Page:
    source: str
    path: str
    html: bytes


class LegacyTextExtractor(reader._TextExtractor, HTMLParser):
    """Keep the pre-Lexbor streaming baseline with the same selection rules."""

    def __init__(self):
        reader._TextExtractor.__init__(self)
        HTMLParser.__init__(self)


class Target:
    def __init__(self):
        self.extractor = reader._TextExtractor()

    def start(self, tag, attributes, _namespaces=None):
        self.extractor.handle_starttag(tag, attributes.items())

    def end(self, tag):
        self.extractor.handle_endtag(tag)

    def data(self, data):
        self.extractor.handle_data(data)

    def close(self):
        return self.extractor


def blocks(engine: str, html: bytes, limit: int) -> list[str]:
    if engine == "lexbor":
        return list(reader.iter_text_blocks(html, max_chars=limit))
    if engine == "stdlib":
        parser = LegacyTextExtractor()
        cleaned = reader._SCRIPT_STYLE.sub(b" ", html)
        decoder = codecs.getincrementaldecoder("utf-8")("ignore")
        for offset in range(0, len(cleaned), CHUNK):
            parser.feed(decoder.decode(cleaned[offset : offset + CHUNK]))
            if parser._can_stop(limit):
                break
        parser.feed(decoder.decode(b"", final=True))
        return parser.candidates()

    from lxml import etree

    target = Target()
    cleaned = reader._SCRIPT_STYLE.sub(b" ", html)
    if engine == "lxml-target":
        parser = etree.HTMLParser(target=target, encoding="utf-8", no_network=True)
        decoder = codecs.getincrementaldecoder("utf-8")("ignore")
        for offset in range(0, len(cleaned), CHUNK):
            parser.feed(decoder.decode(cleaned[offset : offset + CHUNK]))
            if target.extractor._can_stop(limit):
                # Closing synthesizes end tags for the unfed suffix. Avoid that
                # when stopping early, as the stdlib baseline leaves it open.
                break
        else:
            parser.feed(decoder.decode(b"", final=True))
            parser.close()
        return target.extractor.candidates()

    text = cleaned.decode("utf-8", "ignore")
    if engine == "lxml-dom":
        root = etree.HTML(
            text.encode("utf-8"),
            parser=etree.HTMLParser(encoding="utf-8", no_network=True),
        )
        if root is None:
            return []
        for event, node in etree.iterwalk(root, events=("start", "end", "comment")):
            if event == "start":
                target.start(node.tag, node.attrib)
                if node.text:
                    target.data(node.text)
            elif event == "end":
                target.end(node.tag)
                if node.tail:
                    target.data(node.tail)
            elif node.tail:
                target.data(node.tail)
            if event == "end" and target.extractor._can_stop(limit):
                break
    elif engine == "bs4-lxml":
        from bs4 import (
            BeautifulSoup,
            Comment,
            Declaration,
            Doctype,
            NavigableString,
            Tag,
        )

        tree = BeautifulSoup(text, "lxml")
        stack = [(tree, False)]
        while stack:
            node, closing = stack.pop()
            if isinstance(node, Tag):
                if closing:
                    target.end(node.name)
                    if target.extractor._can_stop(limit):
                        break
                else:
                    target.start(
                        node.name,
                        {
                            key: " ".join(value) if isinstance(value, list) else value
                            for key, value in node.attrs.items()
                        },
                    )
                    stack.append((node, True))
                    stack.extend((child, False) for child in reversed(node.contents))
            elif isinstance(node, NavigableString) and not isinstance(
                node, (Comment, Declaration, Doctype)
            ):
                target.data(str(node))
    else:
        raise ValueError(engine)
    return target.extractor.candidates()


def extract(engine: str, page: Page, mode: str) -> str:
    limit = MODES[mode]
    candidates = blocks(engine, page.html, limit)
    if not candidates:
        return ""
    text = reader._join_blocks(candidates, limit)
    return reader.truncate_utf8(text, limit)[0] if mode == "body" else text


def pick_pages(pool: list[Page], count: int, rng: random.Random) -> list[Page]:
    # Mix random pages with large pages, without claiming the latter are the
    # largest in the entire archive (only in the sampled pool).
    largest = sorted(pool, key=lambda page: len(page.html), reverse=True)[: count // 3]
    remaining = [page for page in pool if page not in largest]
    return largest + rng.sample(remaining, min(count - len(largest), len(remaining)))


def load_pages(args) -> list[Page]:
    from libzim.reader import Archive

    rng = random.Random(args.seed)
    pages = []
    for filename in args.zim:
        try:
            archive = Archive(str(filename))
        except RuntimeError as error:
            raise RuntimeError(
                f"Cannot open benchmark input {filename}: {error}"
            ) from error
        pool = []
        seen = set()
        for entry_id in rng.sample(
            range(archive.entry_count), min(archive.entry_count, args.pages * 200)
        ):
            entry = archive.get_entry_by_id(entry_id)
            if entry.is_redirect or reader._HTML_ASSET_PATH.search(entry.path):
                continue
            item = entry.get_item()
            if item.mimetype not in ("text/html", "application/xhtml+xml"):
                continue
            if item.size > reader.DEFAULT_MAX_HTML_BYTES:
                continue
            html = bytes(item.content)
            digest = hashlib.sha256(html).digest()
            if digest in seen:
                continue
            seen.add(digest)
            pool.append(Page(filename.name, entry.path, html))
            if len(pool) >= args.pages * 3:
                break
        pages.extend(pick_pages(pool, args.pages, rng))
    for directory in args.module:
        files = sorted(
            path
            for path in directory.rglob("*")
            if path.suffix.lower() in (".html", ".htm", ".xhtml", ".htmlf")
            and "node_modules" not in path.parts
            and path.stat().st_size <= reader.DEFAULT_MAX_HTML_BYTES
        )
        pool = [
            Page(directory.name, str(path.relative_to(directory)), path.read_bytes())
            for path in rng.sample(files, min(len(files), args.pages * 3))
        ]
        pages.extend(pick_pages(pool, args.pages, rng))
    return pages


def stress_pages() -> list[Page]:
    prose = "Useful article prose explains the scientific principle in clear readable sentences. "
    hidden = (
        "Navigation and hidden boilerplate must not appear in the extracted article. "
    )
    cases = {
        "omitted-p-end-tags": f"<div><p>{prose}<p>Second paragraph. {prose}</div>",
        "unclosed-hidden-container": f"<nav><a>Home</a><div>Menu</nav><p>{prose}",
        "invalid-p-div-nesting": f"<p>{prose}<div>{prose}</div>End of paragraph.</p>",
        "entities-inline-breaks": f"<p>{prose}A&nbsp;B &amp; C &NotEqualTilde; <b>bold</b><br>Next line.</p>",
        "hidden-and-chrome": f"<div hidden><p>{hidden}</p></div><aside>{hidden}</aside><p>{prose}</p>",
        "parallel-translations": (
            f'<p class="lang-fr">Texte traduit. {prose}</p>'
            f'<p class="lang-default">Primary language. {prose}</p>'
        ),
        "deep-valid-containers": "<div>" * 260 + f"<p>{prose}</p>" + "</div>" * 260,
        "large-preview-tail": f"<div><p>{prose * 20}</p>"
        + f"<p>{prose}</p>" * 3000
        + "</div>",
    }
    return [Page("stress", path, html.encode()) for path, html in cases.items()]


def make_review(pages, outputs, destination, seed):
    rng = random.Random(seed + 1)
    different = [
        index
        for index, page in enumerate(pages)
        if page.source != "stress"
        and len({outputs[engine]["body"][index] for engine in ENGINES}) > 1
    ]
    different.sort(
        key=lambda index: max(
            abs(
                len(outputs[engine]["body"][index])
                - len(outputs["stdlib"]["body"][index])
            )
            for engine in ENGINES
        ),
        reverse=True,
    )
    selected = different[:6]
    same = [
        index
        for index, page in enumerate(pages)
        if page.source != "stress"
        and index not in selected
        and outputs["stdlib"]["body"][index]
    ]
    selected += rng.sample(same, min(4, len(same)))
    selected += [index for index, page in enumerate(pages) if page.source == "stress"]
    review, key = [], {}
    for case, index in enumerate(selected, 1):
        page = pages[index]
        engines = list(ENGINES)
        rng.shuffle(engines)
        key[str(case)] = dict(zip("ABCDE", engines, strict=True))
        groups = {}
        for label, engine in key[str(case)].items():
            body = outputs[engine]["body"][index]
            preview = outputs[engine]["preview"][index]
            identity = (body, preview)
            body_file = destination / f"review-{case:02d}-{label}.txt"
            if identity not in groups:
                body_file.write_text(body, encoding="utf-8")
            group = groups.setdefault(
                identity,
                {
                    "labels": [],
                    "body_chars": len(body),
                    "body_file": str(body_file),
                    "body_start": body[:5000],
                    "body_end": body[-1500:] if len(body) > 5000 else "",
                    "preview": preview,
                },
            )
            group["labels"].append(label)
        source_file = destination / f"review-{case:02d}.html"
        source_file.write_bytes(page.html)
        review.append(
            {
                "case": case,
                "source": page.source,
                "path": page.path,
                "source_file": str(source_file),
                "variants": list(groups.values()),
            }
        )
    (destination / "review.json").write_text(
        json.dumps(review, ensure_ascii=False, indent=2)
    )
    (destination / "review-key.json").write_text(json.dumps(key, indent=2))


def peak_rss_mib():
    if sys.platform.startswith("linux"):
        # getrusage can retain a pre-exec high-water mark from the parent.
        # procfs reports this worker's own post-exec high-water mark instead.
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmHWM:"):
                return int(line.split()[1]) / 1024
        raise RuntimeError("VmHWM is unavailable")
    import resource

    scale = 2**20 if sys.platform == "darwin" else 1024
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / scale


def memory_worker(args):
    pages = [Page("memory", str(path), path.read_bytes()) for path in args.memory_file]
    gc.collect()
    baseline = peak_rss_mib()
    for mode in MODES:
        for page in pages:
            extract(args.memory_engine, page, mode)
    peak = peak_rss_mib()
    print(
        json.dumps(
            {
                "baseline_peak_mib": baseline,
                "peak_mib": peak,
                "increase_mib": peak - baseline,
            }
        )
    )


def measure_memory(pages, destination):
    # Fresh processes include native allocations, unlike tracemalloc. Import
    # all backends in every worker so the increment measures extraction rather
    # than library import overhead. High-water increments are approximate.
    selected = sorted(pages, key=lambda page: len(page.html), reverse=True)[:8]
    files = []
    for index, page in enumerate(selected):
        path = destination / f"memory-{index:02d}.html"
        path.write_bytes(page.html)
        files.append(path)
    results = {}
    for engine in ENGINES:
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--memory-engine",
            engine,
            "--output",
            str(destination),
        ]
        for path in files:
            command.extend(("--memory-file", str(path)))
        process = subprocess.run(command, capture_output=True, text=True, check=True)
        results[engine] = json.loads(process.stdout)
    return {
        "method": "Fresh process per engine; eight largest corpus inputs; body and preview; all backends pre-imported.",
        "input_paths": [page.path for page in selected],
        "results": results,
    }


def run(args):
    # Import before timing. These imports also fail clearly if benchmark-only
    # dependencies have not been installed.
    for module in ("bs4", "lxml.etree", "selectolax.lexbor"):
        importlib.import_module(module)
    etree = importlib.import_module("lxml.etree")
    if args.memory_engine:
        memory_worker(args)
        return

    pages = load_pages(args) + stress_pages()
    outputs = {engine: {} for engine in ENGINES}
    timings = {engine: {} for engine in ENGINES}
    rng = random.Random(args.seed)
    for mode in MODES:
        for engine in ENGINES:
            outputs[engine][mode] = [extract(engine, page, mode) for page in pages]
            timings[engine][mode] = [[] for _ in pages]
        for _ in range(args.repeats):
            engines = list(ENGINES)
            rng.shuffle(engines)
            for engine in engines:
                gc.collect()
                for index, page in enumerate(pages):
                    started = time.perf_counter()
                    result = extract(engine, page, mode)
                    elapsed = time.perf_counter() - started
                    if result != outputs[engine][mode][index]:
                        raise RuntimeError(
                            f"Non-deterministic output: {engine} {page.path}"
                        )
                    timings[engine][mode][index].append(elapsed)

    report = {
        "environment": {
            "python": sys.version,
            "platform": platform.platform(),
            "versions": {
                name: importlib.metadata.version(name)
                for name in ("lxml", "selectolax", "beautifulsoup4")
            },
            "libxml": etree.LIBXML_VERSION,
        },
        "seed": args.seed,
        "repeats": args.repeats,
        "inputs": {
            "zims": [str(path) for path in args.zim],
            "modules": [str(path) for path in args.module],
            "pages_per_input": args.pages,
            "max_html_bytes": reader.DEFAULT_MAX_HTML_BYTES,
        },
        "method": "Per-page median after warmup; sum medians. I/O excluded; same rules and UTF-8 ignore decoding.",
        "corpus": dict(Counter(page.source for page in pages)),
        "pages": [
            {
                "source": page.source,
                "path": page.path,
                "bytes": len(page.html),
                "sha256": hashlib.sha256(page.html).hexdigest(),
            }
            for page in pages
        ],
        "results": {},
    }
    real = [index for index, page in enumerate(pages) if page.source != "stress"]
    if not real:
        raise ValueError("Supply at least one --zim or --module containing HTML")
    for mode in MODES:
        baseline = sum(
            statistics.median(timings["stdlib"][mode][index]) for index in real
        )
        for engine in ENGINES:
            medians = [
                statistics.median(timings[engine][mode][index]) for index in real
            ]
            total = sum(medians)
            report["results"].setdefault(engine, {})[mode] = {
                "seconds": total,
                "speedup": baseline / total,
                "p50_ms": statistics.median(medians) * 1000,
                "p95_ms": sorted(medians)[int((len(medians) - 1) * 0.95)] * 1000,
                "exact_matches": sum(
                    outputs[engine][mode][index] == outputs["stdlib"][mode][index]
                    for index in real
                ),
                "empty": sum(not outputs[engine][mode][index] for index in real),
                "lost_nonempty": sum(
                    bool(outputs["stdlib"][mode][index])
                    and not outputs[engine][mode][index]
                    for index in real
                ),
                "sources": {
                    source: sum(
                        statistics.median(timings[engine][mode][index])
                        for index in real
                        if pages[index].source == source
                    )
                    for source in report["corpus"]
                    if source != "stress"
                },
            }
    report["quality"] = [
        {
            "source": page.source,
            "path": page.path,
            "engines": {
                engine: {
                    mode: {
                        "chars": len(outputs[engine][mode][index]),
                        "sha256": hashlib.sha256(
                            outputs[engine][mode][index].encode()
                        ).hexdigest(),
                        "seconds": statistics.median(timings[engine][mode][index]),
                    }
                    for mode in MODES
                }
                for engine in ENGINES
            },
        }
        for index, page in enumerate(pages)
    ]
    args.output.mkdir(parents=True, exist_ok=True)
    report["memory"] = measure_memory(pages, args.output)
    (args.output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2)
    )
    make_review(pages, outputs, args.output, args.seed)
    print(
        json.dumps(
            {
                "pages": len(real),
                "corpus": report["corpus"],
                "results": report["results"],
            },
            indent=2,
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zim", type=Path, action="append", default=[])
    parser.add_argument("--module", type=Path, action="append", default=[])
    parser.add_argument(
        "--pages",
        type=int,
        default=12,
        help="Pages per input, including large sampled pages",
    )
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--seed", type=int, default=20261107)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--memory-engine", choices=ENGINES, help=argparse.SUPPRESS)
    parser.add_argument(
        "--memory-file", type=Path, action="append", default=[], help=argparse.SUPPRESS
    )
    args = parser.parse_args()
    if args.pages < 1 or args.repeats < 1:
        parser.error("--pages and --repeats must be positive")
    if not args.memory_engine and not (args.zim or args.module):
        parser.error("provide at least one --zim or --module input")
    if args.memory_engine and not args.memory_file:
        parser.error("memory workers require --memory-file inputs")
    run(args)


if __name__ == "__main__":
    main()
