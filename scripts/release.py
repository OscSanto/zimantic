#!/usr/bin/env python3
"""Create and publish a semantic-version release for Zimantic."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
VERSION_PATTERN = re.compile(r'(?m)^version = "(\d+)\.(\d+)\.(\d+)"$')
VERSION_VALUE = re.compile(r"^\d+\.\d+\.\d+$")


def command(*args: str, capture: bool = False) -> str:
    result = subprocess.run(
        args,
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=capture,
    )
    return result.stdout.strip() if capture else ""


def project_version() -> str:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    version = data["project"]["version"]
    if not isinstance(version, str) or not VERSION_VALUE.fullmatch(version):
        raise RuntimeError(f"unsupported project version: {version!r}")
    return version


def normalize_version(value: str) -> str:
    version = value.removeprefix("v")
    if not VERSION_VALUE.fullmatch(version):
        raise ValueError("version must have the form X.Y.Z, with an optional leading v")
    return version


def next_patch(version: str) -> str:
    major, minor, patch = (int(part) for part in version.split("."))
    return f"{major}.{minor}.{patch + 1}"


def update_project_version(version: str) -> None:
    text = PYPROJECT.read_text(encoding="utf-8")
    updated, count = VERSION_PATTERN.subn(f'version = "{version}"', text, count=1)
    if count != 1:
        raise RuntimeError("could not find the project version in pyproject.toml")
    PYPROJECT.write_text(updated, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--version",
        help="release this version instead of incrementing the current patch version",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show the release that would be made without changing Git",
    )
    args = parser.parse_args()

    current = project_version()
    try:
        version = normalize_version(args.version) if args.version else next_patch(current)
    except ValueError as error:
        parser.error(str(error))
    tag = f"v{version}"

    if tag in command("git", "tag", "--list", capture=True).splitlines():
        raise RuntimeError(f"tag already exists: {tag}")
    if not args.dry_run:
        if command("git", "status", "--porcelain"):
            raise RuntimeError("release requires a clean worktree")
        if command(
            "git", "symbolic-ref", "--quiet", "--short", "HEAD", capture=True
        ) == "":
            raise RuntimeError("release requires a named branch")

    print(f"{current} -> {version} ({tag})")
    if args.dry_run:
        return 0

    update_project_version(version)
    uv = os.environ.get("UV", "uv")
    command(uv, "lock")
    command("git", "diff", "--check")
    command("git", "add", "pyproject.toml", "uv.lock")
    command("git", "commit", "-m", f"release: {tag}")
    command("git", "tag", "-a", tag, "-m", f"Release {tag}")
    command("git", "push", "origin", "HEAD")
    command("git", "push", "origin", tag)
    print(f"published {tag}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"release failed: {error}", file=sys.stderr)
        raise SystemExit(1)
