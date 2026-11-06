"""Bounded remote-response helpers used by the Kiwix provider."""

import re
import xml.etree.ElementTree as ET
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def _html_attribute(tag: str, name: str) -> str:
    match = re.search(
        rf"(?:^|[\s/]){re.escape(name)}\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+))",
        tag,
        re.IGNORECASE,
    )
    if not match:
        return ""
    return next((value for value in match.groups() if value is not None), "")


def _bounded_catalog_url(url: str, limit: int) -> str:
    """Replace an unbounded Kiwix catalog count with a large finite limit."""
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    bounded = []
    replaced = False
    for key, value in query:
        if key == "count":
            if not replaced:
                bounded.append((key, str(limit)))
                replaced = True
        else:
            bounded.append((key, value))
    if not replaced:
        bounded.append(("count", str(limit)))
    return urlunsplit(parts._replace(query=urlencode(bounded)))


def _read_limited_response(response, limit: int) -> bytes:
    """Read a remote response without allowing unbounded memory growth."""
    content_length = response.headers.get("Content-Length")
    try:
        advertised = int(content_length) if content_length else 0
    except (TypeError, ValueError):
        advertised = 0
    if advertised > limit:
        raise ValueError(f"response exceeds the {limit} byte limit")

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(min(64 * 1024, limit - total + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise ValueError(f"response exceeds the {limit} byte limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _decode_remote_xml(payload: bytes) -> str:
    """Decode common XML encodings so declarations can be rejected first."""
    if payload.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        return payload.decode("utf-32")
    if payload.startswith((b"\xff\xfe", b"\xfe\xff")):
        return payload.decode("utf-16")
    if payload.startswith(b"\xef\xbb\xbf"):
        return payload.decode("utf-8-sig")
    # BOM-less UTF-16/32 declarations are detectable from the null-byte layout.
    if payload[:4] in {b"<\x00?\x00", b"\x00<\x00?"}:
        return payload.decode("utf-16")
    if payload[:4] in {b"<\x00\x00\x00", b"\x00\x00\x00<"}:
        return payload.decode("utf-32")
    return payload.decode("utf-8")


def _parse_remote_xml(payload: bytes) -> ET.Element:
    """Parse trusted-shaped XML while rejecting DTD/entity expansion inputs."""
    text = _decode_remote_xml(payload)
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", text, re.IGNORECASE):
        raise ValueError("DTD and entity declarations are not supported")
    return ET.fromstring(text)


def _local_name(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _child_text(element: ET.Element, name: str) -> str:
    for child in element:
        if _local_name(child) == name:
            return "".join(child.itertext()).strip()
    return ""


__all__ = [
    "_bounded_catalog_url",
    "_child_text",
    "_decode_remote_xml",
    "_html_attribute",
    "_local_name",
    "_parse_remote_xml",
    "_read_limited_response",
]
