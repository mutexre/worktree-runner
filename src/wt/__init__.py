"""wt — git-worktree dispatcher.

Operates on the git repository containing the current working directory.
Resolves a ticket id (or branch substring) to a worktree of the current repo
and dispatches commands defined in .wt.yaml (or falls back to make <target>).

``.wt.yaml`` is always read from the repository's **main** worktree (resolved
via ``git rev-parse --git-common-dir``), never from a linked worktree's
checkout. Branch-specific config files under a linked path are ignored on
purpose: the same ``wt`` invocation must resolve targets the same way no
matter which directory you run it from.

Run state lives in ~/.cache/wt/<repo>-<sha8>__<label>.{json,log}.
The JSON sidecar lets `wt status` enumerate every detached app across every
repository you've ever launched from.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import yaml

XDG_CACHE = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
CACHE_DIR = XDG_CACHE / "wt"

DEFAULT_TARGET = "run"
CONFIG_FILENAME = ".wt.yaml"
_INIT_TARGETS = ["run", "test", "install", "clean"]


# ─────────────────────────────────────────────────────────────────────────────
# Ticket styles
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TicketStyle:
    """How to recognise ticket tokens inside branch names."""
    regex: re.Pattern
    canonicalize: Callable[[re.Match], str]
    # If the style has a prefix (e.g. SPLAT-N), prefix_of returns the canonical
    # prefix from a match so `wt 12` can expand digits into `SPLAT-12`.
    # None for prefix-less styles (e.g. github numeric).
    prefix_of: Optional[Callable[[re.Match], str]] = None
    # Given a digits-only argument like "12", return additional candidate
    # ticket strings to match against (e.g. "#12" for github).
    expand_digits: Optional[Callable[[str], list[str]]] = None


def _upper(m: re.Match) -> str:
    return f"{m.group(1).upper()}-{m.group(2)}"


def _upper_prefix(m: re.Match) -> str:
    return m.group(1).upper()


_STYLE_JIRA = TicketStyle(
    regex=re.compile(r"\b([A-Z][A-Z0-9]+)-(\d+)\b"),
    canonicalize=lambda m: f"{m.group(1)}-{m.group(2)}",
    prefix_of=lambda m: m.group(1),
)

_STYLE_LINEAR = TicketStyle(
    regex=re.compile(r"\b([A-Za-z][A-Za-z0-9]*)-(\d+)\b"),
    canonicalize=_upper,
    prefix_of=_upper_prefix,
)

_STYLE_GITHUB = TicketStyle(
    regex=re.compile(r"(?:^|[\W_])#?(\d+)(?=[\W_]|$)"),
    canonicalize=lambda m: f"#{m.group(1)}",
    prefix_of=None,
    expand_digits=lambda s: [f"#{s}"],
)

_BUILT_IN_STYLES: dict[str, TicketStyle] = {
    "jira": _STYLE_JIRA,
    "linear": _STYLE_LINEAR,
    "github": _STYLE_GITHUB,
}

# Default when no `ticket_style` is configured: case-insensitive prefix-N,
# which catches Jira (SPLAT-12) and Linear (eng-123) in one regex.
_DEFAULT_STYLE = _STYLE_LINEAR


def _custom_style(pattern: str) -> TicketStyle:
    try:
        rx = re.compile(pattern)
    except re.error as e:
        die(f"invalid ticket_style regex: {e}")
    if rx.groups >= 2:
        return TicketStyle(
            regex=rx, canonicalize=_upper, prefix_of=_upper_prefix,
        )
    return TicketStyle(
        regex=rx, canonicalize=lambda m: m.group(0), prefix_of=None,
    )


def _resolve_style(config: Optional[dict]) -> TicketStyle:
    if config is None:
        return _DEFAULT_STYLE
    raw = config.get("ticket_style")
    if raw is None:
        return _DEFAULT_STYLE
    if not isinstance(raw, str):
        die("ticket_style must be a string")
    if raw in _BUILT_IN_STYLES:
        return _BUILT_IN_STYLES[raw]
    return _custom_style(raw)


def _style_for(repo: Path) -> TicketStyle:
    return _resolve_style(_load_config(repo))


# ─────────────────────────────────────────────────────────────────────────────
# Output helpers
# ─────────────────────────────────────────────────────────────────────────────

def info(msg: str) -> None:
    print(f"[wt] {msg}", file=sys.stderr)


def err(msg: str) -> int:
    print(f"[wt] error: {msg}", file=sys.stderr)
    return 1


def die(msg: str) -> "NoReturn":  # type: ignore[name-defined]
    err(msg)
    sys.exit(1)


def _print_table(rows: list[tuple[str, ...]]) -> None:
    if not rows:
        return
    cols = len(rows[0])
    widths = [max(len(r[i]) for r in rows) for i in range(cols)]
    for r in rows:
        print("  ".join(c.ljust(widths[i]) for i, c in enumerate(r)).rstrip())


# ─────────────────────────────────────────────────────────────────────────────
# Repo + worktree discovery
# ─────────────────────────────────────────────────────────────────────────────

def _current_repo() -> Path:
    """Return the main worktree directory of the repo containing $PWD.

    Exits with an error in a bare repository or inside a submodule checkout.
    """
    try:
        common_dir = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        die("not inside a git repository (cwd: " + str(Path.cwd()) + ")")
    cd = Path(common_dir).resolve()
    # `--git-common-dir` returns either an absolute path to the main `.git`
    # directory, or `.git` (relative) if we're already in the main repo.
    if cd.name == ".git":
        root = cd.parent
    else:
        root = cd

    try:
        bare = subprocess.run(
            ["git", "rev-parse", "--is-bare-repository"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        die("not inside a git repository (cwd: " + str(Path.cwd()) + ")")
    if bare == "true":
        die(
            "wt requires a non-bare repository with a working tree "
            "(bare repos have no main checkout for .wt.yaml)."
        )

    spr = subprocess.run(
        ["git", "rev-parse", "--show-superproject-working-tree"],
        capture_output=True, text=True,
    )
    if spr.returncode == 0:
        super_wt = spr.stdout.strip()
        if super_wt:
            die(
                "wt cannot run inside a git submodule checkout "
                f"({Path.cwd()}). Run wt from the superproject tree ({super_wt})."
            )

    return root


def _repo_id(repo: Path) -> str:
    """Stable, human-readable identifier for cache filenames."""
    sha = hashlib.sha1(str(repo.resolve()).encode()).hexdigest()[:8]
    return f"{repo.name}-{sha}"


@dataclass
class Worktree:
    path: Path
    branch: str  # may be "(detached)"
    ticket: Optional[str]  # uppercase canonical, or None
    sha: str  # short HEAD sha (for detached)

    @property
    def label(self) -> str:
        return self.ticket or _slug(self.branch if self.branch != "(detached)" else self.sha)


def _list_worktrees(repo: Path, style: TicketStyle) -> list[Worktree]:
    out = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        capture_output=True, text=True, check=True,
    ).stdout

    worktrees: list[Worktree] = []
    cur_path: Optional[Path] = None
    cur_branch = "(detached)"
    cur_sha = ""
    for line in out.splitlines():
        if line.startswith("worktree "):
            if cur_path is not None:
                worktrees.append(_make_worktree(cur_path, cur_branch, cur_sha, style))
            cur_path = Path(line[len("worktree "):])
            cur_branch = "(detached)"
            cur_sha = ""
        elif line.startswith("HEAD "):
            cur_sha = line[len("HEAD "):][:8]
        elif line.startswith("branch "):
            cur_branch = line[len("branch "):].removeprefix("refs/heads/")
        elif line.startswith("detached"):
            cur_branch = "(detached)"
    if cur_path is not None:
        worktrees.append(_make_worktree(cur_path, cur_branch, cur_sha, style))
    return worktrees


def _make_worktree(path: Path, branch: str, sha: str, style: TicketStyle) -> Worktree:
    m = style.regex.search(branch)
    ticket = style.canonicalize(m) if m else None
    return Worktree(path=path, branch=branch, ticket=ticket, sha=sha)


def _infer_ticket_prefix(repo: Path, style: TicketStyle) -> Optional[str]:
    """Most common ticket prefix across all branches; only meaningful for
    prefix-bearing styles (jira, linear, custom 2-group)."""
    if style.prefix_of is None:
        return None
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "for-each-ref",
             "--format=%(refname:short)", "refs/heads/", "refs/remotes/"],
            capture_output=True, text=True, check=True,
        ).stdout
    except subprocess.CalledProcessError:
        return None
    counter: Counter[str] = Counter()
    for ref in out.splitlines():
        for m in style.regex.finditer(ref):
            counter[style.prefix_of(m)] += 1
    if not counter:
        return None
    return counter.most_common(1)[0][0]


# ─────────────────────────────────────────────────────────────────────────────
# Config loading (.wt.yaml)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RunSpec:
    """One or more named commands resolved from config for a given target."""
    commands: list[tuple[str, str]] = field(default_factory=list)  # [(name, shell_cmd)]
    is_group: bool = False


def _load_config(repo: Path) -> Optional[dict]:
    cfg_path = repo / CONFIG_FILENAME
    if not cfg_path.exists():
        return None
    try:
        with open(cfg_path) as f:
            data = yaml.safe_load(f)
    except Exception as e:
        die(f"failed to parse {cfg_path}: {e}")
    if not isinstance(data, dict):
        die(f"{cfg_path} must be a YAML mapping")
    return data


def _resolve_target(config: Optional[dict], target_name: str) -> RunSpec:
    """Resolve a target name to a RunSpec via config, or fall back to make."""
    if config is None:
        return RunSpec(commands=[(target_name, f"make {target_name}")])

    targets = config.get("targets", {})
    groups = config.get("groups", {})

    if target_name in groups:
        members = groups[target_name]
        if not isinstance(members, list):
            die(f"group '{target_name}' must be a list of target names")
        cmds: list[tuple[str, str]] = []
        for name in members:
            if name not in targets:
                die(f"group '{target_name}' references unknown target '{name}'")
            cmds.append((name, targets[name]))
        return RunSpec(commands=cmds, is_group=True)

    if target_name in targets:
        return RunSpec(commands=[(target_name, targets[target_name])])

    return RunSpec(commands=[(target_name, f"make {target_name}")])


# ─────────────────────────────────────────────────────────────────────────────
# Ticket / branch resolution within current repo
# ─────────────────────────────────────────────────────────────────────────────

def _ticket_candidates(arg: str, repo: Path, style: TicketStyle) -> set[str]:
    """Build the set of ticket strings *arg* could resolve to.

    Includes the arg itself (raw + uppercased), digit-expansions provided by
    the style (e.g. github "#123"), and — if *arg* is bare digits and the
    style has a prefix — the inferred-prefix expansion (e.g. "12" → "WR-12").

    Used by both `_resolve` (against local worktrees) and
    `_resolve_remote_branch` (against `git ls-remote` output) so the two
    resolution paths cannot drift.
    """
    candidates: set[str] = {arg, arg.upper()}
    if arg.isdigit():
        if style.expand_digits:
            candidates.update(style.expand_digits(arg))
        if style.prefix_of:
            prefix = _infer_ticket_prefix(repo, style)
            if prefix:
                candidates.add(f"{prefix}-{arg}".upper())
    return candidates


def _resolve(repo: Path, arg: str, style: TicketStyle) -> Worktree:
    pool = _list_worktrees(repo, style)
    if not pool:
        die(f"no worktrees in {repo}")

    arg_l = arg.lower()
    ticket_candidates = _ticket_candidates(arg, repo, style)

    exact: list[Worktree] = []
    for w in pool:
        if w.ticket and (
            w.ticket in ticket_candidates or w.ticket.upper() in ticket_candidates
        ):
            exact.append(w)
        elif w.branch.lower() == arg_l:
            exact.append(w)
        elif w.path.name.lower() == arg_l:
            exact.append(w)
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        die(_ambig_msg(arg, exact))

    fuzzy = [
        w for w in pool
        if (w.ticket and arg_l in w.ticket.lower())
        or arg_l in w.branch.lower()
        or arg_l in w.path.name.lower()
    ]
    if len(fuzzy) == 1:
        return fuzzy[0]
    if not fuzzy:
        die(f"no worktree matches '{arg}' in {repo.name}. Try `wt`.")
    die(_ambig_msg(arg, fuzzy))


def _ambig_msg(arg: str, candidates: list[Worktree]) -> str:
    listing = ", ".join(c.label for c in candidates)
    return f"ambiguous '{arg}' matches: {listing}. Be more specific."


# ─────────────────────────────────────────────────────────────────────────────
# State file helpers
# ─────────────────────────────────────────────────────────────────────────────

def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", s)


def _state_id(repo: Path, w: Worktree) -> str:
    return f"{_repo_id(repo)}__{_slug(w.label)}"


def _state_file(state_id: str) -> Path:
    return CACHE_DIR / f"{state_id}.json"


def _log_file(state_id: str) -> Path:
    return CACHE_DIR / f"{state_id}.log"


def _read_state(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _write_state_atomic(path: Path, data: dict) -> None:
    """Write JSON state via tmp file + os.replace for crash safety."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _get_boot_id() -> str:
    """Stable identifier for the current boot. Falls back to 'unknown'."""
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["sysctl", "-n", "kern.boottime"],
                capture_output=True, text=True, check=True,
            ).stdout
            m = re.search(r"sec = (\d+)", out)
            if m:
                return m.group(1)
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
    else:
        try:
            for line in Path("/proc/stat").read_text().splitlines():
                if line.startswith("btime "):
                    return line.split()[1]
        except (OSError, IndexError):
            pass
    return "unknown"


_C_ENV: Optional[dict] = None


def _c_env() -> dict:
    """Cached environ with LC_ALL=C for locale-stable ps(1) output."""
    global _C_ENV
    if _C_ENV is None:
        _C_ENV = {**os.environ, "LC_ALL": "C", "LC_TIME": "C"}
    return _C_ENV


def _get_process_start_time(pid: int) -> Optional[str]:
    """Process start time via ps(1), or None if the process doesn't exist."""
    try:
        r = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True, text=True, check=True,
            env=_c_env(),
        )
        val = r.stdout.strip()
        return val if val else None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _group_alive(pgid: int) -> bool:
    """True if the process group has a member alive (raw OS check, no reuse guard)."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _proc_alive(pgid: int, start_time: Optional[str] = None) -> Optional[bool]:
    """Check if a process group is alive with PID-reuse guard.

    Returns True (verified alive), False (dead or PID reused), or None
    (alive at OS level but start_time couldn't be verified — unknown).
    """
    if not _group_alive(pgid):
        return False
    if start_time is None:
        return True
    current = _get_process_start_time(pgid)
    if current is None:
        return None
    if current != start_time:
        return False
    return True


def _is_stale_boot(st: dict) -> bool:
    """True if the state file is from a different boot (all PIDs meaningless)."""
    recorded = st.get("boot_id")
    if not recorded or recorded == "unknown":
        return False
    current = _get_boot_id()
    if current == "unknown":
        return False
    return recorded != current


def _terminate_group(pgid: int, timeout: float = 5.0) -> bool:
    """SIGTERM the whole process group, then SIGKILL after `timeout`."""
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _group_alive(pgid):
            return True
        time.sleep(0.1)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return True
    time.sleep(0.2)
    return not _group_alive(pgid)


def _format_uptime(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _state_pids(st: dict) -> list[dict]:
    """Extract process list from state, handling old single-pid format."""
    if "processes" in st:
        return st["processes"]
    if "pid" in st:
        return [{"name": st.get("target", "run"), "pid": st["pid"]}]
    return []


_LEGACY_WARNED: set[str] = set()


def _warn_legacy(st: dict) -> None:
    """Warn once per label when state file lacks PID-reuse guard fields."""
    label = st.get("label", "?")
    if label in _LEGACY_WARNED:
        return
    has_boot = st.get("boot_id") not in (None, "unknown")
    has_start = all("start_time" in p for p in _state_pids(st))
    if not has_boot or not has_start:
        info(f"{label}: legacy state — pid-reuse guard limited; "
             f"restart with `wt -d` to upgrade")
        _LEGACY_WARNED.add(label)


def _state_any_alive(st: dict) -> bool:
    if _is_stale_boot(st):
        return False
    _warn_legacy(st)
    for p in _state_pids(st):
        status = _proc_alive(int(p["pid"]), p.get("start_time"))
        if status is not False:
            return True
    return False


def _state_terminate_all(st: dict) -> bool:
    if _is_stale_boot(st):
        return True
    ok = True
    for p in _state_pids(st):
        pid = int(p["pid"])
        status = _proc_alive(pid, p.get("start_time"))
        if status is not False:
            if not _terminate_group(pid):
                ok = False
    return ok


def _state_alive_pids_str(st: dict) -> str:
    if _is_stale_boot(st):
        return ""
    parts: list[str] = []
    for p in _state_pids(st):
        status = _proc_alive(int(p["pid"]), p.get("start_time"))
        if status is True:
            parts.append(str(p["pid"]))
        elif status is None:
            parts.append(f"{p['pid']}(?)")
    return ", ".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# Exit-sweep helpers (WR-8)
# ─────────────────────────────────────────────────────────────────────────────

def _read_log_tail(log_path: Path, n: int = 10) -> list[str]:
    """Return the last *n* lines of *log_path*; [] if missing or unreadable."""
    if not log_path.exists():
        return []
    try:
        with open(log_path, errors="replace") as f:
            tail = deque(f, maxlen=n)
        return [line.rstrip("\n") for line in tail]
    except OSError:
        return []


def _format_ago(iso_ts: str) -> str:
    """'2026-05-12T19:30:00+00:00' → '5m ago'."""
    try:
        dt = datetime.fromisoformat(iso_ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = int((datetime.now(timezone.utc) - dt).total_seconds())
        if delta < 60:
            return f"{delta}s ago"
        if delta < 3600:
            return f"{delta // 60}m ago"
        return f"{delta // 3600}h ago"
    except (ValueError, TypeError):
        return iso_ts


def _sweep_exits() -> None:
    """Detect crashed processes and record exit entries in their state files.

    Runs on every ``wt`` invocation before the main command.  Only does OS
    liveness checks + log-tail reads for processes that actually died, so the
    overhead is negligible for the common case (everything still running).
    """
    if not CACHE_DIR.exists():
        return
    for sf in CACHE_DIR.glob("*.json"):
        try:
            st = _read_state(sf)
            if not st:
                continue
            if st.get("stopping"):
                continue
            if _is_stale_boot(st):
                continue
            procs = _state_pids(st)
            if not procs:
                continue

            already_recorded: set[int] = {e["pid"] for e in st.get("exits", [])}
            new_exits: list[dict] = []
            for p in procs:
                pid = int(p["pid"])
                if pid in already_recorded:
                    continue
                if _proc_alive(pid, p.get("start_time")) is False:
                    log_path = sf.with_suffix(".log")
                    # exit_code is always None: wt is not the parent of detached
                    # processes (start_new_session=True puts them in their own
                    # session), so waitpid returns ECHILD.  WR-19 (push-based
                    # watcher) is the path to recovering the actual exit code.
                    new_exits.append({
                        "name": p.get("name", "run"),
                        "pid": pid,
                        "exit_code": None,
                        "exited_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "log_tail": _read_log_tail(log_path),
                    })

            if new_exits:
                updated = dict(st)
                updated["exits"] = st.get("exits", []) + new_exits
                _write_state_atomic(sf, updated)
        except OSError as exc:
            info(f"sweep: skipping {sf.name}: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# Subcommands
# ─────────────────────────────────────────────────────────────────────────────

def cmd_init(_args) -> int:
    repo = _current_repo()
    cfg_path = repo / CONFIG_FILENAME
    if cfg_path.exists():
        resp = input(f"[wt] {CONFIG_FILENAME} already exists. Overwrite? [y/N] ").strip().lower()
        if resp != "y":
            info("aborted")
            return 0

    info(f"initializing config for {repo.name}")
    print("Enter commands for each target (leave blank to skip):\n")

    targets: dict[str, str] = {}
    for name in _INIT_TARGETS:
        cmd = input(f"  {name}: ").strip()
        if cmd:
            targets[name] = cmd

    if not targets:
        info("no targets defined, nothing to write")
        return 0

    data: dict = {"targets": targets}
    fd, tmp = tempfile.mkstemp(dir=cfg_path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, cfg_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    info(f"wrote {cfg_path}")
    return 0


_DEFAULT_SKILL_TARGET = Path.home() / ".cursor" / "skills" / "init-wt"


def cmd_install_skill(args) -> int:
    src = Path(__file__).parent / "skills" / "init-wt"
    if not src.is_dir():
        return err(f"bundled skill not found at {src}")

    target = Path(args.target).expanduser() if args.target else _DEFAULT_SKILL_TARGET
    target.parent.mkdir(parents=True, exist_ok=True)

    if target.exists() or target.is_symlink():
        if not args.force:
            return err(
                f"{target} already exists. Pass --force to replace, "
                f"or --target <path> to install elsewhere."
            )
        if target.is_symlink() or target.is_file():
            target.unlink()
        else:
            import shutil
            shutil.rmtree(target)

    target.symlink_to(src)
    info(f"installed init-wt skill: {target} -> {src}")
    return 0


def cmd_ls(_args) -> int:
    repo = _current_repo()
    pool = _list_worktrees(repo, _style_for(repo))
    if not pool:
        print(f"no worktrees in {repo}")
        return 0
    rows: list[tuple[str, ...]] = [("TICKET", "BRANCH", "WORKTREE")]
    pool.sort(key=lambda w: (w.ticket or "~", w.branch))
    for w in pool:
        rows.append((w.ticket or "—", w.branch, str(w.path)))
    _print_table(rows)
    return 0


def cmd_path(args) -> int:
    repo = _current_repo()
    w = _resolve(repo, args.ticket, _style_for(repo))
    print(str(w.path))
    return 0


_SHELL_INIT_BEGIN = "# BEGIN wt shell integration"
_SHELL_INIT_END = "# END wt shell integration"
_SHELL_INIT_NOTE = "(managed by wt shell-init; do not edit by hand)"


def _shell_snippet_bash_zsh() -> str:
    return (
        "wt() {\n"
        '  if [ "$1" = "cd" ] && [ -n "$2" ]; then\n'
        "    local _wt_path\n"
        '    _wt_path=$(command wt path "$2") || return $?\n'
        '    cd "$_wt_path"\n'
        "  else\n"
        '    command wt "$@"\n'
        "  fi\n"
        "}\n"
    )


def _shell_snippet_fish() -> str:
    return (
        f"{_SHELL_INIT_BEGIN} {_SHELL_INIT_NOTE}\n"
        "function wt --wraps wt\n"
        "    if test (count $argv) -ge 2; and test \"$argv[1]\" = cd\n"
        "        set -l _wt_path (command wt path $argv[2])\n"
        "        or return\n"
        "        cd $_wt_path\n"
        "    else\n"
        "        command wt $argv\n"
        "    end\n"
        "end\n"
        f"{_SHELL_INIT_END} {_SHELL_INIT_NOTE}\n"
    )


def _fish_conf_d_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "fish" / "conf.d" / "wt.fish"


def _is_managed_wt_shell_file(content: str) -> bool:
    return _SHELL_INIT_BEGIN in content and _SHELL_INIT_END in content


def cmd_cd(args) -> int:
    """Print resolved worktree path; hint on stderr when stderr is a TTY."""
    repo = _current_repo()
    w = _resolve(repo, args.ticket, _style_for(repo))
    print(str(w.path))
    if sys.stderr.isatty():
        info(
            "shell cannot cd from a child process — run "
            '`eval "$(wt shell-init zsh)"` (or bash/fish; see `wt shell-init --help`) '
            "so `wt cd` changes this shell's directory, or use: "
            f'cd "$(wt path {args.ticket})"'
        )
    return 0


def cmd_shell_init(args) -> int:
    shell: str = args.shell
    if args.install and args.uninstall:
        return err("use only one of --install / --uninstall")
    if args.install or args.uninstall:
        if shell in ("bash", "zsh"):
            return err(
                f"`wt shell-init {shell} --install` is not supported — bash/zsh have no "
                "standard drop-in path. Print the snippet and paste into your rc file:\n"
                f"  eval \"$(wt shell-init {shell})\""
            )

    if shell in ("bash", "zsh"):
        text = _shell_snippet_bash_zsh()
        if args.uninstall:
            return err("`wt shell-init bash|zsh --uninstall` is not applicable (no installed file).")
        sys.stdout.write(text)
        return 0

    # fish
    path = _fish_conf_d_path()
    content = _shell_snippet_fish()

    if args.uninstall:
        if not path.exists():
            info(f"nothing to remove ({path})")
            return 0
        existing = path.read_text()
        if not _is_managed_wt_shell_file(existing):
            return err(
                f"refusing to remove unrelated file: {path} "
                "(missing wt sentinels; delete manually if needed)"
            )
        path.unlink()
        info(f"removed {path}")
        return 0

    if args.install:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            cur = path.read_text()
            if cur == content:
                info(f"unchanged ({path})")
                return 0
            if not _is_managed_wt_shell_file(cur) and not args.force:
                return err(
                    f"refusing to overwrite unrelated file: {path}. "
                    "Use --force to replace, or remove/rename the file first."
                )
        path.write_text(content)
        info(f"wrote {path}")
        return 0

    sys.stdout.write(content)
    return 0


def cmd_logs(args) -> int:
    repo = _current_repo()
    w = _resolve(repo, args.ticket, _style_for(repo))
    log = _log_file(_state_id(repo, w))
    if not log.exists():
        return err(f"no log for {w.label} ({log})")
    return subprocess.call(["tail", "-F", str(log)])


def _passthrough_name(cmd_str: str) -> str:
    """Extract a short name from a pass-through command for state files."""
    first = cmd_str.split()[0] if cmd_str.strip() else "cmd"
    return Path(first).name


def _no_dispatcher_error() -> int:
    print(
        "[wt] error: no target dispatcher available. Either:\n"
        "  - run `wt init` to create a .wt.yaml\n"
        "  - or pass-through a command: wt <ticket> -- <your command>",
        file=sys.stderr,
    )
    sys.exit(2)


def _has_makefile(wt_path: Path) -> bool:
    return (wt_path / "Makefile").exists() or (wt_path / "makefile").exists()


def cmd_launch_fg(args) -> int:
    repo = _current_repo()
    config = _load_config(repo)
    w = _resolve(repo, args.ticket, _resolve_style(config))
    passthrough: list[str] = getattr(args, "passthrough", [])

    if passthrough:
        cmd = " ".join(passthrough)
        info(f"repo:     {repo.name}")
        info(f"worktree: {w.path}")
        info(f"branch:   {w.branch}")
        info(f"running:  {cmd}")
        return subprocess.call(cmd, shell=True, cwd=str(w.path))

    if config is None and not _has_makefile(w.path):
        return _no_dispatcher_error()

    target = args.target or DEFAULT_TARGET
    spec = _resolve_target(config, target)

    if spec.is_group:
        names = ", ".join(n for n, _ in spec.commands)
        return err(
            f"'{target}' is a group ({names}). "
            f"Use `wt -d {args.ticket}` to run it detached, "
            f"or `wt -t <target> {args.ticket}` for a single target."
        )

    _name, cmd = spec.commands[0]
    info(f"repo:     {repo.name}")
    info(f"worktree: {w.path}")
    info(f"branch:   {w.branch}")
    info(f"running:  {cmd}")
    return subprocess.call(cmd, shell=True, cwd=str(w.path))


def cmd_launch_detached(args) -> int:
    repo = _current_repo()
    config = _load_config(repo)
    w = _resolve(repo, args.ticket, _resolve_style(config))
    passthrough: list[str] = getattr(args, "passthrough", [])

    if passthrough:
        cmd_str = " ".join(passthrough)
        spec = RunSpec(commands=[(_passthrough_name(cmd_str), cmd_str)])
        target = "--"
    else:
        if config is None and not _has_makefile(w.path):
            return _no_dispatcher_error()
        target = args.target or DEFAULT_TARGET
        spec = _resolve_target(config, target)

    sid = _state_id(repo, w)
    state_path = _state_file(sid)
    log_path = _log_file(sid)

    existing = _read_state(state_path)
    if existing and _state_any_alive(existing):
        if not args.force:
            pids = _state_alive_pids_str(existing)
            return err(
                f"{w.label} already running (pid {pids}). "
                f"Use `wt stop {w.label}` or pass --force to replace."
            )
        info("--force: stopping existing processes")
        _state_terminate_all(existing)
        state_path.unlink(missing_ok=True)

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    log_fp = open(log_path, "ab")
    processes: list[dict] = []
    started_at = time.time()
    boot_id = _get_boot_id()

    def _persist() -> None:
        _write_state_atomic(state_path, {
            "boot_id": boot_id,
            "processes": processes,
            "repo": str(repo),
            "repo_name": repo.name,
            "worktree": str(w.path),
            "branch": w.branch,
            "label": w.label,
            "target": target,
            "started_at": started_at,
        })

    try:
        for name, cmd in spec.commands:
            proc = subprocess.Popen(
                cmd,
                shell=True,
                cwd=str(w.path),
                stdin=subprocess.DEVNULL,
                stdout=log_fp,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            entry: dict = {"name": name, "pid": proc.pid}
            stime = _get_process_start_time(proc.pid)
            if stime:
                entry["start_time"] = stime
            processes.append(entry)
            _persist()
            info(f"started {name} (pid {proc.pid})")
    except Exception as exc:
        log_fp.close()
        if processes:
            info(f"partial launch ({len(processes)}/{len(spec.commands)}): {exc}")
            info("use `wt stop` to clean up started processes")
            return 1
        raise

    log_fp.close()
    info(f"log: {log_path}")
    return 0


def cmd_stop(args) -> int:
    if args.all:
        if not CACHE_DIR.exists():
            info("nothing running")
            return 0
        any_stopped = False
        for sf in sorted(CACHE_DIR.glob("*.json")):
            st = _read_state(sf)
            if st and _state_any_alive(st):
                # Set stopping flag before signalling so sweep won't record a crash.
                flagged = dict(st)
                flagged["stopping"] = True
                _write_state_atomic(sf, flagged)
                pids = _state_alive_pids_str(st)
                info(f"stopping {st.get('repo_name')}/{st.get('label')} (pid {pids})")
                _state_terminate_all(st)
                any_stopped = True
            sf.unlink(missing_ok=True)
        if not any_stopped:
            info("nothing running")
        return 0

    if not args.ticket:
        return err("specify <ticket> or --all")

    repo = _current_repo()
    w = _resolve(repo, args.ticket, _style_for(repo))
    state_path = _state_file(_state_id(repo, w))
    st = _read_state(state_path)
    if not st:
        info(f"{w.label} is not running (no state file)")
        return 0
    if not _state_any_alive(st):
        exits = st.get("exits", [])
        if exits:
            info(f"{w.label}: not running (crashed — run `wt status` to see details)")
        else:
            pids = ", ".join(str(p["pid"]) for p in _state_pids(st))
            info(f"{w.label} pid(s) {pids} stale, cleaning state file")
        state_path.unlink(missing_ok=True)
        return 0
    # Set stopping flag before signalling so sweep won't record a crash.
    flagged = dict(st)
    flagged["stopping"] = True
    _write_state_atomic(state_path, flagged)
    pids = _state_alive_pids_str(st)
    info(f"stopping {w.label} (pid {pids})")
    ok = _state_terminate_all(st)
    state_path.unlink(missing_ok=True)
    return 0 if ok else err(f"failed to stop all processes for {w.label}")


# ─────────────────────────────────────────────────────────────────────────────
# Process-tree helpers (WR-9)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class _PsRow:
    pid: int
    ppid: int
    etime: str
    cmd: str


def _ps_group(pgid: int) -> list[_PsRow]:
    """Return all processes whose PGID equals *pgid* via `ps`.

    Uses PGID-scoped query so we only see what `wt stop` can actually signal.
    Processes that escaped via setsid() have a different PGID and are omitted
    intentionally (see WR-1 for the limitations discussion).
    """
    try:
        result = subprocess.run(
            ["ps", "-g", str(pgid), "-o", "pid=,ppid=,etime=,command="],
            capture_output=True,
            text=True,
            env=_c_env(),
        )
    except FileNotFoundError:
        return []

    rows: list[_PsRow] = []
    for line in result.stdout.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            rows.append(_PsRow(
                pid=int(parts[0]),
                ppid=int(parts[1]),
                etime=parts[2],
                cmd=parts[3],
            ))
        except ValueError:
            continue
    return rows


def _render_process_tree(rows: list[_PsRow], pgid: int) -> str:
    """Render *rows* as an ASCII tree rooted at the process whose PID == pgid.

    Processes whose PPID is not in the set are treated as roots (there should
    be exactly one: the session leader).  Connectors follow the classic
    tree(1) style: ├── for non-last children, └── for the last child.
    """
    if not rows:
        return ""

    by_pid: dict[int, _PsRow] = {r.pid: r for r in rows}
    pids_in_set: set[int] = set(by_pid)
    children: dict[int, list[int]] = {r.pid: [] for r in rows}
    roots: list[int] = []

    for r in rows:
        if r.ppid in pids_in_set:
            children[r.ppid].append(r.pid)
        else:
            roots.append(r.pid)

    cmd_width = 80
    lines: list[str] = []

    def _node_line(r: _PsRow) -> str:
        cmd = r.cmd if len(r.cmd) <= cmd_width else r.cmd[:cmd_width - 1] + "…"
        return f"[{r.pid}]  {r.etime:>12}  {cmd}"

    def _walk(pid: int, prefix: str, is_last: bool) -> None:
        connector = "└── " if is_last else "├── "
        lines.append(prefix + connector + _node_line(by_pid[pid]))
        child_prefix = prefix + ("    " if is_last else "│   ")
        kids = children[pid]
        for i, kid in enumerate(kids):
            _walk(kid, child_prefix, i == len(kids) - 1)

    for i, root_pid in enumerate(roots):
        lines.append(_node_line(by_pid[root_pid]))
        kids = children[root_pid]
        for j, kid in enumerate(kids):
            _walk(kid, "", j == len(kids) - 1)

    return "\n".join(lines)


def cmd_tree(args) -> int:
    """Print the process tree for each group under a running worktree."""
    repo = _current_repo()
    w = _resolve(repo, args.ticket, _style_for(repo))
    sid = _state_id(repo, w)
    st = _read_state(_state_file(sid))

    if not st:
        return err(f"{w.label}: no state file (not running?)")

    procs = _state_pids(st)
    if not procs:
        return err(f"{w.label}: state file has no process entries")

    any_output = False
    for p in procs:
        pgid = int(p["pid"])
        name = p.get("name", "?")
        alive = _proc_alive(pgid, p.get("start_time"))
        if alive is False:
            print(f"[{name}] pgid {pgid}  (exited or PID reused)")
            continue
        rows = _ps_group(pgid)
        if not rows:
            print(f"[{name}] pgid {pgid}  (no ps output — process may have exited)")
            continue
        marker = " ?" if alive is None else ""
        print(f"[{name}] pgid {pgid}{marker}")
        tree_str = _render_process_tree(rows, pgid)
        for line in tree_str.splitlines():
            print("  " + line)
        any_output = True

    if any_output:
        print()
        print("Note: processes that called setsid() have a different PGID and are")
        print("not shown here — they are also unreachable by `wt stop` (see WR-1).")
    return 0


def cmd_status(_args) -> int:
    if not CACHE_DIR.exists():
        print("no detached apps running")
        return 0

    running_rows: list[tuple[str, ...]] = [("REPO/LABEL", "TARGET", "PID", "UPTIME", "WORKTREE")]
    found_running = False
    # (state_file, state_dict, exits_list) for entries to display then clear
    exits_pending: list[tuple[Path, dict, list[dict]]] = []

    for sf in sorted(CACHE_DIR.glob("*.json")):
        st = _read_state(sf)
        if not st:
            sf.unlink(missing_ok=True)
            continue

        has_alive = _state_any_alive(st)
        exits = st.get("exits", [])

        if exits:
            exits_pending.append((sf, st, exits))

        if has_alive:
            try:
                uptime = _format_uptime(time.time() - float(st.get("started_at", time.time())))
            except (TypeError, ValueError):
                uptime = "?"
            nice = f"{st.get('repo_name', '?')}/{st.get('label', '?')}"
            for p in _state_pids(st):
                status = _proc_alive(int(p["pid"]), p.get("start_time"))
                if status is True:
                    running_rows.append((nice, p.get("name", "?"), str(p["pid"]), uptime, st.get("worktree", "?")))
                    found_running = True
                elif status is None:
                    running_rows.append((nice, p.get("name", "?"), f"{p['pid']}(?)", uptime, st.get("worktree", "?")))
                    found_running = True
        elif not exits:
            sf.unlink(missing_ok=True)

    if not found_running and not exits_pending:
        print("no detached apps running")
        return 0

    if found_running:
        _print_table(running_rows)

    if exits_pending:
        print()
        print("EXITED SINCE LAST CHECK:")
        for sf, st, exits in exits_pending:
            nice = f"{st.get('repo_name', '?')}/{st.get('label', '?')}"
            for e in exits:
                ec = e.get("exit_code")
                ec_str = f"exit {ec}" if ec is not None else "exit ?"
                ago = _format_ago(e.get("exited_at", ""))
                print(f"  {nice} / {e.get('name', '?')}   {ec_str}   {ago}")
                log_tail = e.get("log_tail", [])
                if log_tail:
                    print("    \u2500\u2500\u2500\u2500 last 10 log lines \u2500\u2500\u2500\u2500")
                    for line in log_tail:
                        print(f"    {line}")
            # Acknowledge: clear exits; delete file if nothing alive.
            updated = dict(st)
            updated.pop("exits", None)
            if _state_any_alive(st):
                _write_state_atomic(sf, updated)
            else:
                sf.unlink(missing_ok=True)

    return 0


# ─────────────────────────────────────────────────────────────────────────────
# wt add: fetch remote branch → create local worktree
# ─────────────────────────────────────────────────────────────────────────────

def _branch_slug(branch: str) -> str:
    """Lowercase branch name → filesystem-safe slug (non-alnum → '-')."""
    slug = re.sub(r"[^A-Za-z0-9-]", "-", branch).lower()
    return re.sub(r"-{2,}", "-", slug).strip("-")


def _ls_remote_branches(repo: Path, remote: str = "origin") -> dict[str, str]:
    """Return {branch_name: sha} for all branches on *remote*.

    Raises subprocess.CalledProcessError on network / auth failure.
    """
    result = subprocess.run(
        ["git", "-C", str(repo), "ls-remote", "--heads", remote],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        msg = (result.stderr.strip() or "git ls-remote failed").splitlines()[0]
        die(f"cannot reach remote '{remote}': {msg}")

    branches: dict[str, str] = {}
    for line in result.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            sha, ref = parts
            name = ref.removeprefix("refs/heads/")
            branches[name] = sha
    return branches


def _resolve_remote_branch(
    arg: str,
    remote_branches: dict[str, str],
    repo: Path,
    style: TicketStyle,
) -> str:
    """Resolve *arg* to a single remote branch name or die."""
    # Strip remote prefix (e.g. "origin/foo" → "foo").
    for prefix in ("origin/", "upstream/"):
        if arg.startswith(prefix):
            bare = arg[len(prefix):]
            if bare in remote_branches:
                return bare
            die(f"branch '{bare}' not found on remote (after stripping '{prefix}')")

    if arg in remote_branches:
        return arg

    ticket_candidates = _ticket_candidates(arg, repo, style)

    matched: list[str] = []
    for branch in remote_branches:
        m = style.regex.search(branch)
        if m:
            canonical = style.canonicalize(m)
            if canonical in ticket_candidates or canonical.upper() in ticket_candidates:
                matched.append(branch)

    if len(matched) == 1:
        return matched[0]
    if len(matched) > 1:
        candidates = "\n  ".join(matched)
        die(
            f"'{arg}' matches multiple remote branches:\n  {candidates}\n"
            f"Re-run with the exact branch name."
        )

    # Fuzzy substring fallback.
    arg_l = arg.lower()
    fuzzy = [b for b in remote_branches if arg_l in b.lower()]
    if len(fuzzy) == 1:
        return fuzzy[0]
    if len(fuzzy) > 1:
        candidates = "\n  ".join(fuzzy)
        die(
            f"'{arg}' matches multiple remote branches:\n  {candidates}\n"
            f"Re-run with the exact branch name."
        )

    die(f"no remote branch matches '{arg}'. Try `git fetch && git branch -r`.")


def cmd_add(args) -> int:
    repo = _current_repo()
    style = _style_for(repo)
    arg: str = args.ref
    remote = "origin"

    info(f"fetching from {remote} …")
    fetch_result = subprocess.run(
        ["git", "-C", str(repo), "fetch", remote],
        capture_output=True, text=True,
    )
    if fetch_result.returncode != 0:
        msg = (fetch_result.stderr.strip() or "git fetch failed").splitlines()[0]
        die(f"fetch failed: {msg}")

    remote_branches = _ls_remote_branches(repo, remote)
    branch = _resolve_remote_branch(arg, remote_branches, repo, style)

    # Determine worktree path.
    if args.path:
        wt_path = Path(args.path).expanduser().resolve()
    else:
        slug = _branch_slug(branch)
        wt_path = repo.parent / f"{repo.name}-{slug}"

    # Idempotency: worktree already registered at this path.
    existing = _list_worktrees(repo, style)
    for w in existing:
        if w.path.resolve() == wt_path.resolve():
            info(f"already added at {wt_path}")
            print(str(wt_path))
            return 0
        if w.branch == branch:
            info(f"branch '{branch}' already checked out at {w.path}")
            info(f"already added at {w.path}")
            print(str(w.path))
            return 0

    # Check for a stale local branch with the same name.
    local_branches_out = subprocess.run(
        ["git", "-C", str(repo), "branch", "--list", branch],
        capture_output=True, text=True,
    ).stdout.strip()
    if local_branches_out:
        die(
            f"local branch '{branch}' already exists; "
            f"delete it (`git branch -D {branch}`) and retry."
        )

    # Create the worktree tracking the remote branch.
    info(f"creating worktree {wt_path}  (branch: {branch})")
    wt_result = subprocess.run(
        [
            "git", "-C", str(repo), "worktree", "add",
            "--track", "-b", branch,
            str(wt_path),
            f"{remote}/{branch}",
        ],
        capture_output=True, text=True,
    )
    if wt_result.returncode != 0:
        msg = (wt_result.stderr.strip() or "git worktree add failed")
        die(f"failed to create worktree: {msg}")

    print(str(wt_path))
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# Argparse plumbing
# ─────────────────────────────────────────────────────────────────────────────

_RESERVED = {
    "add", "ls", "list", "path", "cd", "logs", "stop", "status", "tree",
    "init", "install-skill", "shell-init", "help", "-h", "--help",
}


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wt",
        description="Git-worktree dispatcher (operates on the repo at $PWD).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples (run from anywhere inside the repo):\n"
            "  wt init                 create .wt.yaml for this repo\n"
            "  wt install-skill        install the init-wt agent skill\n"
            "  wt                      list worktrees of this repo\n"
            "  wt add WR-12            fetch + create worktree for remote branch WR-12\n"
            "  wt add cursor/foo       fetch + create worktree for branch cursor/foo\n"
            "  wt add WR-12 --path /tmp/my-wt  use a custom worktree path\n"
            "  wt SPLAT-12             launch 'run' target for SPLAT-12 (foreground)\n"
            "  wt 12                   same, fuzzy match (auto-detects SPLAT- prefix)\n"
            "  wt SPLAT-12 -- pytest   pass-through: run pytest in the worktree\n"
            "  wt -d SPLAT-12 -- npm run dev  detached pass-through\n"
            "  wt -d SPLAT-12          launch detached (supports service groups)\n"
            "  wt -d SPLAT-12 --force  replace running detached\n"
            "  wt -t server SPLAT-12   run a specific target instead of 'run'\n"
            "  wt stop SPLAT-12        stop detached\n"
            "  wt stop --all           stop every detached app (across all repos)\n"
            "  wt status               show running detached apps (across all repos)\n"
            "  wt logs SPLAT-12        tail detached log\n"
            "  wt path SPLAT-12        print absolute worktree path\n"
            "  wt cd SPLAT-12          same as path; tty stderr hints shell-init\n"
            "  wt shell-init zsh       print shell function (eval in ~/.zshrc)\n"
            "  wt tree SPLAT-12        print process tree of running group\n"
        ),
    )
    sub = p.add_subparsers(dest="cmd")

    sp_init = sub.add_parser("init", help="create .wt.yaml config for this repo")
    sp_init.set_defaults(func=cmd_init)

    sp_add = sub.add_parser(
        "add",
        help="fetch a remote branch and create a local worktree for it",
    )
    sp_add.add_argument(
        "ref",
        help="ticket id (e.g. WR-12), branch name (cursor/foo), or origin/branch",
    )
    sp_add.add_argument(
        "--path", default=None,
        help="worktree directory (default: ../<repo>-<branch-slug>)",
    )
    sp_add.set_defaults(func=cmd_add)

    sp_skill = sub.add_parser(
        "install-skill",
        help="symlink the bundled init-wt agent skill into your skills dir",
    )
    sp_skill.add_argument(
        "--target", default=None,
        help="install location (default: ~/.cursor/skills/init-wt)",
    )
    sp_skill.add_argument(
        "--force", action="store_true",
        help="replace an existing target",
    )
    sp_skill.set_defaults(func=cmd_install_skill)

    sp_ls = sub.add_parser("ls", help="list worktrees of this repo", aliases=["list"])
    sp_ls.set_defaults(func=cmd_ls)

    sp_path = sub.add_parser("path", help="print worktree path")
    sp_path.add_argument("ticket")
    sp_path.set_defaults(func=cmd_path)

    sp_cd = sub.add_parser(
        "cd",
        help="print worktree path (use shell-init so the shell can actually cd)",
    )
    sp_cd.add_argument("ticket")
    sp_cd.set_defaults(func=cmd_cd)

    sp_sh = sub.add_parser(
        "shell-init",
        help="emit wt shell wrapper (bash/zsh: stdout; fish: --install to conf.d)",
    )
    sp_sh.add_argument(
        "shell",
        choices=["bash", "zsh", "fish"],
        help="target shell",
    )
    sp_sh.add_argument(
        "--install", action="store_true",
        help="fish only: write ~/.config/fish/conf.d/wt.fish (or $XDG_CONFIG_HOME)",
    )
    sp_sh.add_argument(
        "--uninstall", action="store_true",
        help="fish only: remove the drop-in file if managed by wt",
    )
    sp_sh.add_argument(
        "--force", action="store_true",
        help="fish --install: replace a non-wt file at the target path",
    )
    sp_sh.set_defaults(func=cmd_shell_init)

    sp_logs = sub.add_parser("logs", help="tail detached log")
    sp_logs.add_argument("ticket")
    sp_logs.set_defaults(func=cmd_logs)

    sp_stop = sub.add_parser("stop", help="stop detached app")
    sp_stop.add_argument("ticket", nargs="?")
    sp_stop.add_argument("--all", action="store_true", help="stop every detached (any repo)")
    sp_stop.set_defaults(func=cmd_stop)

    sp_status = sub.add_parser("status", help="show running detached apps (any repo)")
    sp_status.set_defaults(func=cmd_status)

    sp_tree = sub.add_parser("tree", help="print process tree of a running worktree group")
    sp_tree.add_argument("ticket")
    sp_tree.set_defaults(func=cmd_tree)

    sub.add_parser("help", help="show this help message").set_defaults(
        func=lambda a: (p.print_help() or 0)
    )

    return p


def main(argv: Optional[list[str]] = None) -> int:
    """Console entry point. Returns exit code."""
    if argv is None:
        argv = sys.argv[1:]

    try:
        _sweep_exits()

        if len(argv) == 0:
            argv = ["ls"]

        # Split on `--`: everything after is a pass-through command.
        passthrough: list[str] = []
        if "--" in argv:
            idx = argv.index("--")
            passthrough = argv[idx + 1:]
            argv = argv[:idx]

        # Detached form: `wt -d <ticket> [--force] [-t TARGET] [-- cmd...]`
        if argv[0] == "-d":
            d = argparse.ArgumentParser(prog="wt -d", add_help=False)
            d.add_argument("ticket", nargs="?")
            d.add_argument("--force", action="store_true")
            d.add_argument("-t", "--target", default=None)
            args = d.parse_args(argv[1:])
            if not args.ticket:
                return err("specify <ticket>")
            if passthrough and args.target:
                return err("cannot combine -t with --")
            args.passthrough = passthrough
            return cmd_launch_detached(args)

        # Foreground launch: `wt <ticket> [-t TARGET] [-- cmd...]`
        if argv[0] not in _RESERVED:
            f = argparse.ArgumentParser(prog="wt", add_help=False)
            f.add_argument("ticket")
            f.add_argument("-t", "--target", default=None)
            try:
                args = f.parse_args(argv)
            except SystemExit:
                return 1
            if passthrough and args.target:
                return err("cannot combine -t with --")
            args.passthrough = passthrough
            return cmd_launch_fg(args)

        if passthrough:
            return err("-- pass-through is only valid with wt <ticket> or wt -d <ticket>")

        parser = _build_parser()
        args = parser.parse_args(argv)
        if not args.cmd:
            parser.print_help()
            return 0
        return args.func(args)
    except KeyboardInterrupt:
        return 130
