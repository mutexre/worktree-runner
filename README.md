# wt

Git-worktree dispatcher. Resolves a ticket id (e.g. `SPLAT-12`) or branch
substring to a worktree of the current git repository and runs commands
defined in `.wt.yaml` there. Tracks detached processes.

```
$ wt -d 12
$ wt -d 7
$ wt status
REPO/LABEL      TARGET  PID    UPTIME    WORKTREE
myapp/SPLAT-12  server  34567  00:01:42  ../myapp-splat-12
myapp/SPLAT-7   server  33445  00:14:08  ../myapp-splat-7
$ wt stop 7
```

## vs. asking an AI agent

An agent can also run commands in the right worktree, but each invocation
costs tokens and a few seconds of round-trip. `wt 12` is a plain command:
no LLM call, no latency.

## Setup

```bash
pip install git+https://github.com/mutexre/worktree-runner.git
```

Or clone and install editable for development:

```bash
git clone https://github.com/mutexre/worktree-runner.git
pip install -e ./worktree-runner
```

In each project:

```bash
cd <repo-root> && wt init
```

`wt init` prompts for common targets and writes `.wt.yaml`. An agent skill
that scaffolds the same config from project structure is bundled — install
it once with:

```bash
wt install-skill                 # default: ~/.cursor/skills/init-wt
wt install-skill --target <path> # other agent skill directories
```

Then, in any repo, ask your agent to "init wt for this project".

Requirements: Python 3.10+, git >= 2.5.

## Config (`.wt.yaml`)

```yaml
targets:
  run: python run.py
  test: pytest
  install: pip install -e .[dev]
```

For projects with multiple long-running services, group them:

```yaml
targets:
  server: python manage.py runserver
  worker: celery -A myapp worker
  frontend: npm run dev

groups:
  run: [server, frontend]
  full: [server, worker, frontend]
```

`wt -d 12` starts every target in the `run` group as a separately tracked
process. `wt stop 12` terminates all of them. If no `.wt.yaml` exists, `wt`
falls back to `make <target>`.

Each target is launched in its own process group, so any subprocesses it
spawns (build watchers, hot-reload workers, npm-spawned children, etc.) are
tracked together: `wt status` reports the group as alive while any
descendant is running, and `wt stop` SIGTERMs the entire group. The only
escape is a process that explicitly calls `setsid()` to detach itself
(rare for dev tooling).

## Commands

```
wt                       list worktrees
wt init                  create .wt.yaml (interactive)
wt <ticket>              run default target in foreground
wt -d <ticket>           run detached (supports groups)
wt -d <ticket> --force   replace running detached
wt -t <target> <ticket>  run a specific target
wt stop <ticket>         stop detached (SIGTERM, SIGKILL after 5s)
wt stop --all            stop everything, all repos
wt status                show all detached apps, all repos
wt logs <ticket>         tail -f the detached log
wt path <ticket>         print absolute worktree path
```

## Resolution

`wt` resolves the argument against three things, in this order:

1. **Exact match** on ticket, full branch name, or worktree directory name
2. **Fuzzy substring** on the same three fields

Ticket detection is configurable. By default `wt` matches case-insensitive
`prefix-N` tokens (covers Jira `SPLAT-12` and Linear `eng-42`) and infers
the prefix from existing branches.

- `wt SPLAT-12` — exact ticket
- `wt 12` — expands to `SPLAT-12` using the inferred prefix
- `wt feature/login` — exact branch name (works even if no ticket scheme
  applies to this repo)
- `wt login` — fuzzy substring against ticket, branch, or directory name

Override the matcher in `.wt.yaml`:

```yaml
ticket_style: jira         # strict uppercase: SPLAT-12
ticket_style: linear       # case-insensitive prefix-N (default)
ticket_style: github       # numeric IDs: 123-fix-bug -> #123
ticket_style: 'TASK_(\d+)' # any regex; 1 group = full token, 2 groups = prefix-N
```

## License

MIT.
