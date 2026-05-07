"""Tests for worktree discovery and ticket resolution."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from wt import (
    _STYLE_LINEAR,
    _list_worktrees,
    _resolve,
    _infer_ticket_prefix,
    _make_worktree,
)


class TestListWorktrees:
    def test_lists_main_and_linked(self, repo_with_worktrees):
        repo = repo_with_worktrees["repo"]
        wts = _list_worktrees(repo, _STYLE_LINEAR)
        labels = {w.label for w in wts}
        assert "SPLAT-10" in labels
        assert "SPLAT-20" in labels
        assert labels & {"feature_login", "feature/login"} or any("login" in l for l in labels)

    def test_ticket_extracted(self, repo_with_worktrees):
        repo = repo_with_worktrees["repo"]
        wts = _list_worktrees(repo, _STYLE_LINEAR)
        by_ticket = {w.ticket: w for w in wts if w.ticket}
        assert "SPLAT-10" in by_ticket
        assert "SPLAT-20" in by_ticket

    def test_non_ticket_branch_has_no_ticket(self, repo_with_worktrees):
        repo = repo_with_worktrees["repo"]
        wts = _list_worktrees(repo, _STYLE_LINEAR)
        login_wt = [w for w in wts if "login" in w.branch]
        assert len(login_wt) == 1
        assert login_wt[0].ticket is None


class TestResolve:
    def test_exact_ticket(self, repo_with_worktrees):
        repo = repo_with_worktrees["repo"]
        w = _resolve(repo, "SPLAT-10", _STYLE_LINEAR)
        assert w.ticket == "SPLAT-10"
        assert w.path == repo_with_worktrees["wt_10"]

    def test_exact_ticket_case_insensitive(self, repo_with_worktrees):
        repo = repo_with_worktrees["repo"]
        w = _resolve(repo, "splat-10", _STYLE_LINEAR)
        assert w.ticket == "SPLAT-10"

    def test_digit_expansion(self, repo_with_worktrees):
        repo = repo_with_worktrees["repo"]
        w = _resolve(repo, "10", _STYLE_LINEAR)
        assert w.ticket == "SPLAT-10"

    def test_exact_branch_name(self, repo_with_worktrees):
        repo = repo_with_worktrees["repo"]
        w = _resolve(repo, "feature/login", _STYLE_LINEAR)
        assert "login" in w.branch

    def test_fuzzy_substring(self, repo_with_worktrees):
        repo = repo_with_worktrees["repo"]
        w = _resolve(repo, "login", _STYLE_LINEAR)
        assert "login" in w.branch

    def test_no_match_raises(self, repo_with_worktrees):
        repo = repo_with_worktrees["repo"]
        with pytest.raises(SystemExit):
            _resolve(repo, "nonexistent-xyz", _STYLE_LINEAR)

    def test_ambiguous_raises(self, repo_with_worktrees):
        """Both SPLAT-10 and SPLAT-20 contain 'SPLAT', so fuzzy match is ambiguous."""
        repo = repo_with_worktrees["repo"]
        with pytest.raises(SystemExit):
            _resolve(repo, "SPLAT", _STYLE_LINEAR)

    def test_directory_name_match(self, repo_with_worktrees):
        repo = repo_with_worktrees["repo"]
        w = _resolve(repo, "myrepo-splat-10", _STYLE_LINEAR)
        assert w.ticket == "SPLAT-10"


class TestInferTicketPrefix:
    def test_infers_from_branches(self, repo_with_worktrees):
        repo = repo_with_worktrees["repo"]
        prefix = _infer_ticket_prefix(repo, _STYLE_LINEAR)
        assert prefix == "SPLAT"

    def test_no_prefix_for_github_style(self, repo_with_worktrees):
        from wt import _STYLE_GITHUB
        repo = repo_with_worktrees["repo"]
        prefix = _infer_ticket_prefix(repo, _STYLE_GITHUB)
        assert prefix is None
