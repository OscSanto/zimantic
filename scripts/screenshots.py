#!/usr/bin/env python3
"""Regenerate the README screenshots in ``docs/images``.

The script serves the ZIM files in ``evaluation.zims/`` with a temporary
configuration, so screenshots show the repository fixture instead of a
maintainer's private library. It then uses Playwright to capture the search,
library, and article views at a fixed viewport.

Examples::

    uv run --group screenshots python scripts/screenshots.py
    uv run --group screenshots python scripts/screenshots.py --only search-results
    uv run --group screenshots python scripts/screenshots.py --base-url http://127.0.0.1:8090
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "docs" / "images"
DEFAULT_ZIM_DIR = ROOT / "evaluation.zims"
SCREENSHOT_NAMES = ("search-results", "library", "article")
DEFAULT_QUERY = "diabetis"
DEFAULT_SOURCE_QUERY = "WikiMed"
DEFAULT_LABEL = "Evaluation library"
SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_name(value: str, fallback: str) -> str:
    name = SAFE_NAME.sub("-", value).strip("-._")[:63]
    return name or fallback


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _zimantic_command() -> list[str]:
    """Prefer the repository virtual environment over PATH."""
    if os.environ.get("ZIMANTIC"):
        return [os.environ["ZIMANTIC"]]
    for candidate in (
        ROOT / ".venv" / "bin" / "zimantic",
        ROOT / ".venv" / "Scripts" / "zimantic.exe",
    ):
        if candidate.is_file():
            return [str(candidate)]
    found = shutil.which("zimantic")
    if found:
        return [found]
    return [sys.executable, "-m", "zimantic"]


def _browser_executable() -> str | None:
    """Find a system Chromium when Playwright's bundled browser is missing."""
    requested = os.environ.get("ZIMANTIC_SCREENSHOT_BROWSER")
    if requested:
        return requested
    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _group_zims(paths: list[Path]) -> list[tuple[Path, str, str]]:
    """Return one collection per parent directory, in stable order."""
    groups: dict[Path, list[Path]] = {}
    for path in paths:
        groups.setdefault(path.resolve().parent, []).append(path.resolve())
    collections: list[tuple[Path, str, str]] = []
    used: set[str] = set()
    for directory in sorted(groups):
        stem = directory.name.removesuffix(".zims")
        name = _safe_name(stem, "zims")
        suffix = 2
        while name in used:
            name = f"{_safe_name(stem, 'zims')}-{suffix}"[:63]
            suffix += 1
        used.add(name)
        label = DEFAULT_LABEL if directory.name.endswith(".zims") else directory.name
        collections.append((directory, name, label))
    return collections


def _write_config(
    config_dir: Path,
    collections: list[tuple[Path, str, str]],
    port: int,
) -> Path:
    config_path = config_dir / "zimantic" / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"port = {port}", ""]
    for directory, name, label in collections:
        lines.extend(
            [
                "[[collections]]",
                'type = "zim"',
                f'name = "{name}"',
                f'label = "{label}"',
                f'path = "{directory}"',
                "",
            ]
        )
    config_path.write_text("\n".join(lines), encoding="utf-8")
    return config_path


def _json(url: str, timeout: float = 30.0):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.load(response)


def _wait_for_server(base_url: str, process: subprocess.Popen, log_path: Path) -> None:
    deadline = time.monotonic() + 180
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        try:
            health = _json(f"{base_url}/health", timeout=5)
            if health.get("status") == "ok":
                return
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
            last_error = error
        time.sleep(0.5)
    tail = ""
    if log_path.is_file():
        tail = "\n".join(log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-30:])
    raise SystemExit(
        f"zimantic serve did not become ready at {base_url}"
        + (f": {last_error}" if last_error else "")
        + (f"\n{tail}" if tail else "")
    )


def _stop_process(process: subprocess.Popen) -> None:
    """Stop the server and its worker processes, including on Windows."""
    if os.name == "nt":
        process.terminate()
    else:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except ProcessLookupError:
            return
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            process.kill()
        else:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _find_source(base_url: str, title_query: str) -> str | None:
    """Resolve a source key by title, so a scoped search does not hardcode keys."""
    if not title_query:
        return None
    query = urllib.parse.urlencode({"q": title_query, "limit": 50})
    try:
        sources = _json(f"{base_url}/sources?{query}")
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return None
    if not isinstance(sources, list):
        return None
    for source in sources:
        key = source.get("key")
        title = str(source.get("title") or "")
        if key and title_query.casefold() in title.casefold():
            return str(key)
    return None


def _wait_for_results(page) -> None:
    page.wait_for_selector(".result", timeout=180_000)
    page.wait_for_function(
        """() => {
            const status = document.querySelector("#status");
            return status && /source(s)? complete/.test(status.textContent || "");
        }""",
        timeout=180_000,
    )


def _load_search(page, base_url: str, query: str, source: str | None) -> None:
    params = {"q": query}
    if source:
        params["source"] = source
    page.goto(f"{base_url}/?{urllib.parse.urlencode(params)}", wait_until="domcontentloaded")
    _wait_for_results(page)


def _capture_search(page, base_url: str, query: str, source: str | None, out: Path) -> None:
    _load_search(page, base_url, query, source)
    page.wait_for_timeout(1_500)
    page.screenshot(path=out)
    print(f"wrote {_display_path(out)}")


def _capture_article(page, out: Path) -> None:
    page.locator(".result-title a").first.click()
    page.wait_for_selector("#articleModal:not([hidden])", timeout=60_000)
    page.wait_for_timeout(4_000)
    page.screenshot(path=out)
    print(f"wrote {_display_path(out)}")


def _capture_library(page, base_url: str, out: Path) -> None:
    page.goto(f"{base_url}/library", wait_until="domcontentloaded")
    page.wait_for_selector(".library-card", timeout=180_000)
    page.wait_for_timeout(1_500)
    page.screenshot(path=out)
    print(f"wrote {_display_path(out)}")


def _capture(
    base_url: str,
    out_dir: Path,
    names: list[str],
    query: str,
    source_query: str,
    width: int,
    height: int,
    scale: float,
    settle: float,
) -> None:
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SystemExit(
            "Playwright is required: uv run --group screenshots python scripts/screenshots.py"
        )

    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except PlaywrightError as bundled_error:
            executable = _browser_executable()
            if not executable:
                raise SystemExit(
                    "Could not launch Chromium. Run `playwright install chromium`, install a "
                    "system Chromium, or set ZIMANTIC_SCREENSHOT_BROWSER.\n"
                    f"{bundled_error}"
                )
            try:
                browser = playwright.chromium.launch(executable_path=executable)
            except PlaywrightError as error:
                raise SystemExit(
                    f"Could not launch Chromium at {executable}. Set "
                    f"ZIMANTIC_SCREENSHOT_BROWSER to another executable.\n{error}"
                )

        context = browser.new_context(
            viewport={"width": width, "height": height},
            device_scale_factor=scale,
            color_scheme="light",
            reduced_motion="reduce",
            locale="en-US",
        )
        context.add_init_script(
            "try { localStorage.setItem('zimantic.locale', 'en'); } catch (error) {}"
        )
        page = context.new_page()
        page.set_default_timeout(120_000)
        source = _find_source(base_url, source_query)
        try:
            for name in names:
                out = out_dir / f"{name}.png"
                if name == "search-results":
                    _capture_search(page, base_url, query, source, out)
                elif name == "article":
                    if not page.locator(".result").count():
                        _load_search(page, base_url, query, source)
                    _capture_article(page, out)
                elif name == "library":
                    _capture_library(page, base_url, out)
                time.sleep(settle)
        finally:
            context.close()
            browser.close()


def _default_zims() -> list[Path]:
    if not DEFAULT_ZIM_DIR.is_dir():
        return []
    return sorted(DEFAULT_ZIM_DIR.glob("*.zim"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--zim",
        action="append",
        type=Path,
        help=(
            "ZIM file to serve; repeatable. Defaults to every *.zim in "
            "evaluation.zims/. Files in the same directory share one collection."
        ),
    )
    parser.add_argument(
        "--base-url",
        help="capture an already running server instead of starting one",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=f"screenshot directory (default: {DEFAULT_OUT.relative_to(ROOT)})",
    )
    parser.add_argument(
        "--only",
        action="append",
        choices=SCREENSHOT_NAMES,
        help="capture only this image; repeatable",
    )
    parser.add_argument(
        "--query",
        default=DEFAULT_QUERY,
        help=f"search query for the search and article images (default: {DEFAULT_QUERY!r})",
    )
    parser.add_argument(
        "--source-query",
        default=DEFAULT_SOURCE_QUERY,
        help=(
            "scope the search to the first source whose title contains this text; "
            f"empty disables scoping (default: {DEFAULT_SOURCE_QUERY!r})"
        ),
    )
    parser.add_argument("--width", type=int, default=1440, help="viewport width (default: 1440)")
    parser.add_argument("--height", type=int, default=900, help="viewport height (default: 900)")
    parser.add_argument(
        "--scale",
        type=float,
        default=2.0,
        help="device scale factor for crisp HiDPI images (default: 2)",
    )
    parser.add_argument(
        "--settle",
        type=float,
        default=0.5,
        help="seconds to wait between captures (default: 0.5)",
    )
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="reuse existing indexes instead of running an incremental build first",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="server port when starting a temporary server (default: choose a free port)",
    )
    parser.add_argument("--keep-server", action="store_true", help="leave a started server running")
    args = parser.parse_args()

    out_dir = args.out.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    names = args.only or list(SCREENSHOT_NAMES)

    process: subprocess.Popen | None = None
    temp_dir: tempfile.TemporaryDirectory | None = None
    try:
        if args.base_url:
            base_url = args.base_url.rstrip("/")
        else:
            zims = args.zim or _default_zims()
            zims = [path.resolve() for path in zims if path.is_file()]
            if not zims:
                raise SystemExit(
                    f"no ZIM files found in {DEFAULT_ZIM_DIR}; pass --zim or --base-url"
                )
            collections = _group_zims(zims)
            port = args.port or _free_port()
            base_url = f"http://127.0.0.1:{port}"
            temp_dir = tempfile.TemporaryDirectory(prefix="zimantic-screenshots-")
            temp_path = Path(temp_dir.name)
            config_path = _write_config(temp_path, collections, port)
            env = os.environ.copy()
            env["XDG_CONFIG_HOME"] = str(temp_path)
            command = _zimantic_command()
            if not args.skip_build:
                print(f"building indexes for {', '.join(label for _, _, label in collections)}")
                subprocess.run(
                    [*command, "build"],
                    cwd=ROOT,
                    env=env,
                    check=True,
                )
            log_path = temp_path / "serve.log"
            log_file = log_path.open("w", encoding="utf-8")
            process = subprocess.Popen(
                [*command, "serve"],
                cwd=ROOT,
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            _wait_for_server(base_url, process, log_path)
            print(f"serving {len(zims)} ZIM file(s) from {config_path} at {base_url}")

        _capture(
            base_url,
            out_dir,
            names,
            args.query,
            args.source_query,
            args.width,
            args.height,
            args.scale,
            args.settle,
        )
    finally:
        if process is not None and not args.keep_server:
            _stop_process(process)
        if temp_dir is not None and not args.keep_server:
            temp_dir.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
