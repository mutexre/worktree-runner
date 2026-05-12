# wt — architectural limitations and scope

`wt` tracks processes by POSIX process group leader (spawned with
`start_new_session=True`). It has no supervising daemon, supervisor socket, or
stable OS handles beyond PID/PGID. Know these boundaries before relying on it
for automation.

## Cannot reach

- **Detached daemons** — Programs that call `setsid()` or double-fork to leave
  the session (typical systemd-style daemons: `gunicorn --daemon`,
  `celery multi`, most production app servers). They exit our process group;
  we cannot reliably signal them.
- **Explicit session breaks** — A target shell that runs `setsid cmd &`
  (or equivalent) deliberately moves work out of our group.

## Best-effort, not guaranteed

- **PID reuse** — Stale state can, in exceptional cases, make `wt status`
  falsely report "alive" or make `wt stop` signal the wrong process after PIDs
  are recycled by the kernel. Discussed further in spike **WR-1**.
- **Reboot leftovers** — State files surviving a reboot can reference PIDs/PGIDs
  that the new boot later assigns to unrelated processes; `wt status` and
  `wt stop --all` may then misbehave until state is swept or invalidated.

## Not in scope (use other tools)

- **Service supervision** — No automatic restart on crash, health checks,
  resource caps, or liveness probes. Use systemd, supervisord, pm2, Foreman,
  Kubernetes, etc.
- **Log management** — Logs grow without rotation or shipping inside `wt`; use
  `logrotate`, `multilog`, journald forwarding, or pipe targets to real log sinks.
- **Process tree visuals** — `wt status` shows the tracked group leader, not an
  entire tree snapshot. Use `pstree`, `htop`, or `ps -g <pgid>` when you need depth.
- **Sandboxing / isolation** — Targets inherit the invoking user, environment,
  and working directory semantics of ordinary shell subprocesses (same as running
  the script yourself).

## Trust model

`.wt.yaml` is source-controlled YAML whose `targets` expand to arbitrary shell
commands (`shell=True`). Cloning a repository and running `wt` executes whatever
the config defines — same posture as trusting `Makefile`, `package.json`
scripts, or `Cargo.toml` hook commands. Treat changes to `.wt.yaml` like any
other build or automation script in review.
