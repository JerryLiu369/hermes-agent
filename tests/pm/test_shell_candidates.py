"""Windows bash resolution as pure data: Program Files Git beats PATH, and
the WSL / MSIX stubs never win (#116818). Host-independent — the candidate
ladder takes the ``which`` result and env as arguments."""
import pytest

from pm.shell import is_windows_app_alias, windows_bash_candidates

PF = r"D:\Progs"


@pytest.mark.parametrize(
    "stub",
    [r"C:\Windows\System32\bash.exe", r"C:\WINDOWS\system32\bash.exe",
     r"C:\Users\u\AppData\Local\Microsoft\WindowsApps\bash.exe"],
)
def test_system_stub_bash_is_never_a_candidate(stub):
    assert stub not in windows_bash_candidates(stub, {"ProgramFiles": PF})


def test_program_files_git_precedes_the_path_bash():
    on_path = r"C:\msys64\usr\bin\bash.exe"
    candidates = windows_bash_candidates(on_path, {"ProgramFiles": PF})
    assert candidates[0] == PF + r"\Git\bin\bash.exe"
    assert candidates[-1] == on_path

def test_per_user_and_32bit_git_roots_are_candidates():
    candidates = windows_bash_candidates(None, {
        "ProgramFiles": PF, "ProgramFiles(x86)": r"D:\Progs32",
        "LOCALAPPDATA": r"C:\Users\u\AppData\Local",
    })
    assert r"D:\Progs32\Git\bin\bash.exe" in candidates
    assert r"C:\Users\u\AppData\Local\Programs\Git\bin\bash.exe" in candidates
    assert r"C:\Users\u\AppData\Local\hermes\git\usr\bin\bash.exe" in candidates

def test_nonstarting_bash_is_rejected(monkeypatch):
    import subprocess
    from pm import shell

    monkeypatch.setattr(shell.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 1))
    assert shell._bash_starts("broken-bash.exe") is False


@pytest.mark.parametrize(
    "stub",
    [r"C:\Users\u\AppData\Local\Microsoft\WindowsApps\python.exe",
     r"C:\Users\u\AppData\Local\Microsoft\WindowsApps\python3.exe",
     r"C:\Users\u\AppData\Local\Microsoft\WindowsApps\Python3.EXE",
     "/c/Users/u/AppData/Local/Microsoft/WindowsApps/python3",
     "/c/Users/u/AppData/Local/Microsoft/WindowsApps/python3.exe"],
)
def test_windows_app_alias_detects_store_python_stubs(stub):
    """#129102: python/python3 under WindowsApps are Store aliases, in native
    and MSYS spellings. Pure path data — no host faking."""
    assert is_windows_app_alias(stub) is True


@pytest.mark.parametrize(
    "real",
    [None, "",
     r"C:\Python314\python.exe",
     r"C:\Users\u\AppData\Local\hermes\tools\python-3.14.7-win32-x64\python.exe",
     r"C:\msys64\usr\bin\python3",
     "/usr/bin/python3"],
)
def test_windows_app_alias_rejects_real_interpreters(real):
    assert is_windows_app_alias(real) is False


def test_windows_app_alias_needs_a_path_component():
    """A substring is not enough: C:\\mywindowsapps\\python.exe is real."""
    assert is_windows_app_alias(r"C:\mywindowsapps\python.exe") is False
