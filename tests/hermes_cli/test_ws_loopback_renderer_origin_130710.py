"""Regression for #130710: Desktop renderer loopback origin on remote binds.

Since d428df2a65 the packaged Desktop renderer is served from loopback HTTP
(``http://127.0.0.1:<port>``) instead of ``file://``. Its WebSocket upgrades
therefore carry ``Origin: http://127.0.0.1:<port>``. On a remote bind
(``hermes serve --host <tailscale-ip>``) ``_ws_host_origin_reason`` rejected
that origin as ``origin_mismatch`` because only non-web origins or the bound
host were accepted. The sidecar gate (``/api/ws``) then closed pre-accept with
a silent 4403, so the renderer reported ``WebSocket error before open`` while
the backend logged nothing.

Behaviour contract pinned here:
- a loopback http(s) renderer origin is trusted like ``file://`` on any bind;
- a non-loopback cross-site origin is still rejected (DNS-rebinding defence);
- the sidecar pre-accept refusal leaves an ``origin_mismatch`` warning line.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from hermes_cli import web_server
import hermes_cli.web_server_chat as web_server_chat
from hermes_cli.web_routers import chat_ws


def _fake_origin_ws(*, origin: str, host: str):
    class _QP:
        def get(self, k, default=""):
            return default

    return SimpleNamespace(
        query_params=_QP(),
        headers={"host": host, "origin": origin},
        client=SimpleNamespace(host="100.64.0.99"),
        url=SimpleNamespace(path="/api/ws"),
    )


@pytest.fixture
def remote_bind():
    """Explicit non-loopback bind (Tailscale IP), unauthenticated for the
    origin-guard unit level (the guard itself does not consult auth)."""
    prev_host = getattr(web_server.app.state, "bound_host", None)
    prev_port = getattr(web_server.app.state, "bound_port", None)
    prev_required = getattr(web_server.app.state, "auth_required", None)
    prev_trusted = getattr(web_server.app.state, "trusted_public_hosts", None)
    web_server.app.state.bound_host = "100.64.0.10"
    web_server.app.state.bound_port = 9119
    web_server.app.state.auth_required = False
    web_server.app.state.trusted_public_hosts = frozenset()
    yield web_server.app.state
    web_server.app.state.bound_host = prev_host
    web_server.app.state.bound_port = prev_port
    web_server.app.state.auth_required = prev_required
    if prev_trusted is None:
        if hasattr(web_server.app.state, "trusted_public_hosts"):
            delattr(web_server.app.state, "trusted_public_hosts")
    else:
        web_server.app.state.trusted_public_hosts = prev_trusted


@pytest.fixture
def gated_remote_bind():
    """OAuth-gated public bind: loopback renderer origins must pass here too."""
    prev_host = getattr(web_server.app.state, "bound_host", None)
    prev_port = getattr(web_server.app.state, "bound_port", None)
    prev_required = getattr(web_server.app.state, "auth_required", None)
    prev_trusted = getattr(web_server.app.state, "trusted_public_hosts", None)
    web_server.app.state.bound_host = "fly-app.fly.dev"
    web_server.app.state.bound_port = 443
    web_server.app.state.auth_required = True
    web_server.app.state.trusted_public_hosts = frozenset()
    yield web_server.app.state
    web_server.app.state.bound_host = prev_host
    web_server.app.state.bound_port = prev_port
    web_server.app.state.auth_required = prev_required
    if prev_trusted is None:
        if hasattr(web_server.app.state, "trusted_public_hosts"):
            delattr(web_server.app.state, "trusted_public_hosts")
    else:
        web_server.app.state.trusted_public_hosts = prev_trusted


class TestLoopbackRendererOrigin130710:
    @pytest.mark.parametrize(
        "origin",
        [
            "http://127.0.0.1:47891",
            "http://127.0.0.1:12345",
            "http://localhost:47891",
            "http://[::1]:47891",
            "https://127.0.0.1:47891",
        ],
    )
    def test_loopback_renderer_origin_accepted_on_remote_bind(self, remote_bind, origin):
        ws = _fake_origin_ws(origin=origin, host="100.64.0.10:9119")
        assert web_server_chat._ws_host_origin_reason(ws) is None
        assert web_server_chat._ws_host_origin_is_allowed(ws) is True

    def test_loopback_renderer_origin_accepted_on_gated_bind(self, gated_remote_bind):
        ws = _fake_origin_ws(origin="http://127.0.0.1:47891", host="fly-app.fly.dev")
        assert web_server_chat._ws_host_origin_reason(ws) is None

    @pytest.mark.parametrize(
        "origin",
        [
            "http://192.168.0.55:9119",
            "https://evil.test",
            "http://127.0.0.1.evil.test:47891",
            "http://localhost.evil.test",
        ],
    )
    def test_cross_site_origin_still_rejected_on_remote_bind(self, remote_bind, origin):
        ws = _fake_origin_ws(origin=origin, host="100.64.0.10:9119")
        reason = web_server_chat._ws_host_origin_reason(ws)
        assert reason is not None and reason.startswith("origin_mismatch")
        assert web_server_chat._ws_host_origin_is_allowed(ws) is False

    def test_host_mismatch_still_rejected_with_loopback_origin(self, remote_bind):
        ws = _fake_origin_ws(origin="http://127.0.0.1:47891", host="evil.example:9119")
        reason = web_server_chat._ws_host_origin_reason(ws)
        assert reason is not None and reason.startswith("host_mismatch")


class TestSidecarRefusalLogging130710:
    def test_sidecar_refusal_logs_origin_mismatch(self, remote_bind, caplog):
        """A pre-accept 4403 must leave a warning line (was silent before)."""
        closed = {}

        class _QP:
            def get(self, k, default=""):
                if k == "token":
                    return web_server._SESSION_TOKEN
                return default

        ws = SimpleNamespace(
            query_params=_QP(),
            headers={"host": "100.64.0.10:9119", "origin": "https://evil.test"},
            client=SimpleNamespace(host="100.64.0.99"),
            url=SimpleNamespace(path="/api/ws"),
        )

        async def _close(*, code, reason=None):
            closed["code"] = code
            closed["reason"] = reason

        ws.close = _close  # type: ignore[attr-defined]

        import asyncio

        with caplog.at_level(logging.WARNING, logger="hermes_cli.web_server"):
            ok = asyncio.run(chat_ws._close_unless_sidecar_allowed(ws))

        assert ok is False
        assert closed.get("code") == 4403
        assert any(
            "origin_mismatch" in (rec.getMessage() or "")
            for rec in caplog.records
            if rec.name == "hermes_cli.web_server"
        )
