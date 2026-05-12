"""Tests for `wt add <ticket-or-branch>`."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from wt import (
    _STYLE_LINEAR,
    _branch_slug,
    _resolve_remote_branch,
    _ticket_candidates,
    main,
)


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, text=True, check=check,
    )


@pytest.fixture
def repo_with_remote(tmp_path: Path) -> dict:
    """Local clone of a bare 'remote' repo with branches WR-99, WR-100, cursor-foo, devin-bar."""
    remote = tmp_path / "remote"
    remote.mkdir()
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(remote)],
        check=True, capture_output=True,
    )

    local = tmp_path / "local"
    subprocess.run(
        ["git", "clone", "-q", str(remote), str(local)],
        check=True, capture_output=True,
    )
    _git(local, "config", "user.email", "t@t")
    _git(local, "config", "user.name", "Test")
    (local / "README.md").write_text("init")
    _git(local, "add", ".")
    _git(local, "commit", "-q", "-m", "initial")
    _git(local, "push", "-q", "origin", "main")

    for branch in ("WR-99", "WR-100", "cursor-foo", "devin-bar"):
        _git(local, "checkout", "-q", "-b", branch, "main")
        (local / f"x_{branch}").write_text(branch)
        _git(local, "add", f"x_{branch}")
        _git(local, "commit", "-q", "-m", branch)
        _git(local, "push", "-q", "origin", branch)
        _git(local, "checkout", "-q", "main")
        _git(local, "branch", "-D", "-q", branch)

    return {"remote": remote, "local": local, "tmp": tmp_path}


# ─────────────────────────────────────────────────────────────────────────────
# _branch_slug
# ─────────────────────────────────────────────────────────────────────────────

class TestBranchSlug:
    def test_lowercases(self):
        assert _branch_slug("WR-12") == "wr-12"

    def test_replaces_non_alnum(self):
        assert _branch_slug("cursor/foo") == "cursor-foo"

    def test_collapses_dashes(self):
        assert _branch_slug("a//b__c") == "a-b-c"

    def test_strips_edge_dashes(self):
        assert _branch_slug("/foo/") == "foo"


# ─────────────────────────────────────────────────────────────────────────────
# _ticket_candidates (shared with _resolve)
# ─────────────────────────────────────────────────────────────────────────────

class TestTicketCandidates:
    def test_includes_raw_and_uppercase(self, tmp_path):
        cands = _ticket_candidates("wr-12", tmp_path, _STYLE_LINEAR)
        assert "wr-12" in cands
        assert "WR-12" in cands

    def test_digit_with_inferred_prefix(self, repo_with_remote):
        local = repo_with_remote["local"]
        _git(local, "branch", "WR-1")
        cands = _ticket_candidates("99", local, _STYLE_LINEAR)
        assert "WR-99" in cands

    def test_non_digit_no_expansion(self, tmp_path):
        cands = _ticket_candidates("foo", tmp_path, _STYLE_LINEAR)
        assert cands == {"foo", "FOO"}


# ─────────────────────────────────────────────────────────────────────────────
# _resolve_remote_branch (pure logic, no network)
# ─────────────────────────────────────────────────────────────────────────────

class TestResolveRemoteBranch:
    def test_exact_match(self, tmp_path):
        result = _resolve_remote_branch(
            "WR-99", {"WR-99": "sha", "main": "sha2"}, tmp_path, _STYLE_LINEAR,
        )
        assert result == "WR-99"

    def test_origin_prefix_stripped(self, tmp_path):
        result = _resolve_remote_branch(
            "origin/cursor-foo", {"cursor-foo": "sha"}, tmp_path, _STYLE_LINEAR,
        )
        assert result == "cursor-foo"

    def test_origin_prefix_unknown_branch_dies(self, tmp_path):
        with pytest.raises(SystemExit):
            _resolve_remote_branch(
                "origin/nope", {"cursor-foo": "sha"}, tmp_path, _STYLE_LINEAR,
            )

    def test_ambiguous_ticket_match_dies(self, tmp_path, capsys):
        with pytest.raises(SystemExit):
            _resolve_remote_branch(
                "WR", {"WR-99": "a", "WR-100": "b"}, tmp_path, _STYLE_LINEAR,
            )
        captured = capsys.readouterr()
        assert "matches multiple remote branches" in captured.err

    def test_no_match_dies(self, tmp_path, capsys):
        with pytest.raises(SystemExit):
            _resolve_remote_branch(
                "doesnotexist", {"WR-99": "a"}, tmp_path, _STYLE_LINEAR,
            )
        captured = capsys.readouterr()
        assert "no remote branch matches" in captured.err

    def test_fuzzy_substring_fallback(self, tmp_path):
        result = _resolve_remote_branch(
            "fo", {"cursor-foo": "sha", "main": "sha2"}, tmp_path, _STYLE_LINEAR,
        )
        assert result == "cursor-foo"


# ─────────────────────────────────────────────────────────────────────────────
# cmd_add via main() — integration with real git
# ─────────────────────────────────────────────────────────────────────────────

class TestCmdAdd:
    def test_exact_branch_creates_tracked_worktree(self, repo_with_remote, monkeypatch, capsys):
        local = repo_with_remote["local"]
        monkeypatch.chdir(local)
        rc = main(["add", "cursor-foo"])
        assert rc == 0
        wt = local.parent / "local-cursor-foo"
        assert wt.exists()
        upstream = _git(wt, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
        assert upstream.stdout.strip() == "origin/cursor-foo"

    def test_origin_prefix_stripped(self, repo_with_remote, monkeypatch):
        local = repo_with_remote["local"]
        monkeypatch.chdir(local)
        rc = main(["add", "origin/devin-bar"])
        assert rc == 0
        assert (local.parent / "local-devin-bar").exists()

    def test_ticket_style_expansion(self, repo_with_remote, monkeypatch, capsys):
        local = repo_with_remote["local"]
        _git(local, "branch", "WR-1")
        monkeypatch.chdir(local)
        rc = main(["add", "99"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "WR-99" in out or (local.parent / "local-wr-99").exists()

    def test_idempotent_re_add(self, repo_with_remote, monkeypatch, capsys):
        local = repo_with_remote["local"]
        monkeypatch.chdir(local)
        assert main(["add", "WR-99"]) == 0
        capsys.readouterr()
        rc = main(["add", "WR-99"])
        assert rc == 0
        out = capsys.readouterr()
        assert "already added" in out.err or "already added" in out.out

    def test_stale_local_branch_errors(self, repo_with_remote, monkeypatch, capsys):
        local = repo_with_remote["local"]
        monkeypatch.chdir(local)
        _git(local, "branch", "cursor-foo")
        with pytest.raises(SystemExit):
            main(["add", "cursor-foo"])
        captured = capsys.readouterr()
        assert "local branch 'cursor-foo' already exists" in captured.err

    def test_path_override(self, repo_with_remote, monkeypatch):
        local = repo_with_remote["local"]
        custom = repo_with_remote["tmp"] / "custom-dir"
        monkeypatch.chdir(local)
        rc = main(["add", "WR-100", "--path", str(custom)])
        assert rc == 0
        assert custom.exists()
        upstream = _git(custom, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
        assert upstream.stdout.strip() == "origin/WR-100"

    def test_ambiguous_remote_match_errors(self, repo_with_remote, monkeypatch, capsys):
        local = repo_with_remote["local"]
        monkeypatch.chdir(local)
        with pytest.raises(SystemExit):
            main(["add", "WR"])
        captured = capsys.readouterr()
        assert "matches multiple remote branches" in captured.err

    def test_fetch_failure_errors_clearly(self, repo_with_remote, monkeypatch, capsys):
        local = repo_with_remote["local"]
        (repo_with_remote["remote"]).rename(repo_with_remote["tmp"] / "remote.gone")
        monkeypatch.chdir(local)
        with pytest.raises(SystemExit):
            main(["add", "anything"])
        captured = capsys.readouterr()
        assert "fetch failed" in captured.err
