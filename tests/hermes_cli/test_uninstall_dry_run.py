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


def test_dry_run_lists_named_profiles_without_desktop_userdata(monkeypatch, tmp_path, capsys):
    """Full-uninstall dry-run lists named profiles even on a machine with no desktop
    userData dir — the profiles section must not depend on the desktop install."""
    profile = SimpleNamespace(name="work", path=tmp_path / "profiles" / "work")
    monkeypatch.setattr(uninstall, "_is_default_hermes_home", lambda home: True)
    monkeypatch.setattr(uninstall, "_discover_named_profiles", lambda: [profile])
    monkeypatch.setattr(
        "hermes_cli.gui_uninstall.desktop_userdata_dir", lambda: tmp_path / "absent-userdata"
    )

    uninstall._print_uninstall_dry_run(
        project_root=tmp_path, hermes_home=tmp_path / ".hermes", full_uninstall=True
    )

    out = capsys.readouterr().out
    assert "Named profiles" in out
    assert "work" in out


def test_build_uninstall_parser_accepts_dry_run():
    import argparse
    from hermes_cli.subcommands.uninstall import build_uninstall_parser

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    build_uninstall_parser(subparsers, cmd_uninstall=lambda args: args)

    args = parser.parse_args(["uninstall", "--dry-run", "--full"])

    assert args.dry_run is True
    assert args.full is True


def test_gui_dry_run_skips_confirm_and_deletes_nothing(monkeypatch, tmp_path, capsys):
    """`hermes uninstall --gui --dry-run` prints the dry-run notice and exits
    before prompting or deleting anything (regression: it ignored --dry-run
    and removed files)."""
    import builtins

    hermes_home = tmp_path / ".hermes"
    agent_root = hermes_home / "hermes-agent"
    desktop = agent_root / "apps" / "desktop"
    (desktop / "dist").mkdir(parents=True)
    (desktop / "dist" / "index.html").write_text("<html>")
    (agent_root / "hermes_cli").mkdir(parents=True)

    monkeypatch.setattr(uninstall, "get_hermes_home", lambda: hermes_home)
    monkeypatch.setattr(uninstall, "_refuse_if_steward_owned", lambda: None)
    monkeypatch.setattr(
        "hermes_cli.gui_uninstall.packaged_gui_app_paths", lambda: []
    )
    monkeypatch.setattr(
        "hermes_cli.gui_uninstall.desktop_userdata_dir",
        lambda: tmp_path / "absent-userdata",
    )

    def _fail_on_prompt(_text=""):
        raise AssertionError("dry run must not prompt for confirmation")

    def _fail_on_delete(_home, **_kwargs):
        raise AssertionError("dry run must not delete GUI artifacts")

    monkeypatch.setattr(builtins, "input", _fail_on_prompt)
    monkeypatch.setattr("hermes_cli.gui_uninstall.uninstall_gui", _fail_on_delete)

    uninstall.run_gui_uninstall(SimpleNamespace(yes=False, dry_run=True))

    out = capsys.readouterr().out
    assert "Dry run: no files, services, or environment entries will be changed." in out
    assert (desktop / "dist" / "index.html").exists()


def _non_tty_stdin(monkeypatch):
    """Simulate a piped/non-interactive stdin for the TTY gate."""
    import sys

    class _FakeStdin:
        def isatty(self):
            return False

    monkeypatch.setattr(sys, "stdin", _FakeStdin())


def test_cmd_uninstall_gui_dry_run_without_tty_succeeds(monkeypatch):
    """`hermes uninstall --gui --dry-run` through a pipe must not hit the TTY
    gate — there is nothing to prompt for."""
    import argparse

    from hermes_cli.subcommands.uninstall import build_uninstall_parser

    _non_tty_stdin(monkeypatch)
    called = {}

    def _fake_gui_uninstall(args):
        called["args"] = args

    monkeypatch.setattr(uninstall, "run_gui_uninstall", _fake_gui_uninstall)

    parser = argparse.ArgumentParser()
    build_uninstall_parser(parser.add_subparsers(dest="command"), cmd_uninstall=main.cmd_uninstall)
    args = parser.parse_args(["uninstall", "--gui", "--dry-run"])
    args.func(args)  # must not raise SystemExit from _require_tty

    assert called["args"].dry_run is True


def test_cmd_uninstall_default_dry_run_without_tty_succeeds(monkeypatch):
    """`hermes uninstall --dry-run` through a pipe must not hit the TTY gate."""
    import argparse

    from hermes_cli.subcommands.uninstall import build_uninstall_parser

    _non_tty_stdin(monkeypatch)
    called = {}

    def _fake_run_uninstall(args):
        called["args"] = args

    monkeypatch.setattr(uninstall, "run_uninstall", _fake_run_uninstall)

    parser = argparse.ArgumentParser()
    build_uninstall_parser(parser.add_subparsers(dest="command"), cmd_uninstall=main.cmd_uninstall)
    args = parser.parse_args(["uninstall", "--dry-run"])
    args.func(args)  # must not raise SystemExit from _require_tty

    assert called["args"].dry_run is True
