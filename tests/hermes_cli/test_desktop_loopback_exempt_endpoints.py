"""Desktop-owned loopback backends honour the per-spawn session token (#126391).

On a Desktop-owned loopback backend the ticket-only gate refuses the per-spawn
``HERMES_DASHBOARD_SESSION_TOKEN``, so the Desktop's polled dashboard reads
(``GET /api/profiles``, ``GET /api/sessions`` + subpaths, ``GET /api/kanban``,
``GET /api/artifacts``) 401'd despite carrying it. The workaround — listing
those paths in the global ``PUBLIC_API_PATHS`` — is unsafe: that allowlist is
bind-agnostic and method-agnostic (it would also open ``POST /api/profiles``,
unauthenticated profile creation, on every bind) while the subpaths the view
needs stayed gated.

These tests pin the targeted exemption instead: ``GET``/``HEAD`` reads against
those families (``/``-delimited subpaths included) pass the gate on a loopback
bind of a Desktop-owned backend holding an operator-minted credential, WITH a
valid session token on the request. Missing/invalid tokens, state-changing
verbs, and non-loopback binds stay 401.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from hermes_cli import web_server
from hermes_cli.dashboard_auth.public_paths import is_desktop_loopback_exempt_path

_TOKEN = "desktop-per-spawn-token-126391"

# app.state attributes this module mutates. Saved values use a sentinel (not
# None) so teardown deletes attributes that were absent beforehand: leaving
# e.g. ``trusted_public_hosts=None`` behind breaks later tests
# (``host_only in None`` raises TypeError in the host-header check).
_STATE_ATTRS = (
    "auth_required",
    "bound_host",
    "bound_port",
    "trusted_public_hosts",
    "desktop_loopback_exempt",
)
_MISSING = object()


def _save_app_state():
    """Snapshot the mutated app.state attributes, marking absent ones."""
    state = web_server.app.state
    return {name: getattr(state, name, _MISSING) for name in _STATE_ATTRS}


def _restore_app_state(saved):
    """Restore a snapshot, deleting attributes that were absent before."""
    state = web_server.app.state
    for name, value in saved.items():
        if value is _MISSING:
            try:
                delattr(state, name)
            except (AttributeError, KeyError):
                # Already absent (e.g. a test deleted it mid-flight, or this
                # test never set it) — the desired end state either way.
                pass
        else:
            setattr(state, name, value)


@pytest.fixture
def desktop_loopback_gated(monkeypatch, _isolate_hermes_home):
    """Gated Desktop-owned loopback backend with a known spawn token.

    ``auth_required=True`` reproduces the ticket gate the startup exemption
    normally lifts, so the per-request exemption is what lets the reads
    through. State is restored on teardown.
    """
    monkeypatch.setenv("HERMES_DESKTOP", "1")
    monkeypatch.setenv("HERMES_DASHBOARD_SESSION_TOKEN", _TOKEN)
    monkeypatch.setattr(web_server, "_SESSION_TOKEN", _TOKEN)
    prev = _save_app_state()
    web_server.app.state.auth_required = True
    web_server.app.state.bound_host = "127.0.0.1"
    web_server.app.state.bound_port = 9119
    web_server.app.state.trusted_public_hosts = frozenset()
    web_server.app.state.desktop_loopback_exempt = True
    yield TestClient(web_server.app, base_url="http://127.0.0.1")
    _restore_app_state(prev)


@pytest.mark.parametrize("method,path,expected", [
    ("GET", "/api/profiles", True),
    ("GET", "/api/profiles/sessions", True),
    ("GET", "/api/profiles/sessions/sidebar", True),
    ("GET", "/api/profiles/projects/tree", True),
    ("GET", "/api/sessions", True),
    ("GET", "/api/sessions/abc123/messages", True),
    ("GET", "/api/kanban", True),
    ("GET", "/api/kanban/board", True),
    ("GET", "/api/artifacts", True),
    ("GET", "/api/artifacts/xyz", True),
    ("HEAD", "/api/sessions", True),
    ("get", "/api/sessions", True),
    # State-changing verbs never qualify — POST /api/profiles creates a
    # profile (and can clone config/SOUL) with no _require_token of its own.
    ("POST", "/api/profiles", False),
    ("POST", "/api/sessions/prune", False),
    ("DELETE", "/api/sessions/empty", False),
    ("PATCH", "/api/sessions/abc", False),
    # Prefix-boundary: exact-or-slash-delimited only.
    ("GET", "/api/sessions-evil", False),
    ("GET", "/api/profiless", False),
    ("GET", "/api/kanbanx/y", False),
    # Outside the Desktop-polled families (public or still gated as before).
    ("GET", "/api/status", False),
    ("GET", "/api/config", False),
    ("GET", "/api/auth/me", False),
    ("GET", "/other", False),
])
def test_exempt_path_matcher(method, path, expected):
    assert is_desktop_loopback_exempt_path(path, method) is expected


@pytest.mark.parametrize("path", [
    "/api/profiles",
    "/api/sessions?limit=1",
    "/api/profiles/sessions?limit=1",
])
def test_gated_desktop_loopback_reads_succeed_with_spawn_token(
    desktop_loopback_gated, path,
):
    """Positive case: valid per-spawn token + loopback serves the list reads."""
    r = desktop_loopback_gated.get(
        path, headers={web_server._SESSION_HEADER_NAME: _TOKEN}
    )
    assert r.status_code == 200, f"{path}: {r.status_code} {r.text}"


@pytest.mark.parametrize("path", [
    "/api/kanban",
    "/api/artifacts",
    "/api/sessions/no-such-session-126391/messages",
])
def test_gated_desktop_loopback_reads_clear_auth_with_spawn_token(
    desktop_loopback_gated, path,
):
    """These families have no first-party route (kanban/artifacts) or an
    unknown id: auth must pass (never 401) and routing answers 404."""
    r = desktop_loopback_gated.get(
        path, headers={web_server._SESSION_HEADER_NAME: _TOKEN}
    )
    assert r.status_code == 404, f"{path}: {r.status_code} {r.text}"


@pytest.mark.parametrize("path", [
    "/api/profiles",
    "/api/sessions?limit=1",
    "/api/profiles/sessions?limit=1",
    "/api/kanban",
    "/api/artifacts",
    "/api/sessions/abc/messages",
])
def test_gated_desktop_loopback_reads_reject_missing_token(
    desktop_loopback_gated, path,
):
    assert desktop_loopback_gated.get(path).status_code == 401


@pytest.mark.parametrize("path", [
    "/api/profiles",
    "/api/sessions?limit=1",
    "/api/kanban",
    "/api/artifacts",
])
def test_gated_desktop_loopback_reads_reject_invalid_token(
    desktop_loopback_gated, path,
):
    r = desktop_loopback_gated.get(
        path, headers={web_server._SESSION_HEADER_NAME: "wrong-token"}
    )
    assert r.status_code == 401


def test_gated_desktop_loopback_post_stays_gated_with_spawn_token(
    desktop_loopback_gated,
):
    """``POST /api/profiles`` creates a profile — the exemption is GET/HEAD
    only, so a valid token on a write verb still 401s at the gate."""
    r = desktop_loopback_gated.post(
        "/api/profiles",
        headers={web_server._SESSION_HEADER_NAME: _TOKEN},
        json={"name": "should-not-exist-126391"},
    )
    assert r.status_code == 401


def test_gated_non_loopback_bind_rejects_spawn_token(
    desktop_loopback_gated,
):
    """Bind-scoping: the same token on a non-loopback bind stays 401, even
    with a stale startup-exemption flag."""
    web_server.app.state.bound_host = "0.0.0.0"
    r = desktop_loopback_gated.get(
        "/api/profiles", headers={web_server._SESSION_HEADER_NAME: _TOKEN}
    )
    assert r.status_code == 401


def test_gated_non_desktop_loopback_rejects_spawn_token(
    desktop_loopback_gated, monkeypatch,
):
    """No Desktop ownership, no exemption: a plain gated loopback serve keeps
    refusing the legacy session token."""
    monkeypatch.delenv("HERMES_DESKTOP", raising=False)
    monkeypatch.delenv("HERMES_DASHBOARD_SESSION_TOKEN", raising=False)
    web_server.app.state.desktop_loopback_exempt = False
    r = desktop_loopback_gated.get(
        "/api/profiles", headers={web_server._SESSION_HEADER_NAME: _TOKEN}
    )
    assert r.status_code == 401


def test_exemption_recomputed_when_startup_flag_absent(
    desktop_loopback_gated,
):
    """Fallback when the gate engaged without ``_configure_auth_gate``: the
    bound host + process env still identify the Desktop loopback backend."""
    delattr(web_server.app.state, "desktop_loopback_exempt")
    r = desktop_loopback_gated.get(
        "/api/profiles", headers={web_server._SESSION_HEADER_NAME: _TOKEN}
    )
    assert r.status_code == 200, f"{r.status_code} {r.text}"
    assert desktop_loopback_gated.get("/api/profiles").status_code == 401


def test_configure_auth_gate_records_desktop_exemption(
    monkeypatch, _isolate_hermes_home,
):
    """The startup verdict is stashed for the per-request check (it also
    covers SSH-spawn credentials, which are params rather than request-time
    env)."""
    prev = _save_app_state()
    try:
        monkeypatch.setenv("HERMES_DESKTOP", "1")
        monkeypatch.setenv("HERMES_DASHBOARD_SESSION_TOKEN", "spawn-token")
        monkeypatch.setenv(
            "HERMES_DASHBOARD_PUBLIC_URL", "https://dash.example.test"
        )
        web_server._configure_auth_gate("127.0.0.1", False, None, None)
        assert web_server.app.state.auth_required is False
        assert web_server.app.state.desktop_loopback_exempt is True

        monkeypatch.delenv("HERMES_DESKTOP", raising=False)
        # Without Desktop ownership the non-loopback public_url engages the
        # gate, which fails closed without a provider — the verdict is still
        # recorded before the exit.
        with pytest.raises(SystemExit):
            web_server._configure_auth_gate("127.0.0.1", False, None, None)
        assert web_server.app.state.desktop_loopback_exempt is False
    finally:
        _restore_app_state(prev)
