"""#129301: prepare_runtime must write a complete relocatable marker.

An incomplete marker (inputs only) breaks every reader that resolves the
runtime from its recorded paths (packaged-resident resolution, payload
sealing validation, AgentInputs validation). The marker must carry the
identity plus python/sitePackages as paths relative to the generation.
"""
import json
import os
from pathlib import Path


def _fixture_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text("[project]\nname = 'fixture'\n")
    (project / "uv.lock").write_text("version = 1\n")
    return project


def _stage_minimal_venv(uv: Path, python: Path, environment: Path, *, version: str = "3.12", **_kwargs) -> Path:
    """Mimic stage_runtime without uv/network: a venv layout with pyvenv.cfg."""
    from pm.environments import venv_python

    executable = venv_python(environment)
    executable.parent.mkdir(parents=True)
    executable.touch()
    if os.name == "nt":
        site = environment / "Lib" / "site-packages"
    else:
        site = environment / "lib" / f"python{version}" / "site-packages"
    site.mkdir(parents=True)
    (environment / "pyvenv.cfg").write_text(f"home = /build\nversion = {version}\n", encoding="utf-8")
    return executable


def test_prepare_runtime_writes_complete_relocatable_marker(tmp_path, monkeypatch):
    """Regression for #129301: the published marker carries inputs + relocatable paths."""
    from pm import runtime

    project = _fixture_project(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("pm.paths.repo_root", lambda: project)
    monkeypatch.setattr("pm.runtime_stage.stage_runtime", _stage_minimal_venv)
    monkeypatch.setattr(runtime, "_validate", lambda python, env: "")
    monkeypatch.setattr(runtime, "_hold_for_children", lambda environment: None)

    root = tmp_path / "pm-runtime"
    python = runtime.prepare_runtime(Path("uv"), Path("python3"), root, project=project)

    assert python.is_file()
    selected = json.loads((root / "selected.json").read_text(encoding="utf-8"))
    environment = root / selected["generation"]
    marker = json.loads((environment / "pm-runtime.json").read_text(encoding="utf-8"))

    assert set(marker) == {"inputs", "python", "sitePackages"}
    assert marker["inputs"] == selected["inputs"]
    assert not Path(marker["python"]).is_absolute()
    assert not Path(marker["sitePackages"]).is_absolute()
    assert (environment / marker["python"]).resolve() == python.resolve()
    assert (environment / marker["sitePackages"]).is_dir()


def test_prepare_runtime_marker_respects_venv_version_not_caller(tmp_path, monkeypatch):
    """The recorded site-packages dates the staged tree, never the caller (cf. venv_python_version)."""
    from pm import runtime

    project = _fixture_project(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("pm.paths.repo_root", lambda: project)

    def stage_other_version(uv: Path, python: Path, environment: Path, **_) -> Path:
        return _stage_minimal_venv(uv, python, environment, version="3.12")

    monkeypatch.setattr("pm.runtime_stage.stage_runtime", stage_other_version)
    monkeypatch.setattr(runtime, "_validate", lambda python, env: "")
    monkeypatch.setattr(runtime, "_hold_for_children", lambda environment: None)

    root = tmp_path / "pm-runtime"
    runtime.prepare_runtime(Path("uv"), Path("python3"), root, project=project)
    selected = json.loads((root / "selected.json").read_text(encoding="utf-8"))
    marker = json.loads((root / selected["generation"] / "pm-runtime.json").read_text(encoding="utf-8"))

    if os.name == "nt":
        assert marker["sitePackages"] == "Lib/site-packages"
    else:
        assert marker["sitePackages"] == "lib/python3.12/site-packages"
