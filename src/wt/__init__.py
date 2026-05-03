"""wt — git-worktree dispatcher.

Operates on the git repository containing the current working directory.
Resolves a ticket id (or branch substring) to a worktree of the current repo
and dispatches commands defined in .wt.yaml (or falls back to make <target>).

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
import time
from collections import Counter
from dataclasses import dataclass, field
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
    """Return the main worktree (common dir) of the repo containing $PWD."""
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
        return cd.parent
    return cd


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

def _resolve(repo: Path, arg: str, style: TicketStyle) -> Worktree:
    pool = _list_worktrees(repo, style)
    if not pool:
        die(f"no worktrees in {repo}")

    arg_l = arg.lower()
    ticket_candidates: set[str] = {arg, arg.upper()}
    if arg.isdigit():
        if style.expand_digits:
            ticket_candidates.update(style.expand_digits(arg))
        if style.prefix_of:
            prefix = _infer_ticket_prefix(repo, style)
            if prefix:
                ticket_candidates.add(f"{prefix}-{arg}".upper())

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


def _group_alive(pgid: int) -> bool:
    """True if the process group (any descendant) still has a member alive.

    `wt -d` launches each command with start_new_session=True, which puts the
    spawned shell and all its descendants in a process group whose leader pid
    equals the spawned shell's pid. So we use the recorded pid as the pgid."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


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
    except ProcessLookupError:
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


def _state_any_alive(st: dict) -> bool:
    return any(_group_alive(int(p["pid"])) for p in _state_pids(st))


def _state_terminate_all(st: dict) -> bool:
    ok = True
    for p in _state_pids(st):
        if _group_alive(int(p["pid"])):
            if not _terminate_group(int(p["pid"])):
                ok = False
    return ok


def _state_alive_pids_str(st: dict) -> str:
    return ", ".join(str(p["pid"]) for p in _state_pids(st) if _group_alive(int(p["pid"])))


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
    with open(cfg_path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)

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


def cmd_logs(args) -> int:
    repo = _current_repo()
    w = _resolve(repo, args.ticket, _style_for(repo))
    log = _log_file(_state_id(repo, w))
    if not log.exists():
        return err(f"no log for {w.label} ({log})")
    return subprocess.call(["tail", "-F", str(log)])


def cmd_launch_fg(args) -> int:
    repo = _current_repo()
    config = _load_config(repo)
    w = _resolve(repo, args.ticket, _resolve_style(config))
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
        processes.append({"name": name, "pid": proc.pid})
        info(f"started {name} (pid {proc.pid})")

    state_path.write_text(json.dumps({
        "processes": processes,
        "repo": str(repo),
        "repo_name": repo.name,
        "worktree": str(w.path),
        "branch": w.branch,
        "label": w.label,
        "target": target,
        "started_at": time.time(),
    }, indent=2))
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
        pids = ", ".join(str(p["pid"]) for p in _state_pids(st))
        info(f"{w.label} pid(s) {pids} stale, cleaning state file")
        state_path.unlink(missing_ok=True)
        return 0
    pids = _state_alive_pids_str(st)
    info(f"stopping {w.label} (pid {pids})")
    ok = _state_terminate_all(st)
    state_path.unlink(missing_ok=True)
    return 0 if ok else err(f"failed to stop all processes for {w.label}")


def cmd_status(_args) -> int:
    if not CACHE_DIR.exists():
        print("no detached apps running")
        return 0
    rows: list[tuple[str, ...]] = [("REPO/LABEL", "TARGET", "PID", "UPTIME", "WORKTREE")]
    found = False
    for sf in sorted(CACHE_DIR.glob("*.json")):
        st = _read_state(sf)
        if not st:
            sf.unlink(missing_ok=True)
            continue
        if not _state_any_alive(st):
            sf.unlink(missing_ok=True)
            continue
        try:
            uptime = _format_uptime(time.time() - float(st.get("started_at", time.time())))
        except (TypeError, ValueError):
            uptime = "?"
        nice = f"{st.get('repo_name', '?')}/{st.get('label', '?')}"
        for p in _state_pids(st):
            if _group_alive(int(p["pid"])):
                rows.append((nice, p.get("name", "?"), str(p["pid"]), uptime, st.get("worktree", "?")))
                found = True
    if not found:
        print("no detached apps running")
        return 0
    _print_table(rows)
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# Argparse plumbing
# ─────────────────────────────────────────────────────────────────────────────

_RESERVED = {
    "ls", "list", "path", "logs", "stop", "status",
    "init", "install-skill", "help", "-h", "--help",
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
            "  wt SPLAT-12             launch 'run' target for SPLAT-12 (foreground)\n"
            "  wt 12                   same, fuzzy match (auto-detects SPLAT- prefix)\n"
            "  wt -d SPLAT-12          launch detached (supports service groups)\n"
            "  wt -d SPLAT-12 --force  replace running detached\n"
            "  wt -t server SPLAT-12   run a specific target instead of 'run'\n"
            "  wt stop SPLAT-12        stop detached\n"
            "  wt stop --all           stop every detached app (across all repos)\n"
            "  wt status               show running detached apps (across all repos)\n"
            "  wt logs SPLAT-12        tail detached log\n"
            "  wt path SPLAT-12        print absolute worktree path\n"
        ),
    )
    sub = p.add_subparsers(dest="cmd")

    sp_init = sub.add_parser("init", help="create .wt.yaml config for this repo")
    sp_init.set_defaults(func=cmd_init)

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

    sp_logs = sub.add_parser("logs", help="tail detached log")
    sp_logs.add_argument("ticket")
    sp_logs.set_defaults(func=cmd_logs)

    sp_stop = sub.add_parser("stop", help="stop detached app")
    sp_stop.add_argument("ticket", nargs="?")
    sp_stop.add_argument("--all", action="store_true", help="stop every detached (any repo)")
    sp_stop.set_defaults(func=cmd_stop)

    sp_status = sub.add_parser("status", help="show running detached apps (any repo)")
    sp_status.set_defaults(func=cmd_status)

    sub.add_parser("help", help="show this help message").set_defaults(
        func=lambda a: (p.print_help() or 0)
    )

    return p


def main(argv: Optional[list[str]] = None) -> int:
    """Console entry point. Returns exit code."""
    if argv is None:
        argv = sys.argv[1:]

    try:
        if len(argv) == 0:
            argv = ["ls"]

        # Detached form: `wt -d <ticket> [--force] [-t TARGET]`
        if argv[0] == "-d":
            d = argparse.ArgumentParser(prog="wt -d", add_help=False)
            d.add_argument("ticket", nargs="?")
            d.add_argument("--force", action="store_true")
            d.add_argument("-t", "--target", default=None)
            args = d.parse_args(argv[1:])
            if not args.ticket:
                return err("specify <ticket>")
            return cmd_launch_detached(args)

        # Foreground launch: `wt <ticket> [-t TARGET]`
        if argv[0] not in _RESERVED:
            f = argparse.ArgumentParser(prog="wt", add_help=False)
            f.add_argument("ticket")
            f.add_argument("-t", "--target", default=None)
            try:
                args = f.parse_args(argv)
            except SystemExit:
                return 1
            return cmd_launch_fg(args)

        parser = _build_parser()
        args = parser.parse_args(argv)
        if not args.cmd:
            parser.print_help()
            return 0
        return args.func(args)
    except KeyboardInterrupt:
        return 130
