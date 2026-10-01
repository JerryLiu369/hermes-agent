"""Regression tests for #130575: recursive deadlock in plugin load deadline.

``run_with_load_deadline`` spawns a worker thread and joins it. A plugin's
``register()`` can trigger another plugin load on the worker thread itself
(i18n -> YAML -> platform registry -> deferred loader). The nested call must
run inline instead of spawning another worker that joins against the outer
load (or against the discovery lock the loading thread holds while waiting).
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

import hermes_cli.plugins_loader as loader
from hermes_cli.plugins_loader import (
    PluginLoadTimeout,
    _IN_PLUGIN_LOAD,
    _IN_PLUGIN_LOAD_CTX,
    _in_plugin_load,
    in_plugin_load_worker,
    run_with_load_deadline,
)


def _ctx():
    abandoned = {"flag": False}

    class _Ctx:
        def _abandon_load(self):
            abandoned["flag"] = True

    ctx = _Ctx()
    return ctx, abandoned


@pytest.fixture
def isolated_loaders(monkeypatch):
    """Keep abandoned-loader bookkeeping local to each test."""
    monkeypatch.setattr(loader, "_ABANDONED_LOADERS", [])
    yield
    # Release any abandoned workers the test left behind.
    for t in list(loader._ABANDONED_LOADERS):
        if t.is_alive():
            # Daemon threads die with the process; just drop the bookkeeping.
            pass
    loader._ABANDONED_LOADERS.clear()


@pytest.fixture(autouse=True)
def clean_guard():
    """Each test starts and ends outside a plugin load."""
    _IN_PLUGIN_LOAD.active = False
    token = _IN_PLUGIN_LOAD_CTX.set(False)
    try:
        yield
    finally:
        _IN_PLUGIN_LOAD.active = False
        _IN_PLUGIN_LOAD_CTX.reset(token)


def test_nested_deadline_runs_inline_on_same_thread(monkeypatch, isolated_loaders):
    """A nested call inside a plugin load runs on the worker thread, not a new one."""
    monkeypatch.setattr(loader, "_resolve_plugin_load_timeout", lambda: 5.0)
    ctx, _ = _ctx()
    main_thread = threading.current_thread()
    outer_thread = []
    inner_thread = []

    def inner():
        inner_thread.append(threading.current_thread())
        assert _in_plugin_load()
        assert in_plugin_load_worker()
        return "inner-result"

    def outer():
        outer_thread.append(threading.current_thread())
        assert _in_plugin_load()
        # Nested call must not spawn/join another worker: it runs inline.
        return run_with_load_deadline("inner", ctx, inner)

    result = run_with_load_deadline("outer", ctx, outer)
    assert result == "inner-result"
    assert len(outer_thread) == 1 and len(inner_thread) == 1
    # Outer ran on a deadline worker (not the caller), inner ran inline on it.
    assert outer_thread[0] is not main_thread
    assert inner_thread[0] is outer_thread[0]


def test_deeply_nested_loads_all_run_inline(monkeypatch, isolated_loaders):
    """Three levels of nesting complete without deadlock."""
    monkeypatch.setattr(loader, "_resolve_plugin_load_timeout", lambda: 5.0)
    ctx, _ = _ctx()
    seen = []

    def level3():
        seen.append(threading.current_thread())
        return 3

    def level2():
        seen.append(threading.current_thread())
        return run_with_load_deadline("l3", ctx, level3) + 1

    def level1():
        seen.append(threading.current_thread())
        return run_with_load_deadline("l2", ctx, level2) + 1

    assert run_with_load_deadline("l1", ctx, level1) == 5
    assert len(seen) == 3
    assert seen[0] is seen[1] is seen[2]


def test_nested_exception_propagates_and_guard_survives(monkeypatch, isolated_loaders):
    """An inner failure propagates; the outer worker guard stays set until it finishes."""
    monkeypatch.setattr(loader, "_resolve_plugin_load_timeout", lambda: 5.0)
    ctx, _ = _ctx()

    def boom():
        raise ValueError("nested boom")

    def outer():
        with pytest.raises(ValueError, match="nested boom"):
            run_with_load_deadline("inner", ctx, boom)
        # Still inside the outer load after the nested failure.
        assert _in_plugin_load()
        return "recovered"

    assert run_with_load_deadline("outer", ctx, outer) == "recovered"
    assert not _in_plugin_load()
    assert not in_plugin_load_worker()


def test_guard_visible_inside_worker_only(monkeypatch, isolated_loaders):
    """The guard is True on the worker thread, False on the caller before/after."""
    monkeypatch.setattr(loader, "_resolve_plugin_load_timeout", lambda: 5.0)
    ctx, _ = _ctx()
    assert not _in_plugin_load()
    inside = []

    def fn():
        inside.append(_in_plugin_load())
        inside.append(in_plugin_load_worker())
        return "ok"

    assert run_with_load_deadline("p", ctx, fn) == "ok"
    assert inside == [True, True]
    assert not _in_plugin_load()
    assert not in_plugin_load_worker()
    assert _IN_PLUGIN_LOAD_CTX.get() is False
    assert getattr(_IN_PLUGIN_LOAD, "active", False) is False


def test_guard_cleans_up_after_success(monkeypatch, isolated_loaders):
    monkeypatch.setattr(loader, "_resolve_plugin_load_timeout", lambda: 5.0)
    ctx, _ = _ctx()
    assert run_with_load_deadline("p", ctx, lambda: 42) == 42
    assert not _in_plugin_load()
    assert _IN_PLUGIN_LOAD_CTX.get() is False
    assert getattr(_IN_PLUGIN_LOAD, "active", False) is False


def test_guard_cleans_up_after_failure(monkeypatch, isolated_loaders):
    """A raising plugin load must not leave the re-entrancy guard set."""
    monkeypatch.setattr(loader, "_resolve_plugin_load_timeout", lambda: 5.0)
    ctx, _ = _ctx()

    def fn():
        raise RuntimeError("register failed")

    with pytest.raises(RuntimeError, match="register failed"):
        run_with_load_deadline("p", ctx, fn)
    assert not _in_plugin_load()
    assert not in_plugin_load_worker()
    assert _IN_PLUGIN_LOAD_CTX.get() is False
    assert getattr(_IN_PLUGIN_LOAD, "active", False) is False
    # A later top-level load still gets a real deadline worker, not an inline run.
    main_thread = threading.current_thread()
    seen = []
    assert run_with_load_deadline("p2", ctx, lambda: seen.append(threading.current_thread())) is None
    assert seen and seen[0] is not main_thread


def test_timeout_still_fires_for_hanging_plugin(monkeypatch, isolated_loaders):
    """A register() that never returns is abandoned with PluginLoadTimeout."""
    monkeypatch.setattr(loader, "_resolve_plugin_load_timeout", lambda: 0.2)
    ctx, abandoned = _ctx()
    gate = threading.Event()

    def hang():
        gate.wait(10)
        return "never"

    start = time.monotonic()
    with pytest.raises(PluginLoadTimeout):
        run_with_load_deadline("slow", ctx, hang)
    elapsed = time.monotonic() - start
    assert elapsed < 5
    assert abandoned["flag"] is True
    assert not _in_plugin_load()
    gate.set()


def test_timeout_propagates_plugin_exception(monkeypatch, isolated_loaders):
    """A fast failure is re-raised on the loading thread (not converted to timeout)."""
    monkeypatch.setattr(loader, "_resolve_plugin_load_timeout", lambda: 5.0)
    ctx, abandoned = _ctx()

    def fn():
        raise KeyError("bad plugin")

    with pytest.raises(KeyError):
        run_with_load_deadline("p", ctx, fn)
    assert abandoned["flag"] is False


def test_premanually_guarded_caller_runs_inline(monkeypatch, isolated_loaders):
    """When the caller is already marked inside a load, no worker thread is used."""
    monkeypatch.setattr(loader, "_resolve_plugin_load_timeout", lambda: 5.0)
    ctx, _ = _ctx()
    _IN_PLUGIN_LOAD.active = True
    try:
        main_thread = threading.current_thread()
        seen = []
        result = run_with_load_deadline("nested", ctx, lambda: seen.append(threading.current_thread()) or "inline")
        assert result == "inline"
        assert seen == [main_thread]
    finally:
        _IN_PLUGIN_LOAD.active = False
    assert not _in_plugin_load()


def test_contextvar_guard_alone_runs_inline(monkeypatch, isolated_loaders):
    """The ContextVar half of the guard is sufficient (covers copy_context propagation)."""
    monkeypatch.setattr(loader, "_resolve_plugin_load_timeout", lambda: 5.0)
    ctx, _ = _ctx()
    token = _IN_PLUGIN_LOAD_CTX.set(True)
    try:
        assert _in_plugin_load()
        assert in_plugin_load_worker()
        main_thread = threading.current_thread()
        seen = []
        assert run_with_load_deadline("p", ctx, lambda: seen.append(threading.current_thread()) or 7) == 7
        assert seen == [main_thread]
    finally:
        _IN_PLUGIN_LOAD_CTX.reset(token)
    assert not _in_plugin_load()
