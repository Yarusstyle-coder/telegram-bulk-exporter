"""Mocked tests for :mod:`src.services.tdl_wrapper`.

We patch :func:`asyncio.create_subprocess_exec` with a fake process that
yields canned stdout / stderr lines, and assert:

* argv is built correctly for each subcommand;
* progress callbacks receive :class:`TdlProgress` / :class:`TdlError`
  events as lines stream in;
* a non-zero return code surfaces as :class:`TdlSubprocessError`.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from src.services.tdl_types import TdlError, TdlProgress
from src.services.tdl_wrapper import (
    TdlSubprocessError,
    TdlWrapper,
    build_argv,
)


# ---------------------------------------------------------------------------
# Fake subprocess machinery
# ---------------------------------------------------------------------------
class _FakeReader:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    async def readline(self) -> bytes:
        if not self._lines:
            return b""
        return self._lines.pop(0)


class _FakeProc:
    def __init__(
        self,
        stdout_lines: list[bytes],
        stderr_lines: list[bytes],
        returncode: int = 0,
    ) -> None:
        self.stdout = _FakeReader(stdout_lines)
        self.stderr = _FakeReader(stderr_lines)
        self.returncode: int | None = None
        self._rc = returncode
        self.killed = False

    async def wait(self) -> int:
        # Yield so the pump tasks get a chance to drain.
        await asyncio.sleep(0)
        self.returncode = self._rc
        return self._rc

    async def communicate(
        self, input: bytes | None = None
    ) -> tuple[bytes, bytes]:
        out = b"\n".join(ln.rstrip(b"\n") for ln in self.stdout._lines)
        err = b"\n".join(ln.rstrip(b"\n") for ln in self.stderr._lines)
        self.stdout._lines.clear()
        self.stderr._lines.clear()
        self.returncode = self._rc
        return out, err

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


def _install_fake_exec(
    monkeypatch: pytest.MonkeyPatch, proc: _FakeProc
) -> dict[str, Any]:
    """Patch `asyncio.create_subprocess_exec` to return ``proc``.

    Returns a dict we populate with the argv actually passed, so tests
    can assert on it.
    """
    captured: dict[str, Any] = {}

    async def fake_exec(*argv: str, stdout: Any = None, stderr: Any = None, **_: Any):
        captured["argv"] = list(argv)
        captured["stdout"] = stdout
        captured["stderr"] = stderr
        return proc

    monkeypatch.setattr(
        "src.services.tdl_wrapper.asyncio.create_subprocess_exec", fake_exec
    )
    return captured


@pytest.fixture
def tdl_binary(tmp_path: Path) -> Path:
    """A dummy tdl binary file — only needs to exist for init checks."""
    b = tmp_path / "tdl.exe"
    b.write_bytes(b"\x00")
    return b


# ---------------------------------------------------------------------------
# argv assembly
# ---------------------------------------------------------------------------
def test_build_argv_places_global_flags_before_subcommand(tdl_binary: Path) -> None:
    argv = build_argv(
        tdl_binary,
        ["chat", "ls"],
        namespace="myns",
        proxy="http://p:1",
        extra=["-o", "json"],
    )
    assert argv[0] == str(tdl_binary)
    # Global flags before subcommand path
    ns_idx = argv.index("-n")
    proxy_idx = argv.index("--proxy")
    chat_idx = argv.index("chat")
    assert ns_idx < chat_idx and proxy_idx < chat_idx
    # Extras after subcommand
    assert argv.index("-o") > chat_idx


def test_build_argv_omits_optional_flags(tdl_binary: Path) -> None:
    argv = build_argv(tdl_binary, ["version"])
    assert "-n" not in argv
    assert "--proxy" not in argv


# ---------------------------------------------------------------------------
# version()
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_version_parses(monkeypatch: pytest.MonkeyPatch, tdl_binary: Path) -> None:
    proc = _FakeProc(
        stdout_lines=[
            b"Version: 0.20.2\n",
            b"Commit: deadbeef\n",
            b"Date: 2026-01-01T00:00:00Z\n",
        ],
        stderr_lines=[],
        returncode=0,
    )
    captured = _install_fake_exec(monkeypatch, proc)

    w = TdlWrapper(binary_path=tdl_binary, namespace="ns", proxy=None)
    v = await w.version()
    assert v == "v0.20.2"

    # version() omits -n / --proxy per our implementation decision.
    argv = captured["argv"]
    assert argv[0] == str(tdl_binary.resolve())
    assert "version" in argv
    assert "-n" not in argv


# ---------------------------------------------------------------------------
# chat_ls()
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_chat_ls_parses_json(
    monkeypatch: pytest.MonkeyPatch, tdl_binary: Path, tmp_path: Path
) -> None:
    fake_json = json.dumps(
        [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]
    ).encode()
    proc = _FakeProc(stdout_lines=[fake_json], stderr_lines=[], returncode=0)
    captured = _install_fake_exec(monkeypatch, proc)

    w = TdlWrapper(binary_path=tdl_binary, namespace="ns", proxy=None)
    out_json = tmp_path / "chats.json"
    data = await w.chat_ls(output_json=out_json)

    assert len(data) == 2
    assert data[0]["name"] == "A"
    assert out_json.exists()
    assert "chat" in captured["argv"]
    assert "ls" in captured["argv"]
    assert "-o" in captured["argv"] and "json" in captured["argv"]


# ---------------------------------------------------------------------------
# chat_export() — argv assembly + progress streaming
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_chat_export_builds_argv_and_streams_progress(
    monkeypatch: pytest.MonkeyPatch, tdl_binary: Path, tmp_path: Path
) -> None:
    # Two progress frames then a flood-wait, emitted via stderr.
    stderr_lines = [
        b"EXPORTING: 25%  25/100\n",
        b"EXPORTING: 75%  75/100  1.0 MiB/s  eta 1s\n",
        b"EXPORTING done\n",
    ]
    proc = _FakeProc(stdout_lines=[], stderr_lines=stderr_lines, returncode=0)
    captured = _install_fake_exec(monkeypatch, proc)

    # The wrapper reads JSON from the output path after success.
    export_path = tmp_path / "export.json"
    export_path.write_text(
        json.dumps(
            {
                "id": 42,
                "chat_id": 42,
                "messages": [
                    {"id": 1},
                    {"id": 2},
                    {"id": 3},
                ],
            }
        ),
        encoding="utf-8",
    )

    received: list[TdlProgress | TdlError] = []

    def cb(ev: TdlProgress | TdlError) -> None:
        received.append(ev)

    w = TdlWrapper(
        binary_path=tdl_binary,
        namespace="ns",
        proxy="socks5://127.0.0.1:1080",
        on_progress=cb,
    )
    result = await w.chat_export(
        chat=42,
        output=export_path,
        from_id=10,
        to_id=20,
        with_content=True,
        include_all=True,
    )

    # argv assertions
    argv = captured["argv"]
    # Global flags precede subcommand verbs
    assert argv.index("-n") < argv.index("chat")
    assert argv.index("--proxy") < argv.index("chat")
    assert "export" in argv
    assert "-c" in argv and "42" in argv
    assert "-o" in argv and str(export_path) in argv
    # -i 10,20 -T id
    i_idx = argv.index("-i")
    assert argv[i_idx + 1] == "10,20"
    t_idx = argv.index("-T")
    assert argv[t_idx + 1] == "id"
    assert "--with-content" in argv
    assert "--all" in argv

    # progress events
    progresses = [e for e in received if isinstance(e, TdlProgress)]
    assert len(progresses) >= 2
    assert all(p.stage == "export" for p in progresses)
    assert progresses[0].current == 25
    assert progresses[-1].current == 75

    # result
    assert result.chat_id == 42
    assert result.count_messages == 3
    assert result.first_id == 1
    assert result.last_id == 3
    assert result.path == export_path


@pytest.mark.asyncio
async def test_chat_export_from_only_uses_int32_ceiling(
    monkeypatch: pytest.MonkeyPatch, tdl_binary: Path, tmp_path: Path,
) -> None:
    """The "from X onwards to current tip" case — only ``from_id`` is
    set. The wrapper MUST expand the missing ``to_id`` to the int32
    ceiling so tdl's inclusive ``-i lo,hi`` filter doesn't read it as
    "from 0 to X" (which silently re-fetched the entire backward
    history on every incremental sync — see ``fad1f60`` /
    ``e3b38169``-style hangs at 99%)."""
    proc = _FakeProc(stdout_lines=[], stderr_lines=[b"done\n"], returncode=0)
    captured = _install_fake_exec(monkeypatch, proc)

    export_path = tmp_path / "out.json"
    export_path.write_text(
        json.dumps({"id": 1, "chat_id": 1, "messages": []}), encoding="utf-8",
    )
    w = TdlWrapper(binary_path=tdl_binary, namespace="ns", proxy=None)
    await w.chat_export(chat=1, output=export_path, from_id=462517)

    argv = captured["argv"]
    i_idx = argv.index("-i")
    # ``hi`` should be 2**31-1 (signed int32 max), NOT 0.
    assert argv[i_idx + 1] == "462517,2147483647"
    assert argv[argv.index("-T") + 1] == "id"


@pytest.mark.asyncio
async def test_chat_export_to_only_keeps_zero_lower_bound(
    monkeypatch: pytest.MonkeyPatch, tdl_binary: Path, tmp_path: Path,
) -> None:
    """The reverse — only ``to_id`` is set ("up to X"). ``from_id``
    defaults to 0 because Telegram message ids start at 1; this
    selects the full history up to the cap. Pin the behaviour so a
    later refactor doesn't accidentally swap the defaults."""
    proc = _FakeProc(stdout_lines=[], stderr_lines=[b"done\n"], returncode=0)
    captured = _install_fake_exec(monkeypatch, proc)

    export_path = tmp_path / "out.json"
    export_path.write_text(
        json.dumps({"id": 1, "chat_id": 1, "messages": []}), encoding="utf-8",
    )
    w = TdlWrapper(binary_path=tdl_binary, namespace="ns", proxy=None)
    await w.chat_export(chat=1, output=export_path, to_id=1000)

    argv = captured["argv"]
    assert argv[argv.index("-i") + 1] == "0,1000"


@pytest.mark.asyncio
async def test_chat_export_neither_id_omits_range(
    monkeypatch: pytest.MonkeyPatch, tdl_binary: Path, tmp_path: Path,
) -> None:
    """No ``from_id``, no ``to_id``, no ``last_n`` → no ``-i`` flag
    at all. tdl then dumps the whole chat. We rely on this for the
    "first-time export" path."""
    proc = _FakeProc(stdout_lines=[], stderr_lines=[b"done\n"], returncode=0)
    captured = _install_fake_exec(monkeypatch, proc)

    export_path = tmp_path / "out.json"
    export_path.write_text(
        json.dumps({"id": 1, "chat_id": 1, "messages": []}), encoding="utf-8",
    )
    w = TdlWrapper(binary_path=tdl_binary, namespace="ns", proxy=None)
    await w.chat_export(chat=1, output=export_path)

    argv = captured["argv"]
    assert "-i" not in argv
    assert "-T" not in argv


@pytest.mark.asyncio
async def test_chat_export_nonzero_raises(
    monkeypatch: pytest.MonkeyPatch, tdl_binary: Path, tmp_path: Path
) -> None:
    stderr_lines = [
        b"ERRO    FLOOD_WAIT_X 30\n",
    ]
    proc = _FakeProc(stdout_lines=[], stderr_lines=stderr_lines, returncode=1)
    _install_fake_exec(monkeypatch, proc)

    w = TdlWrapper(binary_path=tdl_binary, namespace="ns", proxy=None)
    with pytest.raises(TdlSubprocessError) as excinfo:
        await w.chat_export(
            chat="somechat",
            output=tmp_path / "out.json",
        )
    # The flood-wait got parsed into the structured errors list.
    kinds = [e.kind for e in excinfo.value.errors]
    assert "flood_wait" in kinds


# ---------------------------------------------------------------------------
# dl() — argv
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_dl_argv_and_streams(
    monkeypatch: pytest.MonkeyPatch, tdl_binary: Path, tmp_path: Path
) -> None:
    stderr_lines = [
        b"DOWNLOADING 50% 5/10 1.0 MiB/s eta 5s\n",
        b"DOWNLOADING done\n",
    ]
    proc = _FakeProc(stdout_lines=[], stderr_lines=stderr_lines, returncode=0)
    captured = _install_fake_exec(monkeypatch, proc)

    received: list[TdlProgress | TdlError] = []

    w = TdlWrapper(
        binary_path=tdl_binary,
        namespace="ns",
        proxy=None,
        on_progress=received.append,
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text("[]", encoding="utf-8")
    out_dir = tmp_path / "downloaded"

    result = await w.dl(
        manifest,
        output_dir=out_dir,
        threads=8,
        limit=3,
        skip_same=True,
        reconnect_timeout=10,
    )

    argv = captured["argv"]
    assert "dl" in argv
    f_idx = argv.index("-f")
    assert argv[f_idx + 1] == str(manifest)
    d_idx = argv.index("-d")
    assert argv[d_idx + 1] == str(out_dir)
    t_idx = argv.index("-t")
    assert argv[t_idx + 1] == "8"
    l_idx = argv.index("-l")
    assert argv[l_idx + 1] == "3"
    assert "--skip-same" in argv

    assert result.elapsed_seconds >= 0
    assert any(isinstance(e, TdlProgress) for e in received)


# ---------------------------------------------------------------------------
# login_exists()
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_login_exists_false_on_auth_error(
    monkeypatch: pytest.MonkeyPatch, tdl_binary: Path
) -> None:
    proc = _FakeProc(
        stdout_lines=[],
        stderr_lines=[b"ERRO    AUTH_KEY_UNREGISTERED\n"],
        returncode=1,
    )
    _install_fake_exec(monkeypatch, proc)

    w = TdlWrapper(binary_path=tdl_binary, namespace="ns", proxy=None)
    assert await w.login_exists() is False


@pytest.mark.asyncio
async def test_login_exists_true_on_success(
    monkeypatch: pytest.MonkeyPatch, tdl_binary: Path
) -> None:
    proc = _FakeProc(
        stdout_lines=[b"[]\n"], stderr_lines=[], returncode=0
    )
    _install_fake_exec(monkeypatch, proc)

    w = TdlWrapper(binary_path=tdl_binary, namespace="ns", proxy=None)
    assert await w.login_exists() is True
