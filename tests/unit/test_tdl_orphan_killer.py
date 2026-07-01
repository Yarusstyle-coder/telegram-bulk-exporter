"""Unit tests for ``tdl_orphan_killer``.

The helper sweeps ``tdl.exe`` processes whose command-line references
*our* bolt-DB storage path. We mock the OS-process listing layer
(PowerShell on Windows, /proc on Linux) so the tests stay fast and
don't depend on any real tdl child being alive.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.services.tdl_orphan_killer import _normalise, kill_orphan_tdl


def test_normalise_case_and_slashes() -> None:
    """Path comparison must survive Windows-style backslashes and the
    mix of upper/lower cases tdl + ourselves can serialise."""
    assert _normalise("S:\\foo\\Bar") == "s:/foo/bar"
    assert _normalise(Path("S:/foo/Bar")) == "s:/foo/bar"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows path")
def test_kill_orphan_tdl_windows_kills_matching_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two tdl.exe processes are alive. Only one points at our storage
    path. The killer must hit exactly that one and ignore the other."""
    storage = Path("S:/PycharmProjects/telegramExportChats/data/tdl")
    our_cmd = (
        "S:\\PycharmProjects\\telegramExportChats\\tools\\tdl\\tdl.exe -n default "
        "--storage type=bolt,path=S:/PycharmProjects/telegramExportChats/data/tdl "
        "chat export -c @foo -o out.json"
    )
    other_cmd = (
        "C:\\Users\\someone\\tdl.exe --storage type=bolt,path=C:/other/db chat ls"
    )

    fake_json = json.dumps([
        {"ProcessId": 1111, "CommandLine": our_cmd},
        {"ProcessId": 2222, "CommandLine": other_cmd},
    ])

    def fake_run(*args, **kwargs):  # noqa: ANN001, ANN202
        return SimpleNamespace(returncode=0, stdout=fake_json, stderr="")

    killed_pids: list[int] = []

    def fake_kill(pid: int, sig: int) -> None:
        killed_pids.append(pid)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr("src.services.tdl_orphan_killer.os.kill", fake_kill)
    monkeypatch.setattr(
        "src.services.tdl_orphan_killer.shutil.which",
        lambda name: "powershell.exe" if name in ("powershell", "pwsh") else None,
    )

    n = kill_orphan_tdl(storage)
    assert n == 1
    assert killed_pids == [1111]  # only our matching pid


@pytest.mark.skipif(sys.platform != "win32", reason="Windows path")
def test_kill_orphan_tdl_windows_no_processes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No tdl.exe at all → empty PS output → killer is a clean no-op."""
    def fake_run(*args, **kwargs):  # noqa: ANN001, ANN202
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(
        "src.services.tdl_orphan_killer.shutil.which",
        lambda name: "powershell.exe" if name in ("powershell", "pwsh") else None,
    )

    assert kill_orphan_tdl(Path("S:/anywhere")) == 0


@pytest.mark.skipif(sys.platform != "win32", reason="Windows path")
def test_kill_orphan_tdl_windows_single_dict_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PowerShell unwraps single-element arrays into plain objects.
    Our parser must accept both shapes."""
    storage = Path("S:/path/data/tdl")
    cmd = "tdl.exe --storage type=bolt,path=S:/path/data/tdl chat export -c @x"
    fake_json = json.dumps({"ProcessId": 7777, "CommandLine": cmd})

    def fake_run(*args, **kwargs):  # noqa: ANN001, ANN202
        return SimpleNamespace(returncode=0, stdout=fake_json, stderr="")

    killed: list[int] = []
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(
        "src.services.tdl_orphan_killer.os.kill",
        lambda pid, sig: killed.append(pid),
    )
    monkeypatch.setattr(
        "src.services.tdl_orphan_killer.shutil.which",
        lambda name: "powershell.exe" if name in ("powershell", "pwsh") else None,
    )

    assert kill_orphan_tdl(storage) == 1
    assert killed == [7777]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows path")
def test_kill_orphan_tdl_windows_powershell_missing_is_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No PowerShell on PATH → killer logs a debug breadcrumb and
    returns 0. Must not raise."""
    monkeypatch.setattr(
        "src.services.tdl_orphan_killer.shutil.which", lambda name: None,
    )
    assert kill_orphan_tdl(Path("S:/whatever")) == 0


def test_kill_orphan_tdl_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even if the underlying subprocess blows up, the boot path
    must continue. A swallowed-error sweep returns 0 — never an
    exception that crashes ``attach_runtime``."""
    def boom(*a, **kw):  # noqa: ANN001, ANN201, ANN202
        raise RuntimeError("simulated PS failure")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(
        "src.services.tdl_orphan_killer.shutil.which",
        lambda name: "powershell.exe" if name else None,
    )

    # On non-Windows the function still must not raise.
    assert kill_orphan_tdl(Path("S:/dummy")) == 0


def test_kill_orphan_tdl_ignores_unrelated_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Process for a DIFFERENT bolt-DB path must NOT be killed —
    that would break someone running another tdl install on the
    same machine."""
    storage = Path("S:/our/data/tdl")
    unrelated_cmd = (
        "tdl.exe -n default --storage type=bolt,path=D:/someone-else/db chat ls"
    )
    fake_json = json.dumps([{"ProcessId": 9999, "CommandLine": unrelated_cmd}])

    def fake_run(*args, **kwargs):  # noqa: ANN001, ANN202
        return SimpleNamespace(returncode=0, stdout=fake_json, stderr="")

    killed: list[int] = []
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(
        "src.services.tdl_orphan_killer.os.kill",
        lambda pid, sig: killed.append(pid),
    )
    monkeypatch.setattr(
        "src.services.tdl_orphan_killer.shutil.which",
        lambda name: "powershell.exe" if name in ("powershell", "pwsh") else None,
    )

    n = kill_orphan_tdl(storage)
    assert n == 0
    assert killed == []  # didn't touch the unrelated tdl
