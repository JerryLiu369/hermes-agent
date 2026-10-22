"""``model_catalog.excluded_providers`` must hide user-defined provider rows.

Providers declared under ``providers:`` never reach the picker through the built-in
``_skip(seen, excluded, ...)`` gate: they are emitted by ``_lap_user_provider_rows`` (plain
slug) and, when they also come back from ``get_compatible_custom_providers``, by the custom
row builder (``custom:<name>`` slug). Neither compared against ``b.excluded``, so an escape
route declared this way stayed selectable in ``/model`` even though the config key and the
docstring both promise it is hidden.
"""

from hermes_cli import model_switch_providers


def _user_providers():
    return {
        "back1": {"base_url": "http://127.0.0.1:9999/v1", "api_key": "k1", "models": {"m1": {}}},
        "com": {"base_url": "http://127.0.0.1:8888/v1", "api_key": "k2", "models": {"m2": {}}},
    }


def _custom_providers():
    return [
        {"name": "back1", "base_url": "http://127.0.0.1:9999/v1", "api_key": "k1", "models": ["m1"]},
        {"name": "com", "base_url": "http://127.0.0.1:8888/v1", "api_key": "k2", "models": ["m2"]},
    ]


def _rows(excluded, *, user=True, custom=False):
    return model_switch_providers.list_authenticated_providers(
        current_provider="com",
        current_base_url="http://127.0.0.1:8888/v1",
        current_model="m2",
        user_providers=_user_providers() if user else {},
        custom_providers=_custom_providers() if custom else [],
        excluded_providers=excluded,
        max_models=5,
        non_blocking_catalogs=True,
        probe_custom_providers=False,
        probe_current_custom_provider=False,
    )


def _slugs(excluded, **kw):
    return {str(r.get("slug", "")).lower() for r in _rows(excluded, **kw)}


def test_excluded_user_provider_plain_slug_is_hidden():
    """The shape a ``providers:`` entry takes on the normal listing path."""
    assert "back1" in _slugs([]), "precondition: row exists when nothing is excluded"
    assert "back1" not in _slugs(["back1"])


def test_excluded_user_provider_custom_slug_is_hidden():
    """``custom_providers`` entries surface as ``custom:<name>``; the bare name must still match."""
    assert "custom:back1" in _slugs([], user=False, custom=True), "precondition"
    assert "custom:back1" not in _slugs(["back1"], user=False, custom=True)


def test_exclusion_accepts_the_prefixed_spelling_too():
    assert "custom:back1" not in _slugs(["custom:back1"], user=False, custom=True)


def test_excluding_one_entry_keeps_the_others():
    slugs = _slugs(["back1"])
    assert "com" in slugs
    assert "back1" not in slugs
