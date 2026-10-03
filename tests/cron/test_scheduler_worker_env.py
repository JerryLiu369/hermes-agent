from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import cron.scheduler_worker_env as swe


def test_pin_hermes_tree_on_pythonpath_includes_runtime_site_packages_on_store_python(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    store_purelib = tmp_path / "store" / "site-packages"
    runtime_sp = tmp_path / "installs" / "venv" / "lib" / "python3.14" / "site-packages"
    runtime_sp.mkdir(parents=True)

    with patch.object(swe, "_installed_purelib", return_value=store_purelib), \
         patch.object(swe, "_runtime_site_packages", return_value=runtime_sp):
        env = {"PYTHONPATH": "/user/custom"}
        result = swe.pin_hermes_tree_on_pythonpath(env, repo_root)

        expected = os.pathsep.join([str(repo_root), str(runtime_sp), "/user/custom"])
        assert result["PYTHONPATH"] == expected


def test_pin_hermes_tree_on_pythonpath_skips_runtime_sp_when_matches_purelib(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    venv_sp = tmp_path / "venv" / "lib" / "python3.14" / "site-packages"
    venv_sp.mkdir(parents=True)

    # When the running interpreter is the venv python, purelib == runtime_sp
    with patch.object(swe, "_installed_purelib", return_value=venv_sp), \
         patch.object(swe, "_runtime_site_packages", return_value=venv_sp):
        env = {"PYTHONPATH": "/user/custom"}
        result = swe.pin_hermes_tree_on_pythonpath(env, repo_root)

        # Runtime site-packages is already the interpreter's default; do not hoist above stdlib
        expected = os.pathsep.join([str(repo_root), "/user/custom"])
        assert result["PYTHONPATH"] == expected


def test_pin_hermes_tree_on_pythonpath_skips_when_purelib_is_repo_root(tmp_path):
    # Wheel / pipx / uv-tool install: repo_root is purelib itself
    repo_root = tmp_path / "site-packages"
    repo_root.mkdir()

    with patch.object(swe, "_installed_purelib", return_value=repo_root):
        env = {"PYTHONPATH": "/user/custom"}
        result = swe.pin_hermes_tree_on_pythonpath(env, repo_root)
        assert result == {"PYTHONPATH": "/user/custom"}


def test_pin_hermes_tree_on_pythonpath_handles_no_runtime_site_packages(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    with patch.object(swe, "_installed_purelib", return_value=None), \
         patch.object(swe, "_runtime_site_packages", return_value=None):
        env = {}
        result = swe.pin_hermes_tree_on_pythonpath(env, repo_root)
        assert result["PYTHONPATH"] == str(repo_root)


def test_pin_hermes_tree_on_pythonpath_deduplicates(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    runtime_sp = tmp_path / "sp"
    runtime_sp.mkdir()

    with patch.object(swe, "_installed_purelib", return_value=None), \
         patch.object(swe, "_runtime_site_packages", return_value=runtime_sp):
        env = {"PYTHONPATH": os.pathsep.join([str(repo_root), "/user/path", str(runtime_sp)])}
        result = swe.pin_hermes_tree_on_pythonpath(env, repo_root)
        assert result["PYTHONPATH"] == os.pathsep.join([str(repo_root), str(runtime_sp), "/user/path"])


def test_runtime_site_packages_resolution_missing(tmp_path):
    assert swe._runtime_site_packages(tmp_path / "nonexistent") is None


def test_runtime_site_packages_resolves_facts_json_generation(tmp_path, monkeypatch):
    """Exercise the real facts.json -> selected_venv -> site_packages branch.

    A facts.json naming a generation under <install_state>/environments/<gen>/
    (with pyvenv.cfg at its root) must resolve to that generation's
    site-packages, and the pin must carry it — no _runtime_site_packages patch.
    """
    import json

    from pm.environments import install_state_dir, site_packages

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_RUNTIME_DIR", raising=False)

    generation = install_state_dir(repo_root) / "environments" / "gen1"
    (generation / "pyvenv.cfg").parent.mkdir(parents=True, exist_ok=True)
    (generation / "pyvenv.cfg").write_text(
        "home = /usr/bin\nversion = 3.14.0\n", encoding="utf-8"
    )
    expected_sp = site_packages(generation)
    expected_sp.mkdir(parents=True, exist_ok=True)

    facts_path = install_state_dir(repo_root) / "facts.json"
    facts_path.parent.mkdir(parents=True, exist_ok=True)
    facts_path.write_text(
        json.dumps({"packages": {"venv": {"environment": str(generation)}}}),
        encoding="utf-8",
    )

    assert swe._runtime_site_packages(repo_root) == expected_sp.resolve()

    env = {"PYTHONPATH": "/user/custom"}
    result = swe.pin_hermes_tree_on_pythonpath(dict(env), repo_root)
    assert result["PYTHONPATH"] == os.pathsep.join(
        [str(repo_root), str(expected_sp.resolve()), "/user/custom"]
    )
