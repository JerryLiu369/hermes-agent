"""Failed plugin loads must not permanently disable a platform.

Regression for #126356: a transient plugin-load overrun (the 10s deadline blown by startup
I/O contention after an unclean reboot) left the platform with no adapter, no runtime status
and no retry — dead until a manual restart. The fix queues the missing adapter for the
reconnect watcher (``retrying`` status, so the normal ``needs_attention`` escalation and the
status indicator see a never-loaded platform exactly like a loaded-then-disconnected one),
revives failed deferred loads on each reconnect pass, and raises the load deadline while a
startup progress lease is held.
"""

import asyncio
import time
from unittest.mock import MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platform_registry import PlatformEntry, PlatformRegistry
from gateway.run import GatewayRunner


def _telegram_config(tmp_path) -> GatewayConfig:
    return GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="test")},
        sessions_dir=tmp_path / "sessions",
    )


@pytest.mark.asyncio
async def test_start_prefilter_queues_missing_adapter_with_retrying_status(
    tmp_path, monkeypatch
):
    """A missing adapter at startup is queued for retry with visible status, not dropped."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    runner = GatewayRunner(_telegram_config(tmp_path))
    monkeypatch.setattr(runner, "_create_adapter", lambda platform, cfg: None)

    aborted, enabled, _skipped, pending = await runner._start_prefilter_platforms()

    assert aborted is False
    assert enabled == 1
    assert pending == []
    # Queued for the reconnect watcher with the missing-adapter marker ...
    assert Platform.TELEGRAM in runner._failed_platforms
    info = runner._failed_platforms[Platform.TELEGRAM]
    assert info.get("missing_adapter") is True
    assert info.get("credential_claim") is None
    # ... and stamped retrying so status surfaces carry it like a disconnect.
    from gateway.status import flush_runtime_status_async, read_runtime_status

    assert await flush_runtime_status_async(timeout=5.0)
    state = read_runtime_status()
    entry = state["platforms"]["telegram"]
    assert entry["state"] == "retrying"
    assert entry["error_code"] == "adapter_missing"


@pytest.mark.asyncio
async def test_reconnect_keeps_missing_adapter_queued_with_backoff(tmp_path, monkeypatch):
    """The watcher must not drop a still-missing adapter; it backs off and stays queued."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner = object.__new__(GatewayRunner)
    runner.config = _telegram_config(tmp_path)
    runner._running = True
    runner._shutdown_event = asyncio.Event()
    runner._failed_platforms = {}
    runner.adapters = {}
    runner.delivery_router = MagicMock()
    status_writes: list = []

    def _capture_status(name, **fields):
        status_writes.append((name, fields))

    runner._update_platform_runtime_status = _capture_status  # type: ignore[method-assign]
    cfg = runner.config.platforms[Platform.TELEGRAM]
    runner._failed_platforms[Platform.TELEGRAM] = runner._reconnect_queue_entry(
        Platform.TELEGRAM, None, cfg, attempts=1, delay=0,
    )
    info = runner._failed_platforms[Platform.TELEGRAM]
    info["next_retry"] = time.monotonic() - 1  # due now
    monkeypatch.setattr(runner, "_create_adapter", lambda platform, cfg: None)

    await runner._reconnect_failed_platform(Platform.TELEGRAM, time.monotonic())

    # Still queued (the old code deleted it here), attempt bumped, retrying restamped.
    assert Platform.TELEGRAM in runner._failed_platforms
    assert runner._failed_platforms[Platform.TELEGRAM]["attempts"] == 2
    assert any(
        name == "telegram" and fields.get("platform_state") == "retrying"
        for name, fields in status_writes
    )


def test_failed_deferred_load_revives_on_retry(tmp_path, monkeypatch):
    """A failed deferred load parks its loader; retry re-queues it for one more attempt."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    registry = PlatformRegistry()
    calls: list = []

    def _loader():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("simulated load overrun")
        registry.register(
            PlatformEntry(
                name="revive-target", label="Revive", adapter_factory=lambda cfg: object(),
                check_fn=lambda: True, source="plugin",
            ),
            scope=PlatformRegistry.current_scope_key(),
        )

    registry.register_deferred(
        "revive-target", _loader, scope=PlatformRegistry.current_scope_key()
    )
    assert registry.get("revive-target") is None  # first attempt fails
    assert registry.has_failed_load("revive-target") is True

    assert registry.retry_failed_load("revive-target") is True

    assert registry.get("revive-target") is not None  # second attempt heals
    assert registry.has_failed_load("revive-target") is False
    assert registry.retry_failed_load("revive-target") is False  # nothing left to revive


def test_plugin_deadline_extended_while_progress_lease_held(tmp_path, monkeypatch):
    """A live startup progress lease raises the plugin deadline; explicit 0 still disables."""
    import hermes_startup_watchdog as sw
    from hermes_cli import plugins_loader

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    sw._reset_for_tests()
    try:
        assert plugins_loader._resolve_plugin_load_timeout() == pytest.approx(10.0)

        handle = sw.arm_startup_watchdog(timeout_s=300)
        assert handle is not None
        sw.report_startup_progress(600, phase="state_db_unclean_integrity_check")
        active, phase, _remaining = sw.startup_watchdog_lease_active()
        assert active is True
        assert phase == "state_db_unclean_integrity_check"

        assert plugins_loader._resolve_plugin_load_timeout() >= 60.0

        (tmp_path / "config.yaml").write_text("plugins:\n  load_timeout_seconds: 0\n")
        assert plugins_loader._resolve_plugin_load_timeout() == 0
    finally:
        sw._reset_for_tests()
