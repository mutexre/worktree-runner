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

- **Modern state guards (WR-12, after WR-1)** — Detach records **`boot_id`**
  (Darwin: `sysctl kern.boottime`; Linux: `/proc/stat` `btime`) and each
  session-leader **`start_time`** from `ps` when that query succeeds immediately
  after `Popen`. Code paths: `_is_stale_boot`, `_proc_alive` in `src/wt/__init__.py`.

  **Across reboots** — Entries are discarded as stale (not alive / no signalling)
  only when both **recorded** and **current** `boot_id` are known **and** they
  differ. If `_get_boot_id()` falls back to **`"unknown"`** (parse failure,
  unsupported OS), if the file omits `boot_id`, or if the stored value is the
  literal **`"unknown"`**, the boot shortcut does **not** run (note: **`"unknown"`**
  matches itself across reboots, so that path never auto-invalidates by boot id).
  Phantom PIDs after reboot then depend on **`start_time`** plus plain **`killpg`**
  semantics.

  **Within a boot** — When **`start_time`** is recorded, `_proc_alive` compares it
  to live `ps` output; mismatch ⇒ treat as gone / skip signals. Rows without stored
  **`start_time`** (recording failed right after spawn) behave like legacy: trust
  **`killpg` only** until you re-detach—not hidden guesswork, but weaker guarantees.

- **Unverifiable-but-live PIDs** — If **`start_time`** is present but **`ps` fails**
  while **`os.killpg` still succeeds** (`_proc_alive` returns **`None`**), **`wt stop`**
  may still **`SIGTERM` that PID** (`status is not False`). **`wt status`** shows **`pid(?)`**.

- **Legacy state** — Pre-WR-12 JSON lacks guard fields ⇒ same style of `killpg`
  probing + deprecation warning (`_warn_legacy`). Re-run `wt -d` to refresh.

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
