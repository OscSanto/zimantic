"""Process resource limits shared by command entry points."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# ``RLIM_INFINITY`` is not portable across every platform and some systems
# report an impractically large hard limit.  Cap only that unbounded case.
_UNBOUNDED_TARGET = 1_048_576


def raise_nofile_limit() -> int | None:
    """Raise the soft open-file limit to the hard limit when permitted.

    Read-only discovery can touch one descriptor per source, and systemd's
    default soft limit for services is often 1024.  Raising the soft limit up
    to the existing hard limit needs no privilege and avoids EMFILE on large
    libraries.  Returns the effective soft limit, or ``None`` when the
    platform does not expose ``RLIMIT_NOFILE``.
    """
    try:
        import resource
    except ImportError:  # pragma: no cover - Windows
        return None

    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    except (OSError, ValueError):
        return None

    if hard == resource.RLIM_INFINITY:
        target = _UNBOUNDED_TARGET
    else:
        target = hard
    if soft != resource.RLIM_INFINITY and soft >= target:
        return soft
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    except (OSError, ValueError) as error:
        logger.debug(
            "zimantic: could not raise the open-file soft limit to %s (%s)",
            target,
            error,
        )
        return soft
    logger.debug(
        "zimantic: open-file soft limit raised from %s to %s", soft, target
    )
    return target
