"""Tests for PID-reuse guard (WR-12): boot_id + process start_time verification."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import types
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

import wt


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def cache_dir(tmp_path):
    """Redirect wt.CACHE_DIR to a temporary directory."""
    d = tmp_path / "wt-cache"
    d.mkdir()
    with mock.patch.object(wt, "CACHE_DIR", d):
        yield d


def _write_state(cache: Path, name: str, state: dict) -> Path:
    p = cache / f"{name}.json"
    p.write_text(json.dumps(state, indent=2))
    return p


BOOT_A = "1111111111"
BOOT_B = "2222222222"
LSTART = "Thu May  7 00:55:20 2026"
LSTART_OTHER = "Thu May  7 01:30:00 2026"


def _make_state(boot_id=BOOT_A, pid=99999, start_time=LSTART, **kw):
    base = {
        "boot_id": boot_id,
        "processes": [{"name": "server", "pid": pid, "start_time": start_time}],
        "repo": "/tmp/fakerepo",
        "repo_name": "fakerepo",
        "worktree": "/tmp/fakerepo-wt",
        "branch": "feature/TEST-1",
        "label": "TEST-1",
        "target": "run",
        "started_at": 1700000000.0,
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# 1. Unit: _get_boot_id
# ---------------------------------------------------------------------------

class TestGetBootId:
    def test_macos(self):
        fake = subprocess.CompletedProcess(
            [], 0, stdout="{ sec = 1778086880, usec = 114715 } Wed May  6 18:01:20 2026\n"
        )
        with mock.patch("sys.platform", "darwin"), \
             mock.patch("subprocess.run", return_value=fake):
            assert wt._get_boot_id() == "1778086880"

    def test_linux(self):
        proc_stat = "cpu  123 456\nbtime 1778086880\nprocesses 9999\n"
        with mock.patch("sys.platform", "linux"), \
             mock.patch.object(Path, "read_text", return_value=proc_stat):
            assert wt._get_boot_id() == "1778086880"

    def test_fallback_on_failure(self):
        with mock.patch("sys.platform", "darwin"), \
             mock.patch("subprocess.run", side_effect=FileNotFoundError):
            assert wt._get_boot_id() == "unknown"


# ---------------------------------------------------------------------------
# 2. Unit: _get_process_start_time
# ---------------------------------------------------------------------------

class TestGetProcessStartTime:
    def test_normal(self):
        fake = subprocess.CompletedProcess([], 0, stdout="  Thu May  7 00:55:20 2026\n")
        with mock.patch("subprocess.run", return_value=fake):
            assert wt._get_process_start_time(12345) == "Thu May  7 00:55:20 2026"

    def test_process_gone(self):
        with mock.patch("subprocess.run", side_effect=subprocess.CalledProcessError(1, "ps")):
            assert wt._get_process_start_time(99999) is None

    def test_empty_output(self):
        fake = subprocess.CompletedProcess([], 0, stdout="  \n")
        with mock.patch("subprocess.run", return_value=fake):
            assert wt._get_process_start_time(99999) is None


# ---------------------------------------------------------------------------
# 3. Unit: _proc_alive
# ---------------------------------------------------------------------------

class TestProcAlive:
    def test_alive_start_time_matches(self):
        with mock.patch.object(wt, "_group_alive", return_value=True), \
             mock.patch.object(wt, "_get_process_start_time", return_value=LSTART):
            assert wt._proc_alive(100, LSTART) is True

    def test_alive_start_time_mismatch(self):
        with mock.patch.object(wt, "_group_alive", return_value=True), \
             mock.patch.object(wt, "_get_process_start_time", return_value=LSTART_OTHER):
            assert wt._proc_alive(100, LSTART) is False

    def test_dead(self):
        with mock.patch.object(wt, "_group_alive", return_value=False):
            assert wt._proc_alive(100, LSTART) is False

    def test_no_start_time_legacy(self):
        """Old state without start_time — trusts os.killpg (backward compat)."""
        with mock.patch.object(wt, "_group_alive", return_value=True):
            assert wt._proc_alive(100, None) is True

    def test_ps_fails_for_alive_pid(self):
        """ps can't read the process despite os.killpg succeeding → unknown (None)."""
        with mock.patch.object(wt, "_group_alive", return_value=True), \
             mock.patch.object(wt, "_get_process_start_time", return_value=None):
            assert wt._proc_alive(100, LSTART) is None


# ---------------------------------------------------------------------------
# 4. Integration: reboot simulation
# ---------------------------------------------------------------------------

class TestRebootSimulation:
    def test_status_after_reboot(self, cache_dir, capsys):
        """State from boot A, current boot is B → status shows nothing, file cleaned."""
        sf = _write_state(cache_dir, "fakerepo-abc__TEST-1", _make_state(boot_id=BOOT_A))
        with mock.patch.object(wt, "_get_boot_id", return_value=BOOT_B), \
             mock.patch("os.killpg") as mock_kill:
            wt.cmd_status(SimpleNamespace(global_=True))
        assert not sf.exists(), "state file should be cleaned up"
        mock_kill.assert_not_called()
        assert "no detached apps running" in capsys.readouterr().out

    def test_stop_global_after_reboot(self, cache_dir, capsys):
        """stop -g after reboot: no signal sent, state cleaned."""
        sf = _write_state(cache_dir, "fakerepo-abc__TEST-1", _make_state(boot_id=BOOT_A))
        args = SimpleNamespace(global_=True, ticket=None)
        with mock.patch.object(wt, "_get_boot_id", return_value=BOOT_B), \
             mock.patch("os.killpg") as mock_kill:
            wt.cmd_stop(args)
        assert not sf.exists()
        mock_kill.assert_not_called()

    def test_same_boot_process_alive(self, cache_dir, capsys):
        """Same boot, process alive with matching start_time → shown in status."""
        _write_state(cache_dir, "fakerepo-abc__TEST-1", _make_state(boot_id=BOOT_A))
        with mock.patch.object(wt, "_get_boot_id", return_value=BOOT_A), \
             mock.patch.object(wt, "_group_alive", return_value=True), \
             mock.patch.object(wt, "_get_process_start_time", return_value=LSTART):
            wt.cmd_status(SimpleNamespace(global_=True))
        out = capsys.readouterr().out
        assert "99999" in out
        assert "fakerepo" in out


# ---------------------------------------------------------------------------
# 5. Integration: within-boot PID reuse
# ---------------------------------------------------------------------------

class TestWithinBootPidReuse:
    def test_pid_reused_different_start_time(self, cache_dir):
        """PID alive but start_time mismatch → _state_any_alive returns False."""
        st = _make_state(boot_id=BOOT_A)
        _write_state(cache_dir, "fakerepo-abc__TEST-1", st)
        with mock.patch.object(wt, "_get_boot_id", return_value=BOOT_A), \
             mock.patch.object(wt, "_group_alive", return_value=True), \
             mock.patch.object(wt, "_get_process_start_time", return_value=LSTART_OTHER):
            assert wt._state_any_alive(st) is False

    def test_pid_reused_no_signal_sent(self, cache_dir):
        """stop with reused PID must not send SIGTERM."""
        st = _make_state(boot_id=BOOT_A)
        _write_state(cache_dir, "fakerepo-abc__TEST-1", st)
        with mock.patch.object(wt, "_get_boot_id", return_value=BOOT_A), \
             mock.patch.object(wt, "_group_alive", return_value=True), \
             mock.patch.object(wt, "_get_process_start_time", return_value=LSTART_OTHER), \
             mock.patch("os.killpg") as mock_kill:
            result = wt._state_terminate_all(st)
        assert result is True
        mock_kill.assert_not_called()

    def test_status_cleans_reused_pid(self, cache_dir, capsys):
        """Status should clean up entry when PID is reused."""
        sf = _write_state(cache_dir, "fakerepo-abc__TEST-1", _make_state(boot_id=BOOT_A))
        with mock.patch.object(wt, "_get_boot_id", return_value=BOOT_A), \
             mock.patch.object(wt, "_group_alive", return_value=True), \
             mock.patch.object(wt, "_get_process_start_time", return_value=LSTART_OTHER):
            wt.cmd_status(SimpleNamespace(global_=True))
        assert not sf.exists()
        assert "no detached apps running" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 6. Integration: happy path (real subprocess)
# ---------------------------------------------------------------------------

class TestHappyPath:
    def test_spawn_verify_state_stop(self, cache_dir):
        """Spawn a real sleep process, verify state file schema, stop it."""
        proc = subprocess.Popen(
            ["sleep", "300"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            boot_id = wt._get_boot_id()
            start_time = wt._get_process_start_time(proc.pid)
            st = _make_state(boot_id=boot_id, pid=proc.pid, start_time=start_time)

            assert boot_id != "unknown" or os.environ.get("CI")
            assert start_time is not None

            assert wt._proc_alive(proc.pid, start_time) is True
            assert wt._state_any_alive(st) is True
        finally:
            wt._terminate_group(proc.pid)
            proc.wait()

        assert not wt._group_alive(proc.pid)

    def test_external_kill_detected(self, cache_dir):
        """Spawn, kill externally, verify detected as dead."""
        proc = subprocess.Popen(
            ["sleep", "300"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        start_time = wt._get_process_start_time(proc.pid)
        st = _make_state(
            boot_id=wt._get_boot_id(), pid=proc.pid, start_time=start_time,
        )
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        assert wt._state_any_alive(st) is False


# ---------------------------------------------------------------------------
# 7. Backward compatibility
# ---------------------------------------------------------------------------

class TestBackwardCompat:
    def test_old_state_no_boot_id_alive(self):
        """Old format (no boot_id, no start_time) — trusts os.killpg."""
        st = {"pid": 99999, "target": "run", "started_at": 1700000000.0}
        with mock.patch.object(wt, "_group_alive", return_value=True):
            assert wt._state_any_alive(st) is True

    def test_old_state_no_boot_id_dead(self):
        st = {"pid": 99999, "target": "run", "started_at": 1700000000.0}
        with mock.patch.object(wt, "_group_alive", return_value=False):
            assert wt._state_any_alive(st) is False

    def test_old_processes_format_no_start_time(self):
        """Processes array without start_time — trusts os.killpg."""
        st = {
            "processes": [{"name": "server", "pid": 99999}],
            "started_at": 1700000000.0,
        }
        with mock.patch.object(wt, "_group_alive", return_value=True):
            assert wt._state_any_alive(st) is True


# ---------------------------------------------------------------------------
# 8. Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_boot_id_unknown_at_check_time(self):
        """Can't read current boot → skip boot check, fall through to start_time."""
        st = _make_state(boot_id=BOOT_A)
        with mock.patch.object(wt, "_get_boot_id", return_value="unknown"), \
             mock.patch.object(wt, "_group_alive", return_value=True), \
             mock.patch.object(wt, "_get_process_start_time", return_value=LSTART):
            assert wt._state_any_alive(st) is True

    def test_boot_id_unknown_in_state(self):
        """State has boot_id='unknown' → skip boot check."""
        st = _make_state(boot_id="unknown")
        with mock.patch.object(wt, "_get_boot_id", return_value=BOOT_B), \
             mock.patch.object(wt, "_group_alive", return_value=True), \
             mock.patch.object(wt, "_get_process_start_time", return_value=LSTART):
            assert wt._state_any_alive(st) is True

    def test_ps_fails_at_check_time(self):
        """ps unavailable → unknown, treated as alive (safe: keeps entry visible)."""
        st = _make_state(boot_id=BOOT_A)
        with mock.patch.object(wt, "_get_boot_id", return_value=BOOT_A), \
             mock.patch.object(wt, "_group_alive", return_value=True), \
             mock.patch.object(wt, "_get_process_start_time", return_value=None):
            assert wt._state_any_alive(st) is True

    def test_race_process_exits_between_check_and_signal(self):
        """Process dies between _proc_alive and os.killpg(SIGTERM) — graceful."""
        with mock.patch.object(wt, "_group_alive", return_value=True), \
             mock.patch("os.killpg", side_effect=ProcessLookupError):
            result = wt._terminate_group(99999)
        assert result is True

    def test_is_stale_boot_no_boot_id_key(self):
        """State dict without boot_id key → not stale (can't verify)."""
        assert wt._is_stale_boot({"processes": []}) is False

    def test_is_stale_boot_matching(self):
        with mock.patch.object(wt, "_get_boot_id", return_value=BOOT_A):
            assert wt._is_stale_boot({"boot_id": BOOT_A}) is False

    def test_is_stale_boot_different(self):
        with mock.patch.object(wt, "_get_boot_id", return_value=BOOT_B):
            assert wt._is_stale_boot({"boot_id": BOOT_A}) is True


# ---------------------------------------------------------------------------
# 9. Locale stability (C1)
# ---------------------------------------------------------------------------

class TestLocaleStability:
    def test_ps_called_with_lc_all_c(self):
        """_get_process_start_time must pass LC_ALL=C to ps."""
        fake = subprocess.CompletedProcess([], 0, stdout="  Thu May  7 00:55:20 2026\n")
        with mock.patch("subprocess.run", return_value=fake) as mock_run:
            wt._get_process_start_time(12345)
        call_kwargs = mock_run.call_args
        env = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env")
        assert env is not None, "ps must be called with explicit env"
        assert env.get("LC_ALL") == "C"
        assert env.get("LC_TIME") == "C"


# ---------------------------------------------------------------------------
# 10. cmd_status uses _proc_alive per row (H1)
# ---------------------------------------------------------------------------

class TestStatusPerRow:
    def test_mixed_alive_and_reused(self, cache_dir, capsys):
        """Multi-process state: one alive, one reused → only alive shown."""
        st = {
            "boot_id": BOOT_A,
            "processes": [
                {"name": "server", "pid": 100, "start_time": LSTART},
                {"name": "worker", "pid": 200, "start_time": LSTART},
            ],
            "repo_name": "fakerepo", "label": "TEST-1",
            "worktree": "/tmp/wt", "started_at": 1700000000.0,
        }
        _write_state(cache_dir, "fakerepo-abc__TEST-1", st)

        def fake_group_alive(pgid):
            return True

        def fake_start_time(pid):
            return LSTART if pid == 100 else LSTART_OTHER

        with mock.patch.object(wt, "_get_boot_id", return_value=BOOT_A), \
             mock.patch.object(wt, "_group_alive", side_effect=fake_group_alive), \
             mock.patch.object(wt, "_get_process_start_time", side_effect=fake_start_time):
            wt.cmd_status(SimpleNamespace(global_=True))
        out = capsys.readouterr().out
        assert "100" in out
        assert "200" not in out


# ---------------------------------------------------------------------------
# 11. Unknown state display (H2)
# ---------------------------------------------------------------------------

class TestUnknownStateDisplay:
    def test_unknown_shown_with_question_mark_in_status(self, cache_dir, capsys):
        """Process alive at OS level but ps fails → shown with (?) marker."""
        st = _make_state(boot_id=BOOT_A)
        _write_state(cache_dir, "fakerepo-abc__TEST-1", st)
        with mock.patch.object(wt, "_get_boot_id", return_value=BOOT_A), \
             mock.patch.object(wt, "_group_alive", return_value=True), \
             mock.patch.object(wt, "_get_process_start_time", return_value=None):
            wt.cmd_status(SimpleNamespace(global_=True))
        out = capsys.readouterr().out
        assert "(?)" in out
        assert "fakerepo" in out

    def test_unknown_still_gets_signal_on_stop(self):
        """Unknown process should still be terminated (safe default)."""
        st = _make_state(boot_id=BOOT_A)
        with mock.patch.object(wt, "_get_boot_id", return_value=BOOT_A), \
             mock.patch.object(wt, "_group_alive", return_value=True), \
             mock.patch.object(wt, "_get_process_start_time", return_value=None), \
             mock.patch.object(wt, "_terminate_group", return_value=True) as mock_term:
            wt._state_terminate_all(st)
        mock_term.assert_called_once_with(99999)

    def test_alive_pids_str_unknown_marker(self):
        """_state_alive_pids_str shows (?) for unverifiable entries."""
        st = _make_state(boot_id=BOOT_A)
        with mock.patch.object(wt, "_get_boot_id", return_value=BOOT_A), \
             mock.patch.object(wt, "_group_alive", return_value=True), \
             mock.patch.object(wt, "_get_process_start_time", return_value=None):
            result = wt._state_alive_pids_str(st)
        assert "(?)" in result


# ---------------------------------------------------------------------------
# 12. Legacy state warning (H3)
# ---------------------------------------------------------------------------

class TestLegacyWarning:
    @pytest.fixture(autouse=True)
    def _clear_warned(self):
        wt._LEGACY_WARNED.clear()
        yield
        wt._LEGACY_WARNED.clear()

    def test_warns_on_missing_boot_id(self, capsys):
        st = {"pid": 99999, "target": "run", "label": "OLD-1", "started_at": 1.0}
        with mock.patch.object(wt, "_group_alive", return_value=True):
            wt._state_any_alive(st)
        err = capsys.readouterr().err
        assert "legacy state" in err
        assert "pid-reuse guard limited" in err

    def test_warns_on_missing_start_time(self, capsys):
        st = {
            "boot_id": BOOT_A, "label": "HALF-1",
            "processes": [{"name": "server", "pid": 99999}],
            "started_at": 1.0,
        }
        with mock.patch.object(wt, "_get_boot_id", return_value=BOOT_A), \
             mock.patch.object(wt, "_group_alive", return_value=True):
            wt._state_any_alive(st)
        err = capsys.readouterr().err
        assert "legacy state" in err

    def test_no_warning_for_complete_state(self, capsys):
        st = _make_state(boot_id=BOOT_A)
        with mock.patch.object(wt, "_get_boot_id", return_value=BOOT_A), \
             mock.patch.object(wt, "_group_alive", return_value=True), \
             mock.patch.object(wt, "_get_process_start_time", return_value=LSTART):
            wt._state_any_alive(st)
        err = capsys.readouterr().err
        assert "legacy" not in err

    def test_warns_only_once_per_label(self, capsys):
        st = {"pid": 99999, "target": "run", "label": "DUP-1", "started_at": 1.0}
        with mock.patch.object(wt, "_group_alive", return_value=True):
            wt._state_any_alive(st)
            wt._state_any_alive(st)
        err = capsys.readouterr().err
        assert err.count("legacy state") == 1
