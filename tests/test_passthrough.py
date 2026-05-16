"""Tests for WR-24: pass-through `--` syntax and no-config helpful error."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.conftest import _git, write_wt_yaml
from wt import main, CACHE_DIR


class TestPassthroughForeground:
    def test_runs_in_worktree_dir(self, repo_with_worktrees, monkeypatch, tmp_path):
        monkeypatch.chdir(repo_with_worktrees["repo"])
        marker = tmp_path / "marker.txt"
        rc = main(["SPLAT-10", "--", "pwd", ">", str(marker)])
        assert rc == 0
        cwd_used = marker.read_text().strip()
        assert cwd_used == str(repo_with_worktrees["wt_10"])

    def test_ignores_config(self, repo_with_worktrees, monkeypatch, tmp_path):
        repo = repo_with_worktrees["repo"]
        write_wt_yaml(repo, {"targets": {"run": "echo FROM_CONFIG"}})
        monkeypatch.chdir(repo)
        marker = tmp_path / "marker.txt"
        rc = main(["SPLAT-10", "--", "echo", "FROM_PASSTHROUGH", ">", str(marker)])
        assert rc == 0
        assert "FROM_PASSTHROUGH" in marker.read_text()


class TestPassthroughDetached:
    def test_spawns_and_tracks(self, repo_with_worktrees, monkeypatch, tmp_path):
        monkeypatch.chdir(repo_with_worktrees["repo"])
        cache = tmp_path / "cache"
        monkeypatch.setattr("wt.CACHE_DIR", cache)
        rc = main(["-d", "SPLAT-10", "--", "sleep", "60"])
        assert rc == 0

        state_files = list(cache.glob("*.json"))
        assert len(state_files) == 1
        state = json.loads(state_files[0].read_text())
        assert state["processes"][0]["name"] == "sleep"
        assert "pid" in state["processes"][0]

        rc = main(["stop", "SPLAT-10"])
        assert rc == 0


class TestMutex:
    def test_fg_t_and_passthrough_exit_2(self, repo_with_worktrees, monkeypatch, capsys):
        monkeypatch.chdir(repo_with_worktrees["repo"])
        rc = main(["-t", "test", "SPLAT-10", "--", "echo", "hi"])
        assert rc == 2
        assert "cannot combine" in capsys.readouterr().err

    def test_detached_t_and_passthrough_exit_2(self, repo_with_worktrees, monkeypatch, capsys):
        monkeypatch.chdir(repo_with_worktrees["repo"])
        rc = main(["-d", "-t", "test", "SPLAT-10", "--", "echo", "hi"])
        assert rc == 2
        assert "cannot combine" in capsys.readouterr().err


class TestBareInvocationNoConfig:
    def test_no_config_no_makefile_helpful_error(self, git_repo, monkeypatch, capsys):
        monkeypatch.chdir(git_repo)
        with pytest.raises(SystemExit) as ei:
            main(["main"])
        assert ei.value.code == 2
        err = capsys.readouterr().err
        assert "wt init" in err
        assert "--" in err

    def test_no_config_no_makefile_detached(self, git_repo, monkeypatch, capsys):
        monkeypatch.chdir(git_repo)
        with pytest.raises(SystemExit) as ei:
            main(["-d", "main"])
        assert ei.value.code == 2
        err = capsys.readouterr().err
        assert "wt init" in err

    def test_no_config_with_makefile_falls_back(self, git_repo, monkeypatch, tmp_path):
        marker = tmp_path / "marker.txt"
        (git_repo / "Makefile").write_text(
            f"run:\n\t@echo makefile_ok > {marker}\n"
        )
        monkeypatch.chdir(git_repo)
        rc = main(["main"])
        assert rc == 0
        assert "makefile_ok" in marker.read_text()

    def test_with_config_unchanged(self, repo_with_worktrees, monkeypatch, tmp_path):
        repo = repo_with_worktrees["repo"]
        marker = tmp_path / "marker.txt"
        write_wt_yaml(repo, {"targets": {"run": f"echo config_ok > {marker}"}})
        monkeypatch.chdir(repo)
        rc = main(["SPLAT-10"])
        assert rc == 0
        assert "config_ok" in marker.read_text()
