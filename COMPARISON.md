# Comparison with alternatives

`wt` sits at the intersection of three tool categories — worktree management,
task running, and detached process tracking — glued together by ticket-keyed
addressing. No single tool covers all three; the closest equivalent is
stitching several together with shell aliases.

This document compares `wt` against representative tools from each category
across eight concrete scenarios.

## Tool categories

### 1. Worktree wrappers / git tooling

Raw `git worktree`, `git-branchless`, `lazygit` (worktree UI), `git-town`,
ad-hoc dotfile scripts.

These handle worktree creation and switching. None of them run targets inside
worktrees or track detached processes.

### 2. Task runners

`make`, `just`, `task` (go-task), `mask`, `mise tasks`, `nx`, `turbo`.

These define and run named commands. They operate on the current directory and
have no concept of worktree resolution or cross-repo process tracking.

### 3. Detached service / process group managers

`overmind`, `foreman`, `hivemind`, `goreman`, `mprocs`, `process-compose`,
`pm2`, `tmuxinator`.

These launch and supervise groups of processes. They operate on a single
project directory and don't resolve worktrees or aggregate status across
repositories.

### 4. AI-agent-in-worktree orchestration

`claude-squad` / `tmux-claude`, Cursor cloud agents, Devin, Factory.ai,
Codegen, OpenHands.

These run AI agents in isolated environments. `claude-squad` and `tmux-claude`
use local worktrees with tmux sessions. Cloud agents (Cursor, Devin, etc.)
run in sandboxed VMs, sidestepping worktrees entirely. None of them provide
a CLI to run arbitrary targets in a specific worktree by ticket id.

## Scenarios

Each scenario shows the `wt` command alongside the equivalent using raw git +
a representative alternative tool. Where no equivalent exists, it's noted.

---

### 1. Create worktree for ticket from remote branch

```
# wt
wt add WR-12

# git (raw)
git fetch origin
git worktree add --track -b feature/WR-12-foo ../myapp-feature-wr-12-foo origin/feature/WR-12-foo
```

`wt add` resolves the ticket against remote branches (ticket-style-aware fuzzy
match), picks the path, and sets up tracking. Raw git requires you to know the
full branch name and choose a directory.

`git-branchless` and `lazygit` can create worktrees but don't resolve tickets
against remote refs. `git-town` does not manage worktrees.

---

### 2. Run a target inside a worktree from anywhere

```
# wt (from any directory in the same repo)
wt -t test 12

# just (must cd first)
cd ../myapp-splat-12
just test

# make (must cd first)
cd ../myapp-splat-12
make test
```

`wt` resolves the worktree by ticket and runs the target there. Task runners
require you to `cd` into the directory first — they have no worktree
resolution.

---

### 3. Launch dev services detached (group)

```
# wt
wt -d 12                    # starts all targets in the default group

# overmind (from the worktree directory)
cd ../myapp-splat-12
overmind start -D            # reads Procfile, daemonizes

# process-compose (from the worktree directory)
cd ../myapp-splat-12
process-compose up -d        # reads process-compose.yaml
```

`wt` launches each target in its own process group (`start_new_session=True`),
so `wt stop` can SIGTERM the entire group by PGID without orphans. `overmind`
uses tmux sessions. `process-compose` uses its own supervisor. Both require
being in the project directory and maintaining a separate config file
(Procfile / process-compose.yaml) alongside `.wt.yaml`.

---

### 4. List running detached groups

```
# wt
wt status                   # current repo
wt status -g                # all repos (opt-in)

# overmind
overmind echo                # only the current directory's Procfile

# pm2
pm2 list                    # global, but not repo/worktree-aware

# process-compose
process-compose process list # current project only
```

`wt status` shows detached groups for the current repo, grouped by worktree
label. `wt status -g` widens the view to all repos. Process managers are
either scoped to one directory (`overmind`, `foreman`, `process-compose`) or
global without repo/worktree context (`pm2`).

---

### 5. Tail a detached log

```
# wt
wt logs 12

# overmind
overmind connect <process>   # tmux attach, not a plain tail

# pm2
pm2 logs <name>

# process-compose
process-compose process logs <name>
```

`wt logs` resolves the ticket and tails the combined log file. `pm2` and
`process-compose` have similar log commands but require the process name, not
a ticket id.

---

### 6. Stop detached group (PGID, no orphans)

```
# wt
wt stop 12                   # SIGTERM → wait 5s → SIGKILL, entire PGID

# overmind
overmind stop                # current directory only

# pm2
pm2 stop <name>
pm2 kill                     # kills the pm2 daemon itself

# process-compose
process-compose down         # current project only
```

`wt stop` targets the process group by PGID, so children that didn't escape
via `setsid()` are included. `overmind` and `process-compose` stop the current
directory only. `pm2 kill` tears down the entire pm2 daemon, not a single app
group.

---

### 7. `cd` into a worktree by ticket

```
# wt (with shell-init loaded)
wt cd 12

# git worktree (manual)
cd "$(git worktree list | grep WR-12 | awk '{print $1}')"

# no equivalent in task runners or process managers
```

`wt cd` resolves the ticket to a worktree path. With `eval "$(wt shell-init
zsh)"` in your rc file, the shell function actually changes the directory.
Raw git requires parsing `git worktree list` output.

---

### 8. Process tree of a running group

```
# wt
wt tree 12

# ps (manual, need to know the PGID)
ps -g <pgid> -o pid,ppid,etime,command

# overmind / foreman / process-compose
# no built-in process tree command
```

`wt tree` queries `ps` by PGID and renders an ASCII tree. No worktree wrapper,
task runner, or process manager has a built-in equivalent — you'd need to know
the PGID and call `ps` yourself.

---

## Stitched stack: `just` + `overmind` + worktree alias

The closest DIY equivalent to `wt` is stitching a task runner, a process
manager, and a worktree helper together:

| Scenario | `wt` | Stitched equivalent |
|---|---|---|
| Create worktree from ticket | `wt add 12` | `git fetch && git worktree add …` (manual branch name + path) |
| Run target in worktree | `wt -t test 12` | `cd "$(wt-resolve 12)" && just test` (custom `wt-resolve` alias) |
| Launch detached group | `wt -d 12` | `cd … && overmind start -D` (separate Procfile needed) |
| Status | `wt status` | `overmind echo` (current dir only) |
| Tail log | `wt logs 12` | `overmind connect <proc>` (tmux, not plain tail) |
| Stop group (PGID) | `wt stop 12` | `overmind stop` (current dir only, tmux-based, no PGID) |
| cd by ticket | `wt cd 12` | Custom shell function parsing `git worktree list` |
| Process tree | `wt tree 12` | `ps -g <pgid> …` (must know the PGID) |

`wt` collapses the three tools into one command namespace with ticket-keyed
addressing throughout. The things it does that the stitched stack has no
equivalent for:

- **Ticket-keyed addressing**: `wt 12` resolves the same worktree from any
  directory in the repo. Task runners and process managers require you to be
  in the right directory.
- **PGID-level group stop**: `wt stop` signals the entire process group, so
  child processes (build watchers, npm children, hot-reload helpers) are
  included. `overmind` uses tmux sessions; `foreman` sends signals to the
  foreground process only.
- **Crash detection**: `wt status` reports unexpectedly exited processes with
  log tails. Process managers either restart automatically (pm2) or require
  you to notice the tmux pane died (overmind).
