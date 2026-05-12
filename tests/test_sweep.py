"""Tests for WR-8: crash detection, stopping flag, log-tail, wt status exits."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import wt as wt_mod
from wt import (
    _read_log_tail,
    _sweep_exits,
    _write_state_atomic,
    _read_state,
    CACHE_DIR,
    main,
)
from tests.conftest import write_state_file


# ─────────────────────────────────────────────────────────────────────────────
# _read_log_tail
# ─────────────────────────────────────────────────────────────────────────────

class TestReadLogTail:
    def test_missing_file_returns_empty(self, tmp_path):
        assert _read_log_tail(tmp_path / "nope.log") == []

    def test_reads_all_lines_when_fewer_than_n(self, tmp_path):
        f = tmp_path / "a.log"
        f.write_text("line1\nline2\nline3\n")
        assert _read_log_tail(f, n=10) == ["line1", "line2", "line3"]

    def test_truncates_to_n_lines(self, tmp_path):
        f = tmp_path / "a.log"
        lines = [f"line{i}" for i in range(20)]
        f.write_text("\n".join(lines) + "\n")
        tail = _read_log_tail(f, n=10)
        assert len(tail) == 10
        assert tail == lines[-10:]

    def test_strips_trailing_newline(self, tmp_path):
        f = tmp_path / "a.log"
        f.write_text("hello\n")
        assert _read_log_tail(f, n=5) == ["hello"]

    def test_empty_file_returns_empty(self, tmp_path):
        f = tmp_path / "empty.log"
        f.write_text("")
        assert _read_log_tail(f) == []


# ─────────────────────────────────────────────────────────────────────────────
# _sweep_exits
# ─────────────────────────────────────────────────────────────────────────────

def _make_state(cache_dir: Path, state_id: str, pid: int, **extra) -> Path:
    st = {
        "boot_id": wt_mod._get_boot_id(),
        "processes": [{"name": "run", "pid": pid}],
        "repo": "/fake/repo",
        "repo_name": "myrepo",
        "worktree": "/fake/wt",
        "branch": "feature/WR-8",
        "label": "WR-8",
        "target": "run",
        "started_at": time.time(),
        **extra,
    }
    return write_state_file(cache_dir, state_id, st)


class TestSweepExits:
    def test_crash_recorded(self, tmp_path, monkeypatch):
        """Process dies without stopping flag → exit entry written."""
        monkeypatch.setattr(wt_mod, "CACHE_DIR", tmp_path)
        proc = subprocess.Popen(
            ["true"],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        proc.wait()
        sf = _make_state(tmp_path, "repo__WR-8", proc.pid)

        _sweep_exits()

        st = _read_state(sf)
        assert st is not None
        exits = st.get("exits", [])
        assert len(exits) == 1
        assert exits[0]["pid"] == proc.pid
        assert exits[0]["name"] == "run"
        assert "exited_at" in exits[0]

    def test_clean_stop_not_recorded(self, tmp_path, monkeypatch):
        """stopping:True suppresses exit recording."""
        monkeypatch.setattr(wt_mod, "CACHE_DIR", tmp_path)
        proc = subprocess.Popen(
            ["true"],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        proc.wait()
        sf = _make_state(tmp_path, "repo__WR-8", proc.pid, stopping=True)

        _sweep_exits()

        st = _read_state(sf)
        assert st is not None
        assert st.get("exits", []) == []

    def test_stale_boot_skipped(self, tmp_path, monkeypatch):
        """State from a different boot is ignored by sweep."""
        monkeypatch.setattr(wt_mod, "CACHE_DIR", tmp_path)
        proc = subprocess.Popen(
            ["true"],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        proc.wait()
        # Write state with a different boot_id so _is_stale_boot returns True.
        st = {
            "boot_id": "0000000000",  # epoch 0 — never the current boot
            "processes": [{"name": "run", "pid": proc.pid}],
            "repo": "/fake/repo",
            "repo_name": "myrepo",
            "worktree": "/fake/wt",
            "branch": "feature/WR-8",
            "label": "WR-8",
            "target": "run",
            "started_at": time.time(),
        }
        sf = write_state_file(tmp_path, "repo__WR-8", st)
        # Patch _is_stale_boot to return True for any state so we don't depend on
        # the actual boot_id comparison logic being deterministic in CI.
        monkeypatch.setattr(wt_mod, "_is_stale_boot", lambda _st: True)

        _sweep_exits()

        st2 = _read_state(sf)
        assert st2 is not None
        assert st2.get("exits", []) == []

    def test_alive_process_not_recorded(self, tmp_path, monkeypatch):
        """Running process produces no exit entry."""
        monkeypatch.setattr(wt_mod, "CACHE_DIR", tmp_path)
        proc = subprocess.Popen(
            ["sleep", "60"],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            sf = _make_state(tmp_path, "repo__WR-8", proc.pid)
            _sweep_exits()
            st = _read_state(sf)
            assert st is not None
            assert st.get("exits", []) == []
        finally:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()

    def test_no_duplicate_exits_on_second_sweep(self, tmp_path, monkeypatch):
        """Second sweep does not append duplicate exit for already-recorded PID."""
        monkeypatch.setattr(wt_mod, "CACHE_DIR", tmp_path)
        proc = subprocess.Popen(
            ["true"],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        proc.wait()
        sf = _make_state(tmp_path, "repo__WR-8", proc.pid)

        _sweep_exits()
        _sweep_exits()

        st = _read_state(sf)
        assert len(st.get("exits", [])) == 1

    def test_log_tail_included(self, tmp_path, monkeypatch):
        """Exit entry includes last 10 log lines."""
        monkeypatch.setattr(wt_mod, "CACHE_DIR", tmp_path)
        proc = subprocess.Popen(
            ["true"],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        proc.wait()
        sf = _make_state(tmp_path, "repo__WR-8", proc.pid)
        log_path = tmp_path / "repo__WR-8.log"
        lines = [f"log line {i}" for i in range(15)]
        log_path.write_text("\n".join(lines) + "\n")

        _sweep_exits()

        st = _read_state(sf)
        tail = st["exits"][0]["log_tail"]
        assert len(tail) == 10
        assert tail == lines[-10:]


# ─────────────────────────────────────────────────────────────────────────────
# cmd_stop sets stopping flag
# ─────────────────────────────────────────────────────────────────────────────

class TestCmdStopStoppingFlag:
    def test_stopping_flag_written_before_sigterm(self, tmp_path, monkeypatch,
                                                   repo_with_worktrees):
        """stopping:True must be persisted before _state_terminate_all is called."""
        repo = repo_with_worktrees["repo"]
        monkeypatch.chdir(repo)
        monkeypatch.setattr(wt_mod, "CACHE_DIR", tmp_path)

        from wt import _state_id, _style_for, _resolve

        w = _resolve(repo, "SPLAT-10", _style_for(repo))
        sid = _state_id(repo, w)

        proc = subprocess.Popen(
            ["sleep", "60"],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            write_state_file(tmp_path, sid, {
                "boot_id": wt_mod._get_boot_id(),
                "processes": [{"name": "run", "pid": proc.pid}],
                "repo": str(repo),
                "repo_name": repo.name,
                "worktree": str(repo_with_worktrees["wt_10"]),
                "branch": "SPLAT-10",
                "label": "SPLAT-10",
                "target": "run",
                "started_at": time.time(),
            })

            # Capture every dict passed to _write_state_atomic.
            written: list[dict] = []
            original_write = wt_mod._write_state_atomic
            def capture_write(path, data):
                written.append(dict(data))
                return original_write(path, data)
            monkeypatch.setattr(wt_mod, "_write_state_atomic", capture_write)

            main(["stop", "SPLAT-10"])

            assert any(d.get("stopping") for d in written), (
                "stopping flag never written before termination"
            )
        finally:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            proc.wait()


# ─────────────────────────────────────────────────────────────────────────────
# cmd_status shows and clears exits
# ─────────────────────────────────────────────────────────────────────────────

class TestCmdStatusExits:
    def _write_crashed_state(self, cache_dir: Path, state_id: str,
                              exits: list[dict]) -> Path:
        st = {
            "boot_id": "irrelevant",
            "processes": [{"name": "run", "pid": 99999}],
            "repo": "/fake/repo",
            "repo_name": "myrepo",
            "worktree": "/fake/wt",
            "branch": "WR-9",
            "label": "WR-9",
            "target": "run",
            "started_at": time.time() - 60,
            "exits": exits,
        }
        return write_state_file(cache_dir, state_id, st)

    def test_exits_shown_in_status(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(wt_mod, "CACHE_DIR", tmp_path)
        self._write_crashed_state(tmp_path, "repo__WR-9", [
            {
                "name": "run",
                "pid": 99999,
                "exit_code": None,
                "exited_at": "2026-05-12T19:00:00+00:00",
                "log_tail": ["error: something went wrong"],
            }
        ])

        rc = main(["status"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "EXITED SINCE LAST CHECK" in out
        assert "myrepo/WR-9" in out
        assert "error: something went wrong" in out

    def test_exits_cleared_after_status(self, tmp_path, monkeypatch):
        monkeypatch.setattr(wt_mod, "CACHE_DIR", tmp_path)
        sf = self._write_crashed_state(tmp_path, "repo__WR-9", [
            {
                "name": "run",
                "pid": 99999,
                "exit_code": None,
                "exited_at": "2026-05-12T19:00:00+00:00",
                "log_tail": [],
            }
        ])

        main(["status"])
        # State file should be deleted (no alive processes, exits acknowledged).
        assert not sf.exists()

    def test_no_exits_no_section(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(wt_mod, "CACHE_DIR", tmp_path)
        # Empty cache dir — no exits, no running.
        rc = main(["status"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "EXITED SINCE LAST CHECK" not in out
