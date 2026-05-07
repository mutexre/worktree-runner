"""Shared fixtures for wt tests.

Provides a tmp git repo with branches and worktrees for testing resolution,
config loading, and process management.
"""

from __future__ import annotations

import json
import os
import subprocess
import textwrap
from pathlib import Path
from typing import Optional

import pytest
import yaml


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=check,
    )


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Bare-bones git repo with an initial commit."""
    repo = tmp_path / "myrepo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@test.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("init")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    return repo


@pytest.fixture
def repo_with_worktrees(git_repo: Path) -> dict:
    """Git repo with branches SPLAT-10, SPLAT-20, feature/login and worktrees for each."""
    repo = git_repo

    for branch in ("SPLAT-10", "SPLAT-20", "feature/login"):
        _git(repo, "branch", branch)

    wt_10 = repo.parent / "myrepo-splat-10"
    wt_20 = repo.parent / "myrepo-splat-20"
    wt_login = repo.parent / "myrepo-login"
    _git(repo, "worktree", "add", str(wt_10), "SPLAT-10")
    _git(repo, "worktree", "add", str(wt_20), "SPLAT-20")
    _git(repo, "worktree", "add", str(wt_login), "feature/login")

    return {
        "repo": repo,
        "wt_10": wt_10,
        "wt_20": wt_20,
        "wt_login": wt_login,
    }


def write_wt_yaml(repo: Path, data: dict) -> Path:
    cfg = repo / ".wt.yaml"
    cfg.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))
    return cfg


def write_state_file(cache_dir: Path, state_id: str, state: dict) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    p = cache_dir / f"{state_id}.json"
    p.write_text(json.dumps(state, indent=2))
    return p
