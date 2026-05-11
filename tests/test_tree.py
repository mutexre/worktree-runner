"""Tests for wt tree: _ps_group, _render_process_tree, cmd_tree."""

from __future__ import annotations

import json
import os
import signal
import subprocess
from pathlib import Path

import pytest

from wt import (
    _PsRow,
    _ps_group,
    _render_process_tree,
    main,
)
from tests.conftest import write_state_file


# ─────────────────────────────────────────────────────────────────────────────
# _ps_group
# ─────────────────────────────────────────────────────────────────────────────

class TestPsGroup:
    def test_returns_rows_for_live_process(self):
        proc = subprocess.Popen(
            ["sleep", "60"],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            rows = _ps_group(proc.pid)
            assert len(rows) >= 1
            pids = [r.pid for r in rows]
            assert proc.pid in pids
        finally:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()

    def test_row_fields_populated(self):
        proc = subprocess.Popen(
            ["sleep", "60"],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            rows = _ps_group(proc.pid)
            row = next(r for r in rows if r.pid == proc.pid)
            assert row.ppid > 0
            assert row.etime  # non-empty string
            assert "sleep" in row.cmd
        finally:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()

    def test_returns_empty_for_dead_process(self):
        proc = subprocess.Popen(
            ["true"],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        proc.wait()
        rows = _ps_group(proc.pid)
        assert rows == []


# ─────────────────────────────────────────────────────────────────────────────
# _render_process_tree
# ─────────────────────────────────────────────────────────────────────────────

class TestRenderProcessTree:
    def _rows(self, *specs):
        """Build _PsRow list from (pid, ppid, etime, cmd) tuples."""
        return [_PsRow(pid=p, ppid=pp, etime=e, cmd=c) for p, pp, e, c in specs]

    def test_empty_returns_empty_string(self):
        assert _render_process_tree([], 1) == ""

    def test_single_root_no_children(self):
        rows = self._rows((100, 1, "00:01:00", "bash run.sh"))
        out = _render_process_tree(rows, 100)
        assert "[100]" in out
        assert "bash run.sh" in out
        assert "└──" not in out

    def test_parent_child_connectors(self):
        rows = self._rows(
            (100, 1, "00:01:00", "bash run.sh"),
            (101, 100, "00:00:59", "python app.py"),
        )
        out = _render_process_tree(rows, 100)
        lines = out.splitlines()
        assert any("[100]" in l for l in lines)
        assert any("└──" in l and "[101]" in l for l in lines)

    def test_multiple_children_use_branch_connector(self):
        rows = self._rows(
            (100, 1, "00:01:00", "bash run.sh"),
            (101, 100, "00:00:59", "worker-a"),
            (102, 100, "00:00:58", "worker-b"),
        )
        out = _render_process_tree(rows, 100)
        assert "├──" in out
        assert "└──" in out

    def test_nested_depth(self):
        rows = self._rows(
            (100, 1, "00:01:00", "a"),
            (101, 100, "00:00:59", "b"),
            (102, 101, "00:00:58", "c"),
        )
        out = _render_process_tree(rows, 100)
        lines = out.splitlines()
        # c is deepest — must be indented more than b
        line_b = next(l for l in lines if "[101]" in l)
        line_c = next(l for l in lines if "[102]" in l)
        indent_b = len(line_b) - len(line_b.lstrip())
        indent_c = len(line_c) - len(line_c.lstrip())
        assert indent_c > indent_b

    def test_long_command_truncated(self):
        long_cmd = "x" * 200
        rows = self._rows((100, 1, "00:01:00", long_cmd))
        out = _render_process_tree(rows, 100)
        # truncated to 80 chars + ellipsis
        cmd_part = out.split("  ")[-1]
        assert len(cmd_part) <= 82  # 80 + "…" or nothing

    def test_all_pids_present(self):
        rows = self._rows(
            (100, 1, "00:01:00", "root"),
            (101, 100, "00:00:30", "child1"),
            (102, 100, "00:00:20", "child2"),
            (103, 101, "00:00:10", "grandchild"),
        )
        out = _render_process_tree(rows, 100)
        for pid in (100, 101, 102, 103):
            assert f"[{pid}]" in out


# ─────────────────────────────────────────────────────────────────────────────
# cmd_tree via main()
# ─────────────────────────────────────────────────────────────────────────────

class TestCmdTree:
    def test_no_state_file_returns_error(self, repo_with_worktrees, tmp_path, monkeypatch):
        monkeypatch.chdir(repo_with_worktrees["repo"])
        monkeypatch.setattr("wt.CACHE_DIR", tmp_path / "cache")
        rc = main(["tree", "SPLAT-10"])
        assert rc == 1

    def test_dead_group_prints_exited(self, repo_with_worktrees, tmp_path, monkeypatch, capsys):
        repo = repo_with_worktrees["repo"]
        monkeypatch.chdir(repo)
        cache = tmp_path / "cache"
        monkeypatch.setattr("wt.CACHE_DIR", cache)

        proc = subprocess.Popen(
            ["true"],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        proc.wait()

        import wt as wt_mod
        from wt import _repo_id, _slug
        from wt import Worktree
        from pathlib import Path as _Path

        # Build the state id the same way wt does
        style = wt_mod._style_for(repo)
        w = next(wt for wt in wt_mod._list_worktrees(repo, style) if wt.ticket == "SPLAT-10")
        sid = wt_mod._state_id(repo, w)
        write_state_file(cache, sid, {
            "processes": [{"name": "run", "pid": proc.pid}],
            "repo": str(repo),
            "repo_name": repo.name,
            "worktree": str(repo_with_worktrees["wt_10"]),
            "branch": "SPLAT-10",
            "label": "SPLAT-10",
            "target": "run",
        })

        rc = main(["tree", "SPLAT-10"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "exited" in out

    def test_live_group_renders_tree(self, repo_with_worktrees, tmp_path, monkeypatch, capsys):
        repo = repo_with_worktrees["repo"]
        monkeypatch.chdir(repo)
        cache = tmp_path / "cache"
        monkeypatch.setattr("wt.CACHE_DIR", cache)

        proc = subprocess.Popen(
            ["sleep", "60"],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            import wt as wt_mod
            style = wt_mod._style_for(repo)
            w = next(wt for wt in wt_mod._list_worktrees(repo, style) if wt.ticket == "SPLAT-10")
            sid = wt_mod._state_id(repo, w)
            write_state_file(cache, sid, {
                "processes": [{"name": "run", "pid": proc.pid}],
                "repo": str(repo),
                "repo_name": repo.name,
                "worktree": str(repo_with_worktrees["wt_10"]),
                "branch": "SPLAT-10",
                "label": "SPLAT-10",
                "target": "run",
            })

            rc = main(["tree", "SPLAT-10"])
            assert rc == 0
            out = capsys.readouterr().out
            assert str(proc.pid) in out
            assert "setsid" in out  # limitation note
        finally:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
