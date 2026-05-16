"""Tests for WR-23: `wt status` scoped to current repo; `-g/--global` for cross-repo."""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

import wt as wt_mod
from wt import cmd_status, main, _repo_id
from tests.conftest import _git, write_state_file


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_repo(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    repo.mkdir(parents=True)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@test")
    _git(repo, "config", "user.name", "T")
    (repo / "README").write_text(name)
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    return repo


def _write_running_state(cache: Path, repo: Path, label: str, pid: int = 99999) -> Path:
    rid = _repo_id(repo)
    state_id = f"{rid}__{label}"
    st = {
        "boot_id": wt_mod._get_boot_id(),
        "processes": [{"name": "run", "pid": pid, "start_time": "fake"}],
        "repo": str(repo),
        "repo_name": repo.name,
        "worktree": str(repo / "wt"),
        "branch": f"feature/{label}",
        "label": label,
        "target": "run",
        "started_at": time.time(),
    }
    return write_state_file(cache, state_id, st)


def _write_crashed_state(cache: Path, repo: Path, label: str, pid: int = 88888) -> Path:
    rid = _repo_id(repo)
    state_id = f"{rid}__{label}"
    st = {
        "boot_id": "irrelevant",
        "processes": [{"name": "run", "pid": pid}],
        "repo": str(repo),
        "repo_name": repo.name,
        "worktree": str(repo / "wt"),
        "branch": f"feature/{label}",
        "label": label,
        "target": "run",
        "started_at": time.time() - 60,
        "exits": [{
            "name": "run",
            "pid": pid,
            "exit_code": None,
            "exited_at": "2026-05-12T19:00:00+00:00",
            "log_tail": ["something crashed"],
        }],
    }
    return write_state_file(cache, state_id, st)


# ---------------------------------------------------------------------------
# Two-repo scoping
# ---------------------------------------------------------------------------

class TestStatusScope:
    def test_scoped_shows_only_current_repo(self, tmp_path, monkeypatch, capsys):
        """wt status (no flag) from repo A → only A's entries visible."""
        cache = tmp_path / "cache"
        monkeypatch.setattr(wt_mod, "CACHE_DIR", cache)

        repo_a = _make_repo(tmp_path, "alpha")
        repo_b = _make_repo(tmp_path, "beta")

        _write_running_state(cache, repo_a, "TICK-1", pid=11111)
        _write_running_state(cache, repo_b, "TICK-2", pid=22222)

        monkeypatch.chdir(repo_a)
        with mock.patch.object(wt_mod, "_group_alive", return_value=True), \
             mock.patch.object(wt_mod, "_get_process_start_time", return_value="fake"):
            rc = main(["status"])

        assert rc == 0
        out = capsys.readouterr().out
        assert "alpha" in out
        assert "TICK-1" in out
        assert "beta" not in out
        assert "TICK-2" not in out

    def test_global_shows_all_repos(self, tmp_path, monkeypatch, capsys):
        """wt status -g → both A's and B's entries visible."""
        cache = tmp_path / "cache"
        monkeypatch.setattr(wt_mod, "CACHE_DIR", cache)

        repo_a = _make_repo(tmp_path, "alpha")
        repo_b = _make_repo(tmp_path, "beta")

        _write_running_state(cache, repo_a, "TICK-1", pid=11111)
        _write_running_state(cache, repo_b, "TICK-2", pid=22222)

        monkeypatch.chdir(repo_a)
        with mock.patch.object(wt_mod, "_group_alive", return_value=True), \
             mock.patch.object(wt_mod, "_get_process_start_time", return_value="fake"):
            rc = main(["status", "-g"])

        assert rc == 0
        out = capsys.readouterr().out
        assert "alpha" in out
        assert "TICK-1" in out
        assert "beta" in out
        assert "TICK-2" in out

    def test_global_long_flag(self, tmp_path, monkeypatch, capsys):
        """wt status --global works the same as -g."""
        cache = tmp_path / "cache"
        monkeypatch.setattr(wt_mod, "CACHE_DIR", cache)

        repo_a = _make_repo(tmp_path, "alpha")
        _write_running_state(cache, repo_a, "TICK-1", pid=11111)

        monkeypatch.chdir(repo_a)
        with mock.patch.object(wt_mod, "_group_alive", return_value=True), \
             mock.patch.object(wt_mod, "_get_process_start_time", return_value="fake"):
            rc = main(["status", "--global"])

        assert rc == 0
        assert "TICK-1" in capsys.readouterr().out

    def test_scoped_empty_when_other_repo_has_entries(self, tmp_path, monkeypatch, capsys):
        """wt status in repo A when only repo B has state → 'no detached apps running'."""
        cache = tmp_path / "cache"
        monkeypatch.setattr(wt_mod, "CACHE_DIR", cache)

        repo_a = _make_repo(tmp_path, "alpha")
        repo_b = _make_repo(tmp_path, "beta")

        _write_running_state(cache, repo_b, "TICK-2", pid=22222)

        monkeypatch.chdir(repo_a)
        with mock.patch.object(wt_mod, "_group_alive", return_value=True), \
             mock.patch.object(wt_mod, "_get_process_start_time", return_value="fake"):
            rc = main(["status"])

        assert rc == 0
        assert "no detached apps running" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Outside a git repo
# ---------------------------------------------------------------------------

class TestStatusOutsideRepo:
    def test_no_flag_errors_outside_repo(self, tmp_path, monkeypatch, capsys):
        """wt status outside a git repo → error (needs _current_repo)."""
        cache = tmp_path / "cache"
        cache.mkdir()
        monkeypatch.setattr(wt_mod, "CACHE_DIR", cache)
        monkeypatch.chdir(tmp_path)

        with pytest.raises(SystemExit) as exc:
            main(["status"])
        assert exc.value.code == 1
        assert "not inside a git repository" in capsys.readouterr().err

    def test_global_works_outside_repo(self, tmp_path, monkeypatch, capsys):
        """wt status -g outside a git repo → lists everything."""
        cache = tmp_path / "cache"
        monkeypatch.setattr(wt_mod, "CACHE_DIR", cache)
        monkeypatch.chdir(tmp_path)

        repo_a = _make_repo(tmp_path / "repos", "alpha")
        _write_running_state(cache, repo_a, "TICK-1", pid=11111)

        with mock.patch.object(wt_mod, "_group_alive", return_value=True), \
             mock.patch.object(wt_mod, "_get_process_start_time", return_value="fake"):
            rc = main(["status", "-g"])

        assert rc == 0
        out = capsys.readouterr().out
        assert "TICK-1" in out


# ---------------------------------------------------------------------------
# Crash-exit scoping
# ---------------------------------------------------------------------------

class TestStatusExitsScoped:
    def test_scoped_exits_only_current_repo(self, tmp_path, monkeypatch, capsys):
        """Crash exits from repo B not shown in scoped status from repo A."""
        cache = tmp_path / "cache"
        monkeypatch.setattr(wt_mod, "CACHE_DIR", cache)

        repo_a = _make_repo(tmp_path, "alpha")
        repo_b = _make_repo(tmp_path, "beta")

        _write_crashed_state(cache, repo_a, "CRASH-A", pid=33333)
        _write_crashed_state(cache, repo_b, "CRASH-B", pid=44444)

        monkeypatch.chdir(repo_a)
        with mock.patch.object(wt_mod, "_group_alive", return_value=False):
            rc = main(["status"])

        assert rc == 0
        out = capsys.readouterr().out
        assert "CRASH-A" in out
        assert "CRASH-B" not in out

    def test_global_exits_all_repos(self, tmp_path, monkeypatch, capsys):
        """Crash exits from both repos shown with -g."""
        cache = tmp_path / "cache"
        monkeypatch.setattr(wt_mod, "CACHE_DIR", cache)

        repo_a = _make_repo(tmp_path, "alpha")
        repo_b = _make_repo(tmp_path, "beta")

        _write_crashed_state(cache, repo_a, "CRASH-A", pid=33333)
        _write_crashed_state(cache, repo_b, "CRASH-B", pid=44444)

        monkeypatch.chdir(repo_a)
        with mock.patch.object(wt_mod, "_group_alive", return_value=False):
            rc = main(["status", "-g"])

        assert rc == 0
        out = capsys.readouterr().out
        assert "CRASH-A" in out
        assert "CRASH-B" in out
        assert "EXITED SINCE LAST CHECK" in out

    def test_scoped_exits_cleared(self, tmp_path, monkeypatch):
        """Exit acknowledgement works in scoped mode (state file deleted)."""
        cache = tmp_path / "cache"
        monkeypatch.setattr(wt_mod, "CACHE_DIR", cache)

        repo_a = _make_repo(tmp_path, "alpha")
        sf = _write_crashed_state(cache, repo_a, "CRASH-A", pid=33333)

        monkeypatch.chdir(repo_a)
        with mock.patch.object(wt_mod, "_group_alive", return_value=False):
            main(["status"])

        assert not sf.exists()


# ---------------------------------------------------------------------------
# wt stop -g / --global
# ---------------------------------------------------------------------------

class TestStopGlobal:
    def test_stop_global_empty_cache(self, tmp_path, monkeypatch, capsys):
        """wt stop -g with empty cache → 'nothing running', rc=0."""
        cache = tmp_path / "cache"
        cache.mkdir()
        monkeypatch.setattr(wt_mod, "CACHE_DIR", cache)
        rc = main(["stop", "-g"])
        assert rc == 0
        assert "nothing running" in capsys.readouterr().err

    def test_stop_global_two_repos(self, tmp_path, monkeypatch, capsys):
        """wt stop -g terminates groups from both repos."""
        cache = tmp_path / "cache"
        monkeypatch.setattr(wt_mod, "CACHE_DIR", cache)

        repo_a = _make_repo(tmp_path, "alpha")
        repo_b = _make_repo(tmp_path, "beta")

        sf_a = _write_running_state(cache, repo_a, "TICK-1", pid=11111)
        sf_b = _write_running_state(cache, repo_b, "TICK-2", pid=22222)

        with mock.patch.object(wt_mod, "_group_alive", return_value=True), \
             mock.patch.object(wt_mod, "_get_process_start_time", return_value="fake"), \
             mock.patch.object(wt_mod, "_terminate_group", return_value=True):
            rc = main(["stop", "-g"])

        assert rc == 0
        assert not sf_a.exists()
        assert not sf_b.exists()

    def test_stop_no_args_errors(self, tmp_path, monkeypatch, capsys):
        """wt stop with no args and no -g → error."""
        cache = tmp_path / "cache"
        cache.mkdir()
        monkeypatch.setattr(wt_mod, "CACHE_DIR", cache)
        monkeypatch.chdir(tmp_path)
        rc = main(["stop"])
        assert rc == 1
        assert "-g/--global" in capsys.readouterr().err
