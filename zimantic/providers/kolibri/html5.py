"""Bounded HTML5 zip parsing and streaming for the Kolibri provider.

Python's ``zipfile`` allocates the complete member list before callers can
enforce a member-count or central-directory limit.  This module validates the
archive's end record and central directory itself, rejects unsafe/ambiguous
members, and streams only the requested file within explicit byte and time
budgets.
"""

from __future__ import annotations

import struct
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from ...content.store import (
    ArticleNotFoundError,
    ArticleTooLargeError,
    ArticleUnavailableError,
)
from ..files.common import FilesConfinedError, open_confined
from .schema import extension_mimetype, normalize_member_path

_EOCD = struct.Struct("<4sHHHHLLH")
_CENTRAL = struct.Struct("<4s6H3L5H2L")
_LOCAL = struct.Struct("<4s5H3L2H")
_EOCD_SIGNATURE = b"PK\x05\x06"
_CENTRAL_SIGNATURE = b"PK\x01\x02"
_LOCAL_SIGNATURE = b"PK\x03\x04"
_MAX_COMMENT = 65_535


class Html5ZipError(ArticleUnavailableError):
    """A Kolibri HTML5 archive cannot be served safely."""


class Html5LimitError(ArticleTooLargeError):
    """A Kolibri HTML5 archive exceeds a configured serving limit."""


@dataclass(frozen=True)
class ZipMember:
    name: str
    is_dir: bool
    compression: int
    flags: int
    crc32: int
    compressed_size: int
    file_size: int
    header_offset: int
    data_offset: int


class Html5Archive:
    """One validated ``html5_zip`` bundle rooted at a storage file."""

    def __init__(
        self,
        root: Path | str,
        relative_path: str,
        *,
        max_entries: int = 10_000,
        max_directory_bytes: int = 8_388_608,
        max_member_bytes: int = 268_435_456,
        max_total_bytes: int = 1_073_741_824,
        timeout_seconds: float = 10,
    ) -> None:
        self.root = Path(root)
        self.relative_path = str(relative_path)
        self.max_entries = max(1, int(max_entries))
        self.max_directory_bytes = max(1, int(max_directory_bytes))
        self.max_member_bytes = max(1, int(max_member_bytes))
        self.max_total_bytes = max(1, int(max_total_bytes))
        self.timeout_seconds = max(0.001, float(timeout_seconds))
        self.members: dict[str, ZipMember] = {}
        self._file_size = 0
        self._cd_offset = 0
        self._cd_size = 0
        self._validate()

    # -- validation --------------------------------------------------------

    def _open(self) -> tuple[BinaryIO, int]:
        try:
            opened = open_confined(self.root, self.relative_path)
        except FilesConfinedError as error:
            raise ArticleNotFoundError("HTML5 archive is unavailable") from error
        try:
            size = int(opened.stat.st_size)
        except OSError as error:
            close = getattr(opened.handle, "close", None)
            if callable(close):
                close()
            raise ArticleNotFoundError("HTML5 archive is unavailable") from error
        return opened.handle, size

    def _validate(self) -> None:
        handle, size = self._open()
        try:
            self._file_size = size
            if size < _EOCD.size:
                raise Html5ZipError("invalid HTML5 zip archive")
            tail_size = min(size, _EOCD.size + _MAX_COMMENT)
            handle.seek(size - tail_size)
            tail = handle.read(tail_size)
            index = tail.rfind(_EOCD_SIGNATURE)
            if index < 0 or index + _EOCD.size > len(tail):
                raise Html5ZipError("invalid HTML5 zip end record")
            (
                _signature,
                disk_number,
                cd_disk,
                entries_disk,
                entries_total,
                cd_size,
                cd_offset,
                comment_length,
            ) = _EOCD.unpack_from(tail, index)
            if (
                disk_number != 0
                or cd_disk != 0
                or entries_disk != entries_total
                or entries_total == 0xFFFF
                or cd_size == 0xFFFFFFFF
                or cd_offset == 0xFFFFFFFF
            ):
                raise Html5ZipError(
                    "unsupported multi-disk or zip64 HTML5 archive"
                )
            if entries_total > self.max_entries:
                raise Html5LimitError(
                    f"HTML5 archive has more than {self.max_entries} members"
                )
            if cd_size > self.max_directory_bytes:
                raise Html5LimitError(
                    "HTML5 archive central directory exceeds its byte limit"
                )
            if cd_offset + cd_size > size:
                raise Html5ZipError("HTML5 archive directory is out of bounds")
            if index + _EOCD.size + comment_length != len(tail):
                raise Html5ZipError("invalid HTML5 zip comment length")
            self._cd_offset = int(cd_offset)
            self._cd_size = int(cd_size)
            handle.seek(cd_offset)
            directory = handle.read(cd_size)
            if len(directory) != cd_size:
                raise Html5ZipError("truncated HTML5 zip directory")

            seen: set[str] = set()
            total = 0
            position = 0
            raw_entries: list[tuple[str, int, int, int, int, int, int]] = []
            for _ in range(entries_total):
                if position + _CENTRAL.size > len(directory):
                    raise Html5ZipError("truncated HTML5 zip directory entry")
                values = _CENTRAL.unpack_from(directory, position)
                position += _CENTRAL.size
                (
                    signature,
                    _version_made,
                    _version_needed,
                    flags,
                    compression,
                    _mod_time,
                    _mod_date,
                    crc,
                    compressed_size,
                    file_size,
                    name_length,
                    extra_length,
                    comment_len,
                    disk_start,
                    _internal_attr,
                    external_attr,
                    header_offset,
                ) = values
                if signature != _CENTRAL_SIGNATURE:
                    raise Html5ZipError("invalid HTML5 zip directory entry")
                if disk_start != 0:
                    raise Html5ZipError("multi-disk HTML5 archive is unsupported")
                if name_length == 0 or position + name_length > len(directory):
                    raise Html5ZipError("invalid HTML5 zip member name")
                name_bytes = directory[position : position + name_length]
                position += name_length + extra_length + comment_len
                if position > len(directory):
                    raise Html5ZipError("truncated HTML5 zip directory entry")
                if flags & 0x1:
                    raise Html5ZipError("encrypted HTML5 zip members are unsupported")
                if compression not in (0, 8):
                    raise Html5ZipError(
                        "unsupported HTML5 zip compression method"
                    )
                try:
                    name = name_bytes.decode(
                        "utf-8" if flags & 0x800 else "cp437", "strict"
                    )
                    is_dir = name.endswith("/")
                    normalized = normalize_member_path(name)
                except (UnicodeError, ValueError) as error:
                    raise Html5ZipError(
                        "invalid or unsafe HTML5 zip member path"
                    ) from error
                key = normalized.rstrip("/")
                if not key:
                    raise Html5ZipError("invalid HTML5 zip member path")
                if key in seen:
                    raise Html5ZipError("duplicate HTML5 zip member name")
                seen.add(key)
                mode = (int(external_attr) >> 16) & 0o170000
                if mode not in (0, 0o040000, 0o100000):
                    raise Html5ZipError(
                        "links and special files are not allowed in HTML5 archives"
                    )
                if compressed_size > self.max_member_bytes:
                    raise Html5LimitError(
                        "HTML5 archive member exceeds its compressed byte limit"
                    )
                if file_size > self.max_member_bytes:
                    raise Html5LimitError(
                        "HTML5 archive member exceeds its uncompressed byte limit"
                    )
                total += int(file_size)
                if total > self.max_total_bytes:
                    raise Html5LimitError(
                        "HTML5 archive declared output exceeds its total byte limit"
                    )
                raw_entries.append(
                    (
                        normalized,
                        int(header_offset),
                        int(compression),
                        int(flags),
                        int(crc),
                        int(compressed_size),
                        int(file_size),
                    )
                )
            if position != len(directory):
                # Some writers append a digital-signature record.  It is not a
                # member and must not be interpreted as one.
                if directory[position : position + 4] != b"PK\x05\x05":
                    raise Html5ZipError("trailing data in HTML5 zip directory")

            intervals: list[tuple[int, int]] = []
            for (
                name,
                header_offset,
                compression,
                flags,
                crc,
                compressed_size,
                file_size,
            ) in raw_entries:
                is_dir = name.endswith("/")
                data_offset = self._local_data_offset(
                    handle,
                    header_offset,
                    name,
                    compression,
                    compressed_size,
                    file_size,
                )
                if not is_dir or compressed_size:
                    end = data_offset + compressed_size
                    if end > self._cd_offset:
                        raise Html5ZipError(
                            "HTML5 archive member overlaps the directory"
                        )
                    intervals.append((data_offset, end))
                self.members[name.rstrip("/")] = ZipMember(
                    name=name,
                    is_dir=is_dir,
                    compression=compression,
                    flags=flags,
                    crc32=crc,
                    compressed_size=compressed_size,
                    file_size=file_size,
                    header_offset=header_offset,
                    data_offset=data_offset,
                )
            intervals.sort()
            for previous, current in zip(intervals, intervals[1:]):
                if current[0] < previous[1]:
                    raise Html5ZipError("overlapping HTML5 zip members")
            if "index.html" not in self.members or self.members["index.html"].is_dir:
                raise Html5ZipError("HTML5 archive has no root index.html")
        finally:
            close = getattr(handle, "close", None)
            if callable(close):
                close()

    @staticmethod
    def _local_data_offset(
        handle: BinaryIO,
        header_offset: int,
        expected_name: str,
        compression: int,
        compressed_size: int,
        file_size: int,
    ) -> int:
        if header_offset < 0:
            raise Html5ZipError("invalid HTML5 zip member offset")
        handle.seek(header_offset)
        header = handle.read(_LOCAL.size)
        if len(header) != _LOCAL.size:
            raise Html5ZipError("truncated HTML5 zip local header")
        (
            signature,
            _version,
            flags,
            method,
            _mod_time,
            _mod_date,
            _crc,
            local_compressed,
            local_size,
            name_length,
            extra_length,
        ) = _LOCAL.unpack(header)
        if signature != _LOCAL_SIGNATURE:
            raise Html5ZipError("invalid HTML5 zip local header")
        if method != compression:
            raise Html5ZipError("HTML5 zip compression metadata mismatch")
        if flags & 0x1:
            raise Html5ZipError("encrypted HTML5 zip members are unsupported")
        name_bytes = handle.read(name_length)
        try:
            local_name = name_bytes.decode(
                "utf-8" if flags & 0x800 else "cp437", "strict"
            )
        except UnicodeError as error:
            raise Html5ZipError("invalid HTML5 zip member name") from error
        if local_name != expected_name:
            raise Html5ZipError("HTML5 zip local/central names differ")
        if (local_compressed, local_size) not in {
            (compressed_size, file_size),
            (0, 0),
        }:
            if not (flags & 0x8):
                raise Html5ZipError("HTML5 zip member size mismatch")
        return header_offset + _LOCAL.size + name_length + extra_length

    # -- metadata ----------------------------------------------------------

    @property
    def index_path(self) -> str:
        return "index.html"

    def member(self, name: str) -> ZipMember:
        try:
            normalized = normalize_member_path(name).rstrip("/")
        except ValueError as error:
            raise ArticleNotFoundError("HTML5 asset not found") from error
        member = self.members.get(normalized)
        if member is None or member.is_dir:
            raise ArticleNotFoundError("HTML5 asset not found")
        return member

    def resolve(self, name: str = "") -> str:
        candidate = str(name or "").strip("/")
        if not candidate:
            candidate = self.index_path
        self.member(candidate)
        return candidate

    def mimetype(self, name: str) -> str:
        return extension_mimetype(Path(name).suffix) or "application/octet-stream"

    # -- reads -------------------------------------------------------------

    def read(
        self,
        name: str,
        *,
        head_only: bool,
        max_bytes: int,
        deadline: float | None = None,
    ) -> tuple[bytes, int, bool, str]:
        member = self.member(name)
        limit = max(0, int(max_bytes))
        if limit <= 0:
            raise Html5LimitError("HTML5 asset exceeds the serving limit")
        if not head_only and member.file_size > min(limit, self.max_member_bytes):
            raise Html5LimitError("HTML5 asset exceeds the serving limit")
        target = min(
            int(member.file_size) if head_only else int(member.file_size),
            limit if head_only else limit,
        )
        if head_only:
            target = min(target, 256 << 10)
        if member.compression == 0:
            content = self._read_stored(member, 0, target, deadline)
            if (
                not head_only
                and len(content) == member.file_size
                and (zlib.crc32(content) & 0xFFFFFFFF) != member.crc32
            ):
                raise Html5ZipError("HTML5 zip checksum mismatch")
        else:
            content = self._read_deflated(
                member, max_bytes=target, deadline=deadline, head_only=head_only
            )
        total = int(member.file_size)
        return content, total, len(content) < total, self.mimetype(name)

    def read_range(
        self,
        name: str,
        *,
        start: int,
        end: int,
        max_bytes: int,
        deadline: float | None = None,
    ) -> tuple[bytes, int, int, int, str]:
        member = self.member(name)
        start = int(start)
        end = int(end)
        if start < 0 or end < start:
            raise ValueError("invalid content range")
        total = int(member.file_size)
        if start >= total:
            raise ValueError("content range starts past the representation")
        end = min(end, total - 1)
        limit = max(0, int(max_bytes))
        if limit <= 0:
            raise Html5LimitError("HTML5 range exceeds the serving limit")
        if end - start + 1 > limit:
            end = start + limit - 1
        if member.compression == 0:
            content = self._read_stored(member, start, end - start + 1, deadline)
        else:
            # A compressed member is not seekable for the phase-03 resource
            # policy.  Decompress only when the complete member fits the same
            # response cap; never inflate an oversized prefix for a small range.
            if total > min(limit, self.max_member_bytes):
                raise Html5LimitError(
                    "compressed HTML5 asset exceeds the serving limit"
                )
            full = self._read_deflated(
                member, max_bytes=total, deadline=deadline, head_only=False
            )
            content = full[start : end + 1]
        actual_end = start + len(content) - 1
        return content, start, actual_end, total, self.mimetype(name)

    def _read_stored(
        self,
        member: ZipMember,
        offset: int,
        length: int,
        deadline: float | None,
    ) -> bytes:
        if offset < 0 or length < 0 or offset + length > member.file_size:
            raise ArticleNotFoundError("HTML5 asset not found")
        if deadline is not None and time.monotonic() >= deadline:
            raise Html5LimitError("HTML5 archive read timed out")
        handle, _size = self._open()
        try:
            handle.seek(member.data_offset + offset)
            data = handle.read(length)
            if deadline is not None and time.monotonic() >= deadline:
                raise Html5LimitError("HTML5 archive read timed out")
        except OSError as error:
            raise ArticleUnavailableError(
                "HTML5 asset is unavailable"
            ) from error
        finally:
            close = getattr(handle, "close", None)
            if callable(close):
                close()
        if len(data) != length:
            raise Html5ZipError("truncated HTML5 zip member")
        return data

    def _read_deflated(
        self,
        member: ZipMember,
        *,
        max_bytes: int,
        deadline: float | None,
        head_only: bool,
    ) -> bytes:
        local_deadline = deadline
        if local_deadline is None:
            local_deadline = time.monotonic() + self.timeout_seconds
        limit = max(0, int(max_bytes))
        if limit <= 0:
            raise Html5LimitError("HTML5 asset exceeds the serving limit")
        handle, _size = self._open()
        try:
            handle.seek(member.data_offset)
            decompressor = zlib.decompressobj(-15)
            output = bytearray()
            remaining = min(int(member.compressed_size), self.max_member_bytes)
            produced_total = 0
            while remaining > 0:
                if time.monotonic() >= local_deadline:
                    raise Html5LimitError("HTML5 archive read timed out")
                chunk = handle.read(min(64 << 10, remaining))
                if not chunk:
                    raise Html5ZipError("truncated HTML5 zip member")
                remaining -= len(chunk)
                pending = chunk
                while pending:
                    allowance = limit - len(output) + 1
                    if allowance <= 0:
                        raise Html5LimitError(
                            "HTML5 asset exceeds the serving limit"
                        )
                    piece = decompressor.decompress(pending, allowance)
                    pending = decompressor.unconsumed_tail
                    if piece:
                        output.extend(piece)
                        produced_total += len(piece)
                        if produced_total > member.file_size:
                            raise Html5ZipError(
                                "HTML5 zip declared size mismatch"
                            )
                        if produced_total > self.max_member_bytes:
                            raise Html5LimitError(
                                "HTML5 asset exceeds its uncompressed byte limit"
                            )
                if head_only and len(output) >= limit:
                    break
            if not head_only or len(output) < limit:
                piece = decompressor.flush()
                if piece:
                    output.extend(piece)
                    produced_total += len(piece)
            if not head_only:
                if produced_total != member.file_size:
                    raise Html5ZipError("HTML5 zip actual size mismatch")
                if zlib.crc32(bytes(output)) & 0xFFFFFFFF != member.crc32:
                    raise Html5ZipError("HTML5 zip checksum mismatch")
            return bytes(output[:limit])
        except (Html5ZipError, Html5LimitError):
            raise
        except OSError as error:
            raise ArticleUnavailableError(
                "HTML5 asset is unavailable"
            ) from error
        except zlib.error as error:
            raise Html5ZipError("invalid compressed HTML5 member") from error
        finally:
            close = getattr(handle, "close", None)
            if callable(close):
                close()


__all__ = [
    "Html5Archive",
    "Html5LimitError",
    "Html5ZipError",
    "ZipMember",
]
