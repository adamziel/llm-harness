# LLM Harness

`llm-harness` is a deterministic scheduler around Codex CLI worker agents. It
uses Python, tmux, git worktrees, Turso/SQLite, and small MCP tools to keep agentic work
inspectable and restartable instead of trusting a single long-running chat.

## Run it without cloning

Download the latest single-file release, initialize the project, then start the
resident team:

```bash
curl -fsSL https://github.com/adamziel/llm-harness/releases/latest/download/harness -o harness
chmod +x harness
./harness init --goal "Describe what you want the harness to build"
./harness run
```

Requires `python3`, `git`, `tmux`, `codex`, and `gh` for GitHub publishing. The
`gh` command is optional at startup; the harness prints a red warning and keeps
running if GitHub auth is unavailable.

## Commands

The main user-facing CLI is:

```bash
./harness init --goal "ship the project"  # initialize or repair setup
./harness run                             # start or resume resident sessions
./harness status                          # show the Unicode/ANSI dashboard
./harness poke "message"                  # inject a message into the running system
./harness stop                            # stop harness-owned runtime windows
./harness doctor                          # print setup diagnostics
./harness lanes                           # inspect worklanes
./harness agents                          # inspect agents
./harness logs                            # inspect recent events
```

`run` starts the internal updater, test loop, janitor, MCP, watchdog/service
helpers, status page generation, and tmux windows as needed. Those internal
entrypoints are intentionally hidden from help because users should not run them
directly.

`init` records the goal in `.harness/harness.sqlite3`, initializes Git if needed,
creates `DEVELOPMENT.md`, `PLAN.md`, status templates, role prompt files, and
validates the harness MCP. `run` then starts a small resident control plane:
Coordinator, Integrator, queued Developer capacity, and Manhole/support windows. Conceptual
roles such as Architect, Conflict Resolver, Lane Scout, and Goal Planner are
capabilities invoked as short-lived jobs rather than standing sessions. All
Codex worker commands are generated with `--yolo` and `--model gpt-5.5 -c model_reasoning_effort="xhigh"`.
The Manhole starts in supervisor/read-only mode and should only take concrete
actions when the user explicitly authorizes them.
The deterministic integration support window runs `./harness integrate` on a
short loop so ready branches are merged and pushed from a clean harness-owned
worktree. The full test loop is separate and runs continuously; integration
only runs bounded smoke checks.

## Persistent state

Turso is the preferred source of truth when the `pyturso` package is installed;
the single-file harness falls back to Python's built-in SQLite driver when it is
not. The database stores runs, goals, events, agents, tmux panes,
worktrees, worklanes, agent messages, structured agent reports, integration
attempts, issues, resource samples, test runs, parsed test results, bug history,
metric samples, code index rows, settings, and scheduler-routed spawn requests.
Set `HARNESS_DB_DRIVER=turso` to require the Turso driver and fail clearly when
`pyturso` is missing. Connections enable Turso MVCC for concurrent writes and
fall back to WAL on regular SQLite.

The MCP server exposes that state through deterministic tools documented in
`llm_harness/skills/sqlite_mcp/SKILL.md`.

## Status reports

`./harness status` prints a compact TUI dashboard with Unicode borders and ANSI
colors. The updater writes `STATUS.md` and `STATUS.html` from templates created
on first run at `.harness/STATUS_TEMPLATE.md` and `.harness/STATUS_TEMPLATE.html`.
Each deterministic status update stages only the status artifacts, commits them,
and pushes that commit to the repository's `origin` mainline branch when a safe
remote is configured.

## Watchdog

`./harness watchdog` performs repeated scheduler ticks and is intended to be run
by a service manager. Use `install-watchdog` to print systemd or NixOS snippets.
