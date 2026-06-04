# LLM Harness

`llm-harness` is a deterministic scheduler around Codex CLI worker agents. It
uses Python, tmux, git worktrees, SQLite, and small MCP tools to keep agentic work
inspectable and restartable instead of trusting a single long-running chat.

## Run it without cloning

Download the latest single-file release and start the harness:

```bash
curl -fsSL https://github.com/adamziel/llm-harness/releases/latest/download/harness -o harness && chmod +x harness && ./harness run --goal "Describe what you want the harness to build"
```

Requires `python3`, `git`, `tmux`, `codex`, and `gh` for GitHub publishing. The
`gh` command is optional at startup; the harness prints a red warning and keeps
running if GitHub auth is unavailable.

## Commands

The user-facing CLI has only the three commands requested in `harness.md`:

```bash
./harness run --goal "ship the project"   # start or resume everything
./harness status                          # show the Unicode/ANSI dashboard
./harness poke "message"                  # inject a message into the running system
```

`run` starts the internal updater, test loop, janitor, MCP, watchdog/service
helpers, status page generation, and tmux windows as needed. Those internal
entrypoints are intentionally hidden from help because users should not run them
directly.

On first run, the harness records the goal in `.harness/harness.sqlite3`, creates
`PLAN.md` if needed, starts a tmux session (or uses the current one), opens
`manhole`, `status`, `updater`, and `tests` windows, and then maintains the
selected team preset. All Codex worker commands are generated with `--yolo` and
`--model gpt-5.5-xhigh-fast`.

## Persistent state

SQLite is the source of truth. It stores goals, events, agents, tmux panes,
worktrees, work lanes, resource samples, test runs, parsed test results, bug
reports, metric samples, code index rows, prompt messages, and scheduler-routed
spawn requests.

The MCP server exposes that state through deterministic tools documented in
`llm_harness/skills/sqlite_mcp/SKILL.md`.

## Status reports

`./harness status` prints a compact TUI dashboard with Unicode borders and ANSI
colors. The updater writes `STATUS.md` and `STATUS.html` from templates created
on first run at `.harness/STATUS_TEMPLATE.md` and `.harness/STATUS_TEMPLATE.html`.

## Watchdog

`./harness watchdog` performs repeated scheduler ticks and is intended to be run
by a service manager. Use `install-watchdog` to print systemd or NixOS snippets.
