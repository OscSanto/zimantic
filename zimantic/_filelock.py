"""Cross-platform byte-range locks with shared-lock support on Windows."""

from __future__ import annotations

import errno
import os

if os.name == "nt":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class _Overlapped(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_void_p),
            ("InternalHigh", ctypes.c_void_p),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _lock_file_ex = _kernel32.LockFileEx
    _lock_file_ex.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_Overlapped),
    ]
    _lock_file_ex.restype = wintypes.BOOL
    _unlock_file_ex = _kernel32.UnlockFileEx
    _unlock_file_ex.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_Overlapped),
    ]
    _unlock_file_ex.restype = wintypes.BOOL


def lock_file(
    fd: int, *, shared: bool = False, blocking: bool = False
) -> bool:
    """Lock the first byte of an open file descriptor."""
    if os.name == "nt":
        flags = 0 if shared else 0x2  # LOCKFILE_EXCLUSIVE_LOCK
        if not blocking:
            flags |= 0x1  # LOCKFILE_FAIL_IMMEDIATELY
        overlapped = _Overlapped()
        handle = msvcrt.get_osfhandle(fd)
        if _lock_file_ex(
            handle, flags, 0, 1, 0, ctypes.byref(overlapped)
        ):
            return True
        error = ctypes.get_last_error()
        if error == 33:  # ERROR_LOCK_VIOLATION
            return False
        raise OSError(error, ctypes.FormatError(error))

    import fcntl

    mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
    if not blocking:
        mode |= fcntl.LOCK_NB
    try:
        fcntl.flock(fd, mode)
        return True
    except OSError as error:
        if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
            return False
        raise


def unlock_file(fd: int) -> None:
    """Unlock the first byte of an open file descriptor."""
    if os.name == "nt":
        overlapped = _Overlapped()
        handle = msvcrt.get_osfhandle(fd)
        if not _unlock_file_ex(handle, 0, 1, 0, ctypes.byref(overlapped)):
            error = ctypes.get_last_error()
            raise OSError(error, ctypes.FormatError(error))
        return

    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)
