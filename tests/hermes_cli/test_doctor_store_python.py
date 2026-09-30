"""Windows Store python alias detection (#129102).

``windows_store_python_stubs`` takes resolved paths as data so these tests run
on any host without faking the OS. The one ``platforms("windows")`` test below
drives the real ``shutil.which`` results on a genuine Windows runner (CI OS
lane / wine2e) as the live receipt.
"""
import pytest

from hermes_cli import doctor_platform
from hermes_cli.doctor_report import Finding


def test_both_aliases_reported():
    assert doctor_platform.windows_store_python_stubs(
        r"C:\Users\u\AppData\Local\Microsoft\WindowsApps\python.exe",
        r"C:\Users\u\AppData\Local\Microsoft\WindowsApps\python3.exe",
    ) == ["python", "python3"]


def test_msys_spelling_counts_as_alias():
    assert doctor_platform.windows_store_python_stubs(
        r"C:\Python314\python.exe",
        "/c/Users/u/AppData/Local/Microsoft/WindowsApps/python3",
    ) == ["python3"]


def test_real_interpreters_are_not_reported():
    assert doctor_platform.windows_store_python_stubs(
        r"C:\Python314\python.exe",
        r"C:\Python314\python3.exe",
    ) == []
    assert doctor_platform.windows_store_python_stubs(None, None) == []


def test_check_is_silent_off_windows(capsys):
    """The win32-gated check stays silent on this (Linux) host without faking
    the platform — it must never warn here."""
    f = Finding()
    doctor_platform._check_windows_store_python_aliases(f)
    assert capsys.readouterr().out == ""
    assert f.manual_issues == []


@pytest.mark.platforms("windows")
def test_live_which_results_agree_with_alias_helper():
    """Live Windows receipt: the helper's verdict on the real ``which``
    results matches a direct ``is_windows_app_alias`` probe, and the check
    never raises on the live host."""
    import shutil

    from pm.shell import is_windows_app_alias

    python_path = shutil.which("python")
    python3_path = shutil.which("python3")
    expected = [
        name for name, path in (("python", python_path), ("python3", python3_path))
        if path and is_windows_app_alias(path)
    ]
    assert doctor_platform.windows_store_python_stubs(python_path, python3_path) == expected
    doctor_platform._check_windows_store_python_aliases(Finding())
