"""Cron: import path of the restart-safe external worker.

The worker is spawned as ``sys.executable -m cron.scheduler``. Its entry module is
``cron.scheduler``, not ``hermes_cli.main``, so nothing bootstraps the gateway's checkout
onto its ``sys.path``; historically it imported ``cron`` only through the implicit ``-m``
cwd entry. That entry is gone under ``PYTHONSAFEPATH`` and useless when the venv's
editable install maps a moved/deleted checkout -- the worker then dies with
"No module named 'cron'" before its ownership ack (#112729, hypothesised cause).

The shared subprocess sanitizer strips Hermes-owned PYTHONPATH entries because user
children must not see our tree. This child IS Hermes, so the pin is applied *after* the
env is built, on the sanitized env -- the sanitizer's other decisions (dropped venv
markers) stand, but the runtime site-packages are pinned alongside the repo root
so dependencies (croniter, ruamel, etc.) remain importable under bare store interpreters
(#129235).
"""

from __future__ import annotations

import os
import sysconfig
from pathlib import Path


def _installed_purelib() -> Path | None:
    try:
        return Path(sysconfig.get_paths()["purelib"]).resolve()
    except (KeyError, OSError):
        return None


def _runtime_site_packages(repo_root: Path) -> Path | None:
    """Resolve the runtime environment's site-packages directory for *repo_root*, if any."""
    try:
        from pm.environments import runtime_facts_path, selected_venv, site_packages

        if runtime_facts_path(repo_root).is_file():
            sp = site_packages(selected_venv(repo_root)).resolve()
            if sp.is_dir():
                return sp
        venv = selected_venv(repo_root)
        if venv and venv.is_dir():
            sp = site_packages(venv).resolve()
            if sp.is_dir():
                return sp
    except Exception:
        pass
    return None


def pin_hermes_tree_on_pythonpath(worker_env: dict, repo_root: Path) -> dict:
    """Prepend ``repo_root`` to the worker env's own PYTHONPATH (never ``os.environ``'s).

    Skipped when ``repo_root`` is the interpreter's ``purelib``: under a wheel / pipx /
    uv-tool install ``cron/`` lives in site-packages itself, which is already importable,
    and pinning it would move site-packages ahead of the stdlib on ``sys.path``.
    """
    root = str(repo_root)
    purelib = _installed_purelib()
    if purelib is not None and purelib == Path(root).resolve():
        return worker_env

    pins = [root]
    sp = _runtime_site_packages(repo_root)
    if sp is not None and (purelib is None or purelib != sp):
        pins.append(str(sp))

    existing = [e for e in worker_env.get("PYTHONPATH", "").split(os.pathsep) if e]
    worker_env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys([*pins, *existing]))
    return worker_env
