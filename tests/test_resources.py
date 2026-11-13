"""Process resource-limit helpers."""

from __future__ import annotations

import resource

from zimantic import resources


def test_raise_nofile_limit_uses_the_hard_limit(monkeypatch):
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(resource, "getrlimit", lambda _which: (256, 4096))
    monkeypatch.setattr(
        resource, "setrlimit", lambda _which, limits: calls.append(limits)
    )

    assert resources.raise_nofile_limit() == 4096
    assert calls == [(4096, 4096)]


def test_raise_nofile_limit_leaves_an_already_high_soft_limit(monkeypatch):
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(resource, "getrlimit", lambda _which: (8192, 4096))
    monkeypatch.setattr(
        resource, "setrlimit", lambda _which, limits: calls.append(limits)
    )

    assert resources.raise_nofile_limit() == 8192
    assert calls == []


def test_raise_nofile_limit_caps_an_infinite_hard_limit(monkeypatch):
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        resource, "getrlimit", lambda _which: (1024, resource.RLIM_INFINITY)
    )
    monkeypatch.setattr(
        resource, "setrlimit", lambda _which, limits: calls.append(limits)
    )

    assert resources.raise_nofile_limit() == resources._UNBOUNDED_TARGET
    assert calls == [(resources._UNBOUNDED_TARGET, resource.RLIM_INFINITY)]
