"""Shared allowlist of ``/api/*`` paths that bypass dashboard auth. Imported by BOTH gates —
``web_server.auth_middleware`` (loopback / ``--insecure``) and
``dashboard_auth.middleware.gated_auth_middleware`` (OAuth cookie) — so the lists cannot drift
again (a drift once 401'd ``/api/status`` and broke the portal's cookie-less liveness probe).
Keep minimal: every entry must be safe for external uptime probes, the pre-login SPA, and anyone
who ``curl``s the hostname; otherwise gate it and bootstrap after login."""
from __future__ import annotations

PUBLIC_API_PATHS: frozenset[str] = frozenset({
    # Minimal process liveness probe for desktop/backend boot handshakes; avoids
    # gateway config, platform discovery, MCP setup and cold plugin imports.
    "/api/health",
    # Portal wildcard liveness probe (``docs/agent-dashboard-public-url-contract.md``,
    # NAS side): version, gateway state, session count, auth-gate shape. No secrets.
    "/api/status",
    # Read-only config-defaults / schema feeds for the SPA's Config page.
    "/api/config/defaults",
    "/api/config/schema",
    # Read-only model metadata — same shape as public provider catalogs.
    "/api/model/info",
    # Read-only theme + plugin manifests for the dashboard skin engine.
    "/api/dashboard/themes",
    "/api/dashboard/plugins",
    # Chronos managed-cron fire webhook (NAS -> agent). NOT cookie-gated: it
    # carries its own short-lived NAS-minted JWT (purpose=cron_fire), which the
    # handler verifies — the JWT, not this allowlist, is the security boundary.
    "/api/cron/fire"})


# Dashboard read families the Desktop app polls on its OWNED loopback backend
# (#126391). Deliberately NOT in ``PUBLIC_API_PATHS``: that allowlist is
# bind-scoped to nowhere and method-agnostic, so listing ``/api/profiles``
# there would also open ``POST /api/profiles`` (unauthenticated profile
# creation) on every bind, while the subpaths the view needs
# (``/api/sessions/{id}/messages``) would stay gated. These prefixes are only
# honoured together with the loopback + credential-gated Desktop exemption
# (loopback bind, ``HERMES_DESKTOP=1``, operator-minted credential) AND a
# valid per-spawn session token on the request — see
# ``middleware._desktop_loopback_session_token_exempt``. GET/HEAD only: every
# state-changing verb on these families stays behind the normal gate.
DESKTOP_LOOPBACK_EXEMPT_API_PREFIXES: frozenset[str] = frozenset({
    "/api/profiles",
    "/api/sessions",
    "/api/kanban",
    "/api/artifacts"})

# Verbs the exemption honours. Read-only: POST/PATCH/PUT/DELETE on these
# families (profile create/rename/delete, session prune/import, board writes)
# must never bypass the gate.
_DESKTOP_LOOPBACK_EXEMPT_METHODS: frozenset[str] = frozenset({"GET", "HEAD"})


def is_desktop_loopback_exempt_path(path: str, method: str) -> bool:
    """True when ``(path, method)`` falls under the #126391 Desktop-loopback
    exemption: a ``GET``/``HEAD`` read against one of
    :data:`DESKTOP_LOOPBACK_EXEMPT_API_PREFIXES`, matched as the prefix itself
    or a ``/``-delimited subpath so ``/api/sessions/{id}/messages`` is covered
    without leaking ``/api/sessions-evil``. The caller must still verify the
    loopback bind, the Desktop credential, and the request's session token."""
    if (method or "").upper() not in _DESKTOP_LOOPBACK_EXEMPT_METHODS:
        return False
    return any(path == prefix or path.startswith(prefix + "/")
               for prefix in DESKTOP_LOOPBACK_EXEMPT_API_PREFIXES)
