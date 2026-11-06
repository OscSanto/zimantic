"""Shared helpers for inspected local sources.

Kiwix remote bounds and XML helpers live in
:mod:`zimantic.providers.kiwix.catalog`.
"""

import re


def _html_attribute(tag: str, name: str) -> str:
    match = re.search(
        rf"(?:^|[\s/]){re.escape(name)}\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))",
        tag,
        re.IGNORECASE,
    )
    if not match:
        return ""
    return next((value for value in match.groups() if value is not None), "")


__all__ = ["_html_attribute"]
