import pytest

# Avoid tripping home_io_guard when worktree is inside ~/.hermes
import pm.environments
pm.environments.payload_venv = lambda root: None

"""Regression tests for #129418.

Importing ``cli`` in the TUI gateway reset ``TERMINAL_DOCKER_VOLUMES`` from
``.env`` to the default ``[]``: ``_mirror_config_to_env`` treated "the file
has ANY terminal section" as permission to overwrite EVERY ``TERMINAL_*`` env
var with merged defaults. Only a key explicitly present in the file's
``terminal`` section may beat an existing env var; unset vars still backfill
from defaults.
"""

import json
import os
from unittest.mock import patch


def _write_config(home, text):
    (home / "config.yaml").write_text(text, encoding="utf-8")


def _load_with_home(monkeypatch, home):
    import cli

    monkeypatch.setattr(cli, "_hermes_home", home)
    monkeypatch.delenv("HERMES_IGNORE_USER_CONFIG", raising=False)
    monkeypatch.delenv("_HERMES_GATEWAY", raising=False)
    # Ensure no stale managed overlay leaks into the loader.
    monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)
    from hermes_cli import managed_scope

    try:
        managed_scope.invalidate_managed_cache()
    except Exception:
        pass
    return cli.load_cli_config()


def test_dotenv_volumes_survive_unrelated_terminal_section(tmp_path, monkeypatch):
    """A terminal section without docker_volumes must not clobber .env volumes,
    while an explicit backend key still overrides TERMINAL_ENV."""
    home = tmp_path / ".hermes"
    home.mkdir()
    _write_config(home, "terminal:\n  backend: docker\n")

    with patch.dict(os.environ, {"TERMINAL_DOCKER_VOLUMES": json.dumps(["/host:/ctr"]), "TERMINAL_ENV": "local"}):
        _load_with_home(monkeypatch, home)
        assert os.environ["TERMINAL_DOCKER_VOLUMES"] == json.dumps(["/host:/ctr"])
        assert os.environ["TERMINAL_ENV"] == "docker"


def test_missing_keys_backfilled_from_defaults(tmp_path, monkeypatch):
    """Keys absent from both file and env are backfilled from merged defaults."""
    home = tmp_path / ".hermes"
    home.mkdir()
    _write_config(home, "terminal:\n  lifetime_seconds: 300\n")

    with patch.dict(os.environ):
        os.environ.pop("TERMINAL_DOCKER_VOLUMES", None)
        os.environ.pop("TERMINAL_ENV", None)
        _load_with_home(monkeypatch, home)
        assert os.environ["TERMINAL_DOCKER_VOLUMES"] == "[]"
        assert os.environ["TERMINAL_ENV"] == "local"
