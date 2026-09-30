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
