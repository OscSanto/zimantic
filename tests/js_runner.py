"""Shared helpers for executing browser-side JavaScript under node."""

from __future__ import annotations

import shutil
import subprocess

NODE = shutil.which("node")


def run_node(script: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a JavaScript snippet with node, feeding the source through stdin.

    ``node -e <script>`` puts the whole snippet on the command line.  Windows
    caps that command line at about 32K characters, so the app.js harness fails
    with ``WinError 206`` for the larger tests.  Stdin has no such limit.
    """
    if NODE is None:  # pragma: no cover - the test classes skip without node
        raise RuntimeError("node is unavailable")
    return subprocess.run(
        [NODE, "-"],
        input=script,
        check=check,
        encoding="utf-8",
        capture_output=True,
    )
