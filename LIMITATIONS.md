# wt — architectural limitations and scope

`wt` tracks processes by POSIX process group leader (spawned with
`start_new_session=True`). It has no supervising daemon, supervisor socket, or
stable OS handles beyond PID/PGID and whatever `ps(1)` reports. Know these
boundaries before relying on it for automation.

## Cannot reach

- **Detached daemons** — Programs that call `setsid()` or double-fork to leave
  the session (typical systemd-style daemons: `gunicorn --daemon`,
  `celery multi`, most production app servers). They exit our process group;
  we cannot reliably signal them.
- **Explicit session breaks** — A target shell that runs `setsid cmd &`
  (or equivalent) deliberately moves work out of our group.

## Best-effort, not guaranteed

- **Boot and PID checks** — Detach stores **`boot_id`** (Darwin **`sysctl kern.boottime`**;
  Linux **`/proc/stat btime`**) plus each leader **`start_time`** from **`ps`** when
  capture succeeds after launch. After reboot, state is treated as stale (not alive,
  no signals) **only if** recorded and current **`boot_id`** are both resolved **and**
  differ. If **`boot_id`** is missing, or **either** side is the literal string
  **`unknown`**, boot-based invalidation does not run—the string **`unknown`** compares
  equal to itself across reboots. **Within a boot**, **`start_time`** mismatch ⇒ that
  leader is not yours / **no `SIGTERM`**. If **`start_time`** was never stored, only
  **`killpg`** semantics apply for that PID.

- **`ps`** gaps — If **`start_time`** is stored but **`ps`** later fails while **`killpg`**
  succeeds, **`wt status`** prints **`pid(?)`** and **`wt stop`** may still **`SIGTERM`**.

- **Thin cache files** — JSON under **`~/.cache/wt`** can still lack **`boot_id`**
  or **`start_time`** until you **`wt -d`** again; **`wt`** warns once per label and
  falls back to signalling-only checks.

## Not in scope (use other tools)

- **Service supervision** — No automatic restart on crash, health checks,
  resource caps, or liveness probes. Use systemd, supervisord, pm2, Foreman,
  Kubernetes, etc.
- **Log management** — Logs grow without rotation or shipping inside `wt`; use
  `logrotate`, `multilog`, journald forwarding, or pipe targets to real log sinks.
- **Process tree visuals** — **`wt status`** prints **one PID row per detached
  target**, i.e. each tracked session-leader PID, **not** its subprocess tree.
  **`wt tree <ticket>`** shows the PGID-scoped tree for each leader; system-wide depth
  still means **`pstree`**, **`htop`**, **`ps`**, etc.
- **Sandboxing / isolation** — Targets inherit **`subprocess` defaults**
  (**`cwd`** is the resolved worktree; user/env match the invoking shell unless you wrap commands).

## Trust model

`.wt.yaml` is source-controlled YAML. **`wt`** (foreground, single target),
**`wt -d …`**, and **`wt -t …`** run commands **`shell=True`**, **`cwd`** at the matched
worktree—same posture as **`Makefile`**, **`package.json`** scripts,
**`Cargo.toml`** hooks. **`wt`** refuses group targets unless you detach (**`-d`**).
Review changes like any executable automation checked into the repo.
