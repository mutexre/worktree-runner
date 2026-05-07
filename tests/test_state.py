"""Tests for state files and process tracking."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from wt import (
    _read_state,
    _group_alive,
    _terminate_group,
    _format_uptime,
    _state_pids,
    _state_any_alive,
    _state_terminate_all,
    _slug,
    _state_id,
    _repo_id,
    CACHE_DIR,
)
from tests.conftest import write_state_file


class TestSlug:
    def test_clean_string(self):
        assert _slug("SPLAT-10") == "SPLAT-10"

    def test_replaces_special_chars(self):
        assert _slug("feature/login") == "feature_login"

    def test_preserves_dots_and_dashes(self):
        assert _slug("v1.2-beta") == "v1.2-beta"


class TestRepoId:
    def test_deterministic(self, git_repo):
        assert _repo_id(git_repo) == _repo_id(git_repo)

    def test_contains_name(self, git_repo):
        rid = _repo_id(git_repo)
        assert rid.startswith("myrepo-")


class TestReadState:
    def test_returns_none_for_missing_file(self, tmp_path):
        assert _read_state(tmp_path / "nope.json") is None

    def test_returns_none_for_corrupt_json(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{broken")
        assert _read_state(bad) is None

    def test_reads_valid_state(self, tmp_path):
        p = tmp_path / "ok.json"
        data = {"pid": 1234, "target": "run"}
        p.write_text(json.dumps(data))
        assert _read_state(p) == data


class TestStatePids:
    def test_new_format_with_processes(self):
        st = {"processes": [{"name": "server", "pid": 100}, {"name": "worker", "pid": 200}]}
        pids = _state_pids(st)
        assert len(pids) == 2
        assert pids[0]["pid"] == 100

    def test_old_format_with_single_pid(self):
        st = {"pid": 42, "target": "run"}
        pids = _state_pids(st)
        assert len(pids) == 1
        assert pids[0]["pid"] == 42
        assert pids[0]["name"] == "run"

    def test_empty_state(self):
        assert _state_pids({}) == []


class TestFormatUptime:
    def test_zero(self):
        assert _format_uptime(0) == "00:00:00"

    def test_minutes_and_seconds(self):
        assert _format_uptime(102) == "00:01:42"

    def test_hours(self):
        assert _format_uptime(3661) == "01:01:01"


class TestGroupAlive:
    def test_alive_process(self):
        proc = subprocess.Popen(
            ["sleep", "60"],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            assert _group_alive(proc.pid)
        finally:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()

    def test_dead_process(self):
        proc = subprocess.Popen(
            ["true"],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        proc.wait()
        assert not _group_alive(proc.pid)


_SIGTERM_FRIENDLY = [
    sys.executable, "-c",
    "import signal, time; signal.signal(signal.SIGTERM, lambda *_: exit(0)); time.sleep(60)",
]


class TestTerminateGroup:
    def test_terminates_running_process(self):
        proc = subprocess.Popen(
            _SIGTERM_FRIENDLY,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        assert _group_alive(proc.pid)
        # Reap zombie in background so _group_alive detects death
        threading.Thread(target=proc.wait, daemon=True).start()
        ok = _terminate_group(proc.pid, timeout=3.0)
        assert ok
        assert not _group_alive(proc.pid)

    def test_already_dead(self):
        proc = subprocess.Popen(
            ["true"],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        proc.wait()
        ok = _terminate_group(proc.pid)
        assert ok


class TestStateAnyAlive:
    def test_with_live_process(self):
        proc = subprocess.Popen(
            ["sleep", "60"],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            st = {"processes": [{"name": "test", "pid": proc.pid}]}
            assert _state_any_alive(st)
        finally:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()

    def test_with_dead_process(self):
        proc = subprocess.Popen(
            ["true"],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        proc.wait()
        st = {"processes": [{"name": "test", "pid": proc.pid}]}
        assert not _state_any_alive(st)


class TestStateTerminateAll:
    def test_terminates_multiple(self):
        procs = []
        for _ in range(2):
            p = subprocess.Popen(
                _SIGTERM_FRIENDLY,
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            procs.append(p)
        for p in procs:
            threading.Thread(target=p.wait, daemon=True).start()
        st = {"processes": [{"name": f"p{i}", "pid": p.pid} for i, p in enumerate(procs)]}
        ok = _state_terminate_all(st)
        assert ok
        for p in procs:
            assert not _group_alive(p.pid)
