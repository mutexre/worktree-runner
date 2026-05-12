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

- **Modern state guards (WR-12, after WR-1)** — Current `wt` records `boot_id`
  when detaching and each leader's process start timestamp from `ps`. After a
  **reboot**, that stored boot id disagrees with the running system, so leftovers
  in `~/.cache/wt/*.json` are treated as stale (nothing "alive", no signalling)
  unless you knowingly carry over pre-WR-12 state missing `boot_id`. **Within a
  boot**, if the kernel recycles the same PID number for a different process, the
  recorded start time no longer matches `ps` → `wt status`/`wt stop` do not treat
  that slot as your old detached job — the classic PID-reuse failure mode from early
  `wt` is covered for guarded state files.
- **Legacy or unverifiable paths** — State files produced before those fields land
  (or if `start_time`/boot checks cannot run) warn once per label and revert to a
  `killpg`-only interpretation; ambiguity can still surface as `pid(?)` in
  `wt status`. Re-run `wt -d` to regenerate guarded state once you upgrade.

## Not in scope (use other tools)

- **Service supervision** — No automatic restart on crash, health checks,
  resource caps, or liveness probes. Use systemd, supervisord, pm2, Foreman,
  Kubernetes, etc.
- **Log management** — Logs grow without rotation or shipping inside `wt`; use
  `logrotate`, `multilog`, journald forwarding, or pipe targets to real log sinks.
- **Process tree visuals** — `wt status` shows only the tracked group leader.
  Use `wt tree <ticket>` for a PGID-scoped ASCII tree, or `pstree`/`htop` for
  system-wide depth.
- **Sandboxing / isolation** — Targets inherit the invoking user, environment,
  and working directory semantics of ordinary shell subprocesses (same as running
  the script yourself).

## Trust model

`.wt.yaml` is source-controlled YAML whose `targets` expand to arbitrary shell
commands (`shell=True`). Cloning a repository and running `wt` executes whatever
the config defines — same posture as trusting `Makefile`, `package.json`
scripts, or `Cargo.toml` hook commands. Treat changes to `.wt.yaml` like any
other build or automation script in review.
