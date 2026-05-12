"""Tests for wt init, wt install-skill, and CLI entry point."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from tests.conftest import _git, write_wt_yaml
from wt import main, cmd_install_skill, cmd_shell_init


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
        import wt as wt_mod
        src = Path(wt_mod.__file__).resolve().parent / "skills" / "init-wt"
        if not src.is_dir():
            pytest.skip("bundled skill not found")

        target = tmp_path / "skills" / "init-wt"
        args = type("Args", (), {"target": str(target), "force": False})()
        rc = cmd_install_skill(args)
        assert rc == 0
        assert target.is_symlink()
        assert target.resolve() == src.resolve()

    def test_refuses_overwrite_without_force(self, tmp_path):
        import wt as wt_mod
        src = Path(wt_mod.__file__).resolve().parent / "skills" / "init-wt"
        if not src.is_dir():
            pytest.skip("bundled skill not found")

        target = tmp_path / "skills" / "init-wt"
        target.parent.mkdir(parents=True)
        target.mkdir()
        args = type("Args", (), {"target": str(target), "force": False})()
        rc = cmd_install_skill(args)
        assert rc == 1

    def test_force_replaces_existing(self, tmp_path):
        import wt as wt_mod
        src = Path(wt_mod.__file__).resolve().parent / "skills" / "init-wt"
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

    def test_cd_prints_same_path_as_path_non_tty(
        self, repo_with_worktrees, monkeypatch, capsys,
    ):
        monkeypatch.chdir(repo_with_worktrees["repo"])
        monkeypatch.setattr("sys.stderr.isatty", lambda: False)
        rc_path = main(["path", "SPLAT-10"])
        out_path = capsys.readouterr().out.strip()
        rc_cd = main(["cd", "SPLAT-10"])
        out_cd = capsys.readouterr().out.strip()
        assert rc_path == 0 and rc_cd == 0
        assert out_path == out_cd

    def test_cd_tty_prints_tip(self, repo_with_worktrees, monkeypatch, capsys):
        monkeypatch.chdir(repo_with_worktrees["repo"])
        monkeypatch.setattr("sys.stderr.isatty", lambda: True)
        rc = main(["cd", "SPLAT-10"])
        assert rc == 0
        err = capsys.readouterr().err
        assert "shell-init" in err

    def test_shell_init_zsh_prints_wrapped_function(self, capsys):
        args = type(
            "Args", (),
            {"shell": "zsh", "install": False, "uninstall": False, "force": False},
        )()
        rc = cmd_shell_init(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert 'command wt path "$2"' in out
        assert "wt() {" in out

    def test_shell_init_bash_install_rejected(self, capsys):
        args = type(
            "Args", (),
            {"shell": "bash", "install": True, "uninstall": False, "force": False},
        )()
        rc = cmd_shell_init(args)
        assert rc == 1
        assert "not supported" in capsys.readouterr().err

    def test_shell_init_fish_install_write_unrelated_errors(
        self, tmp_path, monkeypatch, capsys,
    ):
        conf = tmp_path / "cfg"
        monkeypatch.setenv("XDG_CONFIG_HOME", str(conf))
        drop = conf / "fish" / "conf.d"
        drop.mkdir(parents=True)
        target = drop / "wt.fish"
        target.write_text("# user\n")
        args = type(
            "Args", (),
            {"shell": "fish", "install": True, "uninstall": False, "force": False},
        )()
        rc = cmd_shell_init(args)
        assert rc == 1
        assert target.read_text() == "# user\n"

    def test_shell_init_fish_install_force_replaces(
        self, tmp_path, monkeypatch, capsys,
    ):
        conf = tmp_path / "cfg"
        monkeypatch.setenv("XDG_CONFIG_HOME", str(conf))
        drop = conf / "fish" / "conf.d"
        drop.mkdir(parents=True)
        target = drop / "wt.fish"
        target.write_text("# user\n")
        args = type(
            "Args", (),
            {"shell": "fish", "install": True, "uninstall": False, "force": True},
        )()
        rc = cmd_shell_init(args)
        assert rc == 0
        text = target.read_text()
        assert "# BEGIN wt shell integration" in text
        assert "function wt" in text

    def test_shell_init_fish_uninstall_managed(
        self, tmp_path, monkeypatch, capsys,
    ):
        conf = tmp_path / "cfg"
        monkeypatch.setenv("XDG_CONFIG_HOME", str(conf))
        args = type(
            "Args", (),
            {"shell": "fish", "install": True, "uninstall": False, "force": False},
        )()
        assert cmd_shell_init(args) == 0
        target = conf / "fish" / "conf.d" / "wt.fish"
        assert target.is_file()
        args2 = type(
            "Args", (),
            {"shell": "fish", "install": False, "uninstall": True, "force": False},
        )()
        assert cmd_shell_init(args2) == 0
        assert not target.exists()

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

    def test_config_always_from_main_worktree(self, repo_with_worktrees, monkeypatch):
        """Linked checkout's .wt.yaml must not override the main tree's config (WR-11)."""
        from wt import _current_repo, _load_config, _resolve_target

        repo = repo_with_worktrees["repo"]
        wt_10 = repo_with_worktrees["wt_10"]
        write_wt_yaml(repo, {"targets": {"run": "echo MAIN_CFG"}})
        write_wt_yaml(wt_10, {"targets": {"run": "echo LINKED_CFG"}})
        monkeypatch.chdir(wt_10)
        assert _current_repo().resolve() == repo.resolve()
        cfg = _load_config(_current_repo())
        spec = _resolve_target(cfg, "run")
        assert spec.commands[0][1] == "echo MAIN_CFG"

    def test_bare_repo_rejected(self, tmp_path, monkeypatch, capsys):
        bare = tmp_path / "demo.git"
        subprocess.run(
            ["git", "init", "--bare", "-b", "main", str(bare)],
            check=True, capture_output=True,
        )
        monkeypatch.chdir(bare)
        with pytest.raises(SystemExit) as ei:
            main([])
        assert ei.value.code == 1
        err = capsys.readouterr().err.lower()
        assert "bare" in err

    def test_submodule_checkout_rejected(self, tmp_path, monkeypatch, capsys):
        super_repo = tmp_path / "super"
        child = tmp_path / "child"
        super_repo.mkdir()
        child.mkdir()
        _git(super_repo, "init", "-b", "main")
        _git(super_repo, "config", "user.email", "t@test")
        _git(super_repo, "config", "user.name", "T")
        (super_repo / "README").write_text("super")
        _git(super_repo, "add", ".")
        _git(super_repo, "commit", "-m", "init super")

        _git(child, "init", "-b", "main")
        _git(child, "config", "user.email", "t@test")
        _git(child, "config", "user.name", "T")
        (child / "f").write_text("c")
        _git(child, "add", ".")
        _git(child, "commit", "-m", "init child")

        subprocess.run(
            [
                "git", "-C", str(super_repo), "-c", "protocol.file.allow=always",
                "submodule", "add", str(child.resolve()), "submod",
            ],
            check=True,
            capture_output=True,
        )
        _git(super_repo, "commit", "-m", "add submodule")

        sub_path = super_repo / "submod"
        assert sub_path.is_dir()
        monkeypatch.chdir(sub_path)
        with pytest.raises(SystemExit) as ei:
            main([])
        assert ei.value.code == 1
        assert "submodule" in capsys.readouterr().err.lower()

    def test_help_flag(self, capsys):
        with pytest.raises(SystemExit) as exc:
            main(["--help"])
        assert exc.value.code == 0
