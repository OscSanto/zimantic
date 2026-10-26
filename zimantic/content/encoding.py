"""Best-effort decoding for ordinary text files.

Text outside ZIMs is frequently not UTF-8: a saved email, a Windows-1252
Markdown file or a Japanese subtitle.  Decoding those with ``errors="ignore"``
silently drops exactly the characters a search needs, so this module samples
the bytes, asks ``chardet`` when it is confident, and scores the candidate
decodings before committing to one.
"""
from __future__ import annotations

import codecs

# Try the likely encodings in a useful order.  UTF-8 first because it is by far
# the most common and a successful strict decode is definitive.  The rest also
# act as a preference order when two legacy encodings score equally.
COMMON_ENCODINGS = (
    "utf-8-sig",
    "utf-8",
    "utf-16",
    "utf-16-le",
    "utf-16-be",
    "utf-32",
    "utf-32-le",
    "utf-32-be",
    "windows-1252",
    "windows-1250",
    "windows-1251",
    "windows-1256",
    "windows-1253",
    "windows-1255",
    "windows-1254",
    "windows-1257",
    "gb18030",
    "big5",
    "shift_jis",
    "euc-jp",
    "cp949",
)

# A short sample is enough for detection; decoding the whole file still uses the
# selected encoding below.
SAMPLE_BYTES = 1 << 20

# Characters and blocks that are almost never part of ordinary prose.  Arabic
# presentation forms (FE70-FEFF), private use and control characters are strong
# signals that a legacy encoding was guessed incorrectly.
_PRESENTATION_RANGES = (
    (0x2500, 0x257F),  # box drawing: common output of a wrong single-byte guess
    (0xE000, 0xF8FF),  # private use
    (0xFE70, 0xFEFF),  # Arabic presentation forms
    (0xF0000, 0xFFFFD),
    (0x100000, 0x10FFFD),
)


def _strict_decode(raw: bytes, encoding: str) -> str | None:
    try:
        return raw.decode(encoding)
    except (LookupError, UnicodeDecodeError):
        return None


def _script(char: str) -> str | None:
    code = ord(char)
    for start, end in _PRESENTATION_RANGES:
        if start <= code <= end:
            return "suspicious"
    if 0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF:
        return "han"
    if 0x3040 <= code <= 0x30FF:
        return "japanese"
    if 0xAC00 <= code <= 0xD7AF:
        return "hangul"
    if 0x0400 <= code <= 0x04FF:
        return "cyrillic"
    if 0x0370 <= code <= 0x03FF:
        return "greek"
    if 0x0590 <= code <= 0x05FF:
        return "hebrew"
    if 0x0600 <= code <= 0x06FF:
        return "arabic"
    if code <= 0x024F:
        return "latin"
    return None


def _score(text: str, encoding: str) -> float:
    """Prefer a coherent script and common encodings over exotic ones."""
    letters = [char for char in text if char.isalpha()]
    if not letters:
        return -1.0
    counts: dict[str, int] = {}
    suspicious = 0
    control = 0
    for char in text:
        script = _script(char)
        if script == "suspicious":
            suspicious += 1
        elif script and char.isalpha():
            counts[script] = counts.get(script, 0) + 1
        elif char.isprintable() or char.isspace():
            continue
        else:
            control += 1
    if not counts:
        return -1.0
    consistency = max(counts.values()) / len(letters)
    penalty = 1.5 * suspicious / max(1, len(letters))
    penalty += 2.0 * control / max(1, len(text))
    try:
        preference = COMMON_ENCODINGS.index(encoding)
    except ValueError:
        preference = len(COMMON_ENCODINGS)
    # A small tie-breaker, not a replacement for script consistency.
    bonus = max(0.0, (len(COMMON_ENCODINGS) - preference)) * 0.001
    return consistency - penalty + bonus


def _dominant_script(text: str) -> str | None:
    counts: dict[str, int] = {}
    for char in text:
        if not char.isalpha():
            continue
        script = _script(char)
        if script and script != "suspicious":
            counts[script] = counts.get(script, 0) + 1
    if not counts:
        return None
    return max(counts, key=counts.__getitem__)


def _ascii_letter_ratio(text: str) -> float:
    letters = [char for char in text if char.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for char in letters if ord(char) < 128) / len(letters)


def _normalize_encoding(name: str | None) -> str | None:
    if not name:
        return None
    folded = name.strip().lower().replace("_", "-")
    aliases = {
        "ascii": "utf-8",
        "iso-8859-1": "windows-1252",
        "latin-1": "windows-1252",
        "latin1": "windows-1252",
        "gb2312": "gb18030",
        "gbk": "gb18030",
        "shift-jis": "shift_jis",
        "euc-jp": "euc-jp",
        "windows-1250": "windows-1250",
        "windows-1251": "windows-1251",
        "windows-1252": "windows-1252",
        "windows-1253": "windows-1253",
        "windows-1254": "windows-1254",
        "windows-1255": "windows-1255",
        "windows-1256": "windows-1256",
        "windows-1257": "windows-1257",
    }
    return aliases.get(folded, name)


def _aware_detection(sample: bytes) -> str | None:
    """Use chardet when it is confident; it recognizes short CJK samples well."""
    try:
        import chardet
    except ImportError:  # pragma: no cover - optional accelerator
        return None
    try:
        result = chardet.detect(sample)
    except Exception:
        return None
    if not isinstance(result, dict):
        return None
    try:
        confidence = float(result.get("confidence") or 0.0)
    except (TypeError, ValueError):
        return None
    language = str(result.get("language") or "").lower()
    # chardet's confidence scales differ between versions, and its CJK
    # recognition is reliable even when the absolute number is low. For
    # Latin/Cyrillic text the script scorer below is less easily fooled by a
    # short sample, so only trust a high-confidence non-CJK answer.
    cjk = language in {"ja", "jp", "zh", "zh-cn", "zh-tw", "ko", "kr"}
    if confidence < (0.3 if cjk else 0.7):
        return None
    encoding = _normalize_encoding(result.get("encoding"))
    if encoding and _strict_decode(sample, encoding) is not None:
        return encoding
    return None


def _choose_encoding(raw: bytes) -> str | None:
    sample = raw[:SAMPLE_BYTES]
    aware = _aware_detection(sample)
    if aware is not None:
        return aware

    scored: list[tuple[float, str, str]] = []
    for encoding in COMMON_ENCODINGS:
        decoded = _strict_decode(sample, encoding)
        if decoded is None:
            continue
        scored.append((_score(decoded, encoding), encoding, decoded))
    if not scored:
        return None
    scored.sort(key=lambda item: item[0], reverse=True)

    # Western legacy text usually contains a healthy share of ASCII letters.
    # When a Latin decoding is mostly accented/symbolic, the bytes are more
    # likely a Cyrillic (or other non-Latin) single-byte encoding.
    latin = next(
        (item for item in scored if _dominant_script(item[2]) == "latin"),
        None,
    )
    if latin is not None and _ascii_letter_ratio(latin[2]) >= 0.35:
        return latin[1]
    non_latin = next(
        (
            item
            for item in scored
            if item[1] in COMMON_ENCODINGS
            and _dominant_script(item[2]) not in {None, "latin"}
        ),
        None,
    )
    if non_latin is not None:
        return non_latin[1]
    return scored[0][1]


def decode_text(raw: bytes, *, sample_bytes: int = SAMPLE_BYTES) -> str:
    """Decode arbitrary bytes to ``str`` with the best available encoding.

    A UTF BOM wins immediately.  Otherwise strict UTF-8 is tried before any
    detector, so the common case costs one decode and never depends on an
    optional package.  The last resort uses replacement characters: partial
    text is more useful for search than an empty document.
    """
    if not raw:
        return ""
    for bom, encoding in (
        (codecs.BOM_UTF32_LE, "utf-32-le"),
        (codecs.BOM_UTF32_BE, "utf-32-be"),
        (codecs.BOM_UTF8, "utf-8-sig"),
        (codecs.BOM_UTF16_LE, "utf-16-le"),
        (codecs.BOM_UTF16_BE, "utf-16-be"),
    ):
        if raw.startswith(bom):
            decoded = _strict_decode(raw[len(bom):], encoding)
            if decoded is not None:
                return decoded

    decoded = _strict_decode(raw, "utf-8")
    if decoded is not None:
        return decoded

    chosen = _choose_encoding(raw[:sample_bytes])
    if chosen is not None:
        decoded = _strict_decode(raw, chosen)
        if decoded is not None:
            return decoded
        decoded = _strict_decode(raw[:sample_bytes], chosen)
        if decoded is not None:
            # The sample decoded but the remainder did not; keep what works.
            return decoded
    return raw.decode("utf-8", "replace")
