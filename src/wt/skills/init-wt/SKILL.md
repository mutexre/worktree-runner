---
name: init-wt
description: >-
  Initialize wt (git-worktree dispatcher) for a project by analyzing its
  structure and creating a .wt.yaml config. Use when the user asks to
  "init wt", "set up wt", "configure wt", create a .wt.yaml, or wants to
  use the wt worktree tool with a new project.
---

# Initialize wt for a project

Create a `.wt.yaml` config file for the wt git-worktree dispatcher by
analyzing the project and interactively confirming targets with the user.

## Workflow

### 1. Check prerequisites

- Confirm `wt` is available: `which wt`
- Confirm the directory is a git repo: `git rev-parse --git-common-dir`
- If `.wt.yaml` already exists, show its contents and ask whether to replace

### 2. Analyze the project

Scan the repo root for project markers. Look for these files and infer
candidate targets from whichever are present:

| Marker file | Likely stack | Candidate targets |
|---|---|---|
| `pyproject.toml` / `setup.py` / `setup.cfg` | Python | `run: python <entrypoint>`, `test: python -m pytest`, `install: pip install -e .[dev]` |
| `package.json` | Node/JS/TS | `run: npm start` or script from package.json, `test: npm test`, `install: npm install` |
| `Cargo.toml` | Rust | `run: cargo run`, `test: cargo test`, `install: cargo build` |
| `go.mod` | Go | `run: go run ./cmd/...`, `test: go test ./...` |
| `Makefile` | Any | Extract targets from existing Makefile |
| `docker-compose.yml` | Multi-service | One target per service, group them |
| `Procfile` | Heroku-style | One target per process type |
| `manage.py` | Django | `run: python manage.py runserver`, `test: python manage.py test` |
| `Gemfile` | Ruby | `run: bundle exec rails server`, `test: bundle exec rspec` |
| `mix.exs` | Elixir | `run: mix phx.server`, `test: mix test` |

Also look for:
- `run.py`, `app.py`, `main.py`, `server.py` -- likely entrypoints
- `scripts/` directory -- may contain run/start scripts
- `.env` or `.envrc` -- may need env activation wrapper
- `conda` / `venv` / `.python-version` -- environment hints

### 3. Propose targets to the user

Use the AskQuestion tool to present the discovered targets and let the user
choose which to include. Show each candidate with its inferred command and
let the user confirm, edit, or skip.

Structure the question as a multi-select:

```
Which targets should .wt.yaml include?
[x] run: python run.py
[x] test: python -m pytest
[ ] install: pip install -e .[dev]
[ ] clean: rm -rf build dist *.egg-info
```

After selection, for each selected target, confirm the exact command. If the
user wants to change a command, let them.

### 4. Detect service groups

If the project has multiple long-running services (e.g. backend + frontend,
or docker-compose with multiple services), propose grouping them:

```yaml
targets:
  server: python manage.py runserver
  worker: celery -A myapp worker
  frontend: npm run dev --prefix frontend

groups:
  run:
    - server
    - frontend
  full:
    - server
    - worker
    - frontend
```

Ask the user whether they want a group and which targets belong in it.
Only propose groups when there are 2+ long-running targets.

### 5. Write the config

Write `.wt.yaml` to the repo root. Use this exact format:

```yaml
targets:
  <name>: <shell command>
  ...

# Only if groups were requested:
groups:
  <group-name>:
    - <target-name>
    - <target-name>
```

Values are bare strings (no quoting needed unless the command contains
YAML-special characters like `:` or `#`).

### 6. Verify

After writing, run a quick validation:
- `python3 -c "import yaml; print(yaml.safe_load(open('.wt.yaml')))"` to
  confirm the file parses correctly
- Show the user the final `.wt.yaml` contents
- Remind them they can now use `wt <ticket>` to launch

## .wt.yaml schema reference

```yaml
# Required: map of target-name -> shell command
targets:
  run: <command>          # default target when no -t given
  test: <command>         # wt -t test <ticket>
  install: <command>      # wt -t install <ticket>
  clean: <command>        # wt -t clean <ticket>
  <any-name>: <command>   # custom targets

# Optional: named groups of targets (launched together in detached mode)
groups:
  <group-name>:
    - <target-name>       # must reference a key in targets
    - <target-name>
```

Resolution order when wt resolves `-t TARGET` (default `run`):
1. If TARGET matches a group name, start all member targets
2. If TARGET matches a target name, start that single command
3. If no `.wt.yaml` exists, fall back to `make TARGET`

Groups can only run in detached mode (`wt -d <ticket>`).
