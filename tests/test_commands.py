"""Tests for wt init, wt install-skill, and CLI entry point."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from wt import main, cmd_install_skill


class TestWtInit:
    def test_creates_config(self, git_repo, monkeypatch):
        monkeypatch.chdir(git_repo)
        inputs = iter(["python app.py", "python -m pytest", "", ""])
        monkeypatch.setattr("builtins.input", lambda _: next(inputs))
        rc = main(["init"])
        assert rc == 0
        cfg = yaml.safe_load((git_repo / ".wt.yaml").read_text())
        assert cfg["targets"]["run"] == "python app.py"
        assert cfg["targets"]["test"] == "python -m pytest"
        assert "install" not in cfg["targets"]

    def test_aborts_on_existing_no_overwrite(self, git_repo, monkeypatch):
        monkeypatch.chdir(git_repo)
        (git_repo / ".wt.yaml").write_text("targets:\n  run: echo hi\n")
        monkeypatch.setattr("builtins.input", lambda _: "n")
        rc = main(["init"])
        assert rc == 0
        assert "echo hi" in (git_repo / ".wt.yaml").read_text()

    def test_no_targets_skips_write(self, git_repo, monkeypatch):
        monkeypatch.chdir(git_repo)
        monkeypatch.setattr("builtins.input", lambda _: "")
        rc = main(["init"])
        assert rc == 0
        assert not (git_repo / ".wt.yaml").exists()


class TestWtInstallSkill:
    def test_installs_symlink(self, tmp_path):
        src = Path(__file__).resolve().parent.parent / "src" / "wt" / "skills" / "init-wt"
        if not src.is_dir():
            pytest.skip("bundled skill not found")

        target = tmp_path / "skills" / "init-wt"
        args = type("Args", (), {"target": str(target), "force": False})()
        rc = cmd_install_skill(args)
        assert rc == 0
        assert target.is_symlink()
        assert target.resolve() == src.resolve()

    def test_refuses_overwrite_without_force(self, tmp_path):
        src = Path(__file__).resolve().parent.parent / "src" / "wt" / "skills" / "init-wt"
        if not src.is_dir():
            pytest.skip("bundled skill not found")

        target = tmp_path / "skills" / "init-wt"
        target.parent.mkdir(parents=True)
        target.mkdir()
        args = type("Args", (), {"target": str(target), "force": False})()
        rc = cmd_install_skill(args)
        assert rc == 1

    def test_force_replaces_existing(self, tmp_path):
        src = Path(__file__).resolve().parent.parent / "src" / "wt" / "skills" / "init-wt"
        if not src.is_dir():
            pytest.skip("bundled skill not found")

        target = tmp_path / "skills" / "init-wt"
        target.parent.mkdir(parents=True)
        target.mkdir()
        (target / "dummy").write_text("old")
        args = type("Args", (), {"target": str(target), "force": True})()
        rc = cmd_install_skill(args)
        assert rc == 0
        assert target.is_symlink()


class TestMainEntryPoint:
    def test_ls_runs_without_error(self, repo_with_worktrees, monkeypatch):
        monkeypatch.chdir(repo_with_worktrees["repo"])
        rc = main(["ls"])
        assert rc == 0

    def test_no_args_defaults_to_ls(self, repo_with_worktrees, monkeypatch):
        monkeypatch.chdir(repo_with_worktrees["repo"])
        rc = main([])
        assert rc == 0

    def test_path_prints_worktree(self, repo_with_worktrees, monkeypatch, capsys):
        monkeypatch.chdir(repo_with_worktrees["repo"])
        rc = main(["path", "SPLAT-10"])
        assert rc == 0
        out = capsys.readouterr().out.strip()
        assert "splat-10" in out.lower()

    def test_status_no_crash(self, tmp_path, monkeypatch):
        monkeypatch.setattr("wt.CACHE_DIR", tmp_path / "cache")
        rc = main(["status"])
        assert rc == 0

    def test_stop_all_no_crash(self, tmp_path, monkeypatch):
        monkeypatch.setattr("wt.CACHE_DIR", tmp_path / "cache")
        rc = main(["stop", "--all"])
        assert rc == 0

    def test_foreground_launch(self, repo_with_worktrees, monkeypatch):
        from tests.conftest import write_wt_yaml
        repo = repo_with_worktrees["repo"]
        write_wt_yaml(repo, {"targets": {"run": "echo hello"}})
        monkeypatch.chdir(repo)
        rc = main(["SPLAT-10"])
        assert rc == 0

    def test_foreground_rejects_group(self, repo_with_worktrees, monkeypatch, capsys):
        from tests.conftest import write_wt_yaml
        repo = repo_with_worktrees["repo"]
        write_wt_yaml(repo, {
            "targets": {"server": "echo s", "worker": "echo w"},
            "groups": {"run": ["server", "worker"]},
        })
        monkeypatch.chdir(repo)
        rc = main(["SPLAT-10"])
        assert rc == 1

    def test_specific_target(self, repo_with_worktrees, monkeypatch):
        from tests.conftest import write_wt_yaml
        repo = repo_with_worktrees["repo"]
        write_wt_yaml(repo, {"targets": {"test": "echo tests_pass"}})
        monkeypatch.chdir(repo)
        rc = main(["-t", "test", "SPLAT-10"])
        assert rc == 0

    def test_help_flag(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["--help"])
        assert exc.value.code == 0
