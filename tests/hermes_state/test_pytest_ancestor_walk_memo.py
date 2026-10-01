"""A failed pytest-ancestor walk must not stick (#126766).

``_has_pytest_ancestor`` fails open when psutil cannot walk the process tree.
Memoizing that ``False`` disarms the production state.db guard for the rest of
the process, even after a later walk would see pytest.
"""

from __future__ import annotations

from typing import Any

import hermes_state_guard as guard


class _Proc:
    def __init__(self, argv: list[str]):
        self._argv = list(argv)

    def cmdline(self) -> list[str]:
        return list(self._argv)


class _Psutil:
    def __init__(self, walks: list[Any]):
        self.walks = list(walks)

    def Process(self) -> _Psutil:
        return self

    def parents(self) -> list[_Proc]:
        item = self.walks.pop(0)
        if isinstance(item, BaseException):
            raise item
        return [_Proc(argv) for argv in item]


def test_transient_ancestor_walk_error_is_not_memoized(monkeypatch):
    """A PermissionError on the first walk must not cache ``False``."""
    fake = _Psutil([
        PermissionError("transient access denied"),
        [["/usr/bin/pytest", "tests/test_x.py"]],
    ])
    monkeypatch.setattr(guard, "psutil", fake)
    monkeypatch.setattr(guard, "_PYTEST_ANCESTOR", None)

    assert guard._has_pytest_ancestor() is False
    assert guard._PYTEST_ANCESTOR is None
    assert guard._has_pytest_ancestor() is True
    assert guard._PYTEST_ANCESTOR is True


def test_successful_negative_ancestor_walk_stays_memoized(monkeypatch):
    """A completed negative walk is still cached: process tree above us does not change."""
    fake = _Psutil([[["/usr/bin/python3", "server.py"]]])
    monkeypatch.setattr(guard, "psutil", fake)
    monkeypatch.setattr(guard, "_PYTEST_ANCESTOR", None)

    assert guard._has_pytest_ancestor() is False
    assert guard._PYTEST_ANCESTOR is False
    # Next call uses the cache and does not call parents() again
    assert guard._has_pytest_ancestor() is False
    assert fake.walks == []


def test_successful_positive_ancestor_walk_stays_memoized(monkeypatch):
    """A completed positive walk is cached and returns True without re-querying."""
    fake = _Psutil([[["/usr/bin/pytest", "-q"]]])
    monkeypatch.setattr(guard, "psutil", fake)
    monkeypatch.setattr(guard, "_PYTEST_ANCESTOR", None)

    assert guard._has_pytest_ancestor() is True
    assert guard._PYTEST_ANCESTOR is True
    assert guard._has_pytest_ancestor() is True
    assert fake.walks == []
