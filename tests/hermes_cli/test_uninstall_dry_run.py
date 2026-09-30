from pathlib import Path
from types import SimpleNamespace

from hermes_cli import main, uninstall


def test_dry_run_prints_plan_without_mutating(monkeypatch, tmp_path, capsys):
    project_root = tmp_path / "hermes-agent"
    hermes_home = tmp_path / ".hermes"
    project_root.mkdir()
    # A .git dir marks the tree as a removable git checkout — without it the
    # install-kind gate refuses before the dry-run plan prints.
    (project_root / ".git").mkdir()
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text("model: {}\n", encoding="utf-8")

    called = False

    def _fail_if_called(**kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(uninstall, "get_project_root", lambda: project_root)
    monkeypatch.setattr(uninstall, "get_hermes_home", lambda: hermes_home)
    monkeypatch.setattr(uninstall, "_is_default_hermes_home", lambda home: False)
    monkeypatch.setattr(uninstall, "_discover_named_profiles", lambda: [])
    monkeypatch.setattr(uninstall, "_perform_uninstall", _fail_if_called)

    uninstall.run_uninstall(SimpleNamespace(dry_run=True, yes=True, full=True))

    output = capsys.readouterr().out
    assert called is False
    assert "Dry run" in output
    assert str(project_root) in output
    assert str(hermes_home) in output
    assert project_root.exists()
    assert hermes_home.exists()


def test_build_uninstall_parser_accepts_dry_run():
    import argparse
    from hermes_cli.subcommands.uninstall import build_uninstall_parser

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    build_uninstall_parser(subparsers, cmd_uninstall=lambda args: args)

    args = parser.parse_args(["uninstall", "--dry-run", "--full"])

    assert args.dry_run is True
    assert args.full is True


def test_gui_dry_run_skips_prompt_and_removal(monkeypatch, tmp_path, capsys):
    """Regression for #128974: --dry-run must not prompt or remove GUI files."""
    import builtins

    import hermes_cli.gui_uninstall as gu

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    gui_artifact = tmp_path / "Hermes.app"
    gui_artifact.mkdir()

    monkeypatch.setattr(uninstall, "_refuse_if_steward_owned", lambda: None)
    monkeypatch.setattr(uninstall, "get_hermes_home", lambda: hermes_home)
    monkeypatch.setattr(
        gu,
        "gui_install_summary",
        lambda home=None: {
            "gui_installed": True,
            "source_built_artifacts": [str(gui_artifact)],
            "packaged_app_paths": [],
            "userdata_dir": str(tmp_path / "userdata"),
            "userdata_exists": False,
        },
    )
    monkeypatch.setattr(gu, "agent_is_installed", lambda home: True)

    def _fail_uninstall(*args, **kwargs):
        raise AssertionError("uninstall_gui must not run during a dry run")

    def _fail_prompt(*args, **kwargs):
        raise AssertionError("dry run must not prompt for confirmation")

    monkeypatch.setattr(gu, "uninstall_gui", _fail_uninstall)
    monkeypatch.setattr(uninstall, "_confirm_yes", _fail_prompt)
    monkeypatch.setattr(builtins, "input", _fail_prompt)

    uninstall.run_gui_uninstall(SimpleNamespace(dry_run=True, yes=False))

    output = capsys.readouterr().out
    assert "Will remove:" in output
    assert "Dry run: no files or directories removed." in output
    assert gui_artifact.exists()


def test_gui_dry_run_skips_tty_gate(monkeypatch, capsys):
    """cmd_uninstall --gui --dry-run works without a TTY, like --data --dry-run."""

    monkeypatch.setattr(
        main, "_require_tty", lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("_require_tty must not run during a dry run"))
    )
    calls = []
    monkeypatch.setattr(
        uninstall, "run_gui_uninstall", lambda args: calls.append(args))

    main.cmd_uninstall(
        SimpleNamespace(gui=True, data=False, gui_summary=False,
                        yes=False, dry_run=True))

    assert len(calls) == 1
    assert calls[0].dry_run is True
