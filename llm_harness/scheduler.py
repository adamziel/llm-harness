"""Deterministic scheduler that owns Codex worker process lifecycle."""

from __future__ import annotations

import os
import re
import signal
import shlex
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import db
from .codex import MCP_SERVER_NAME, build_codex_command, codex_mcp_config_args
from .indexer import refresh_index
from .integration import branch_integration_state
from .janitor import run_janitor
from .resources import sample_resources
from .roles import prompt_for_role, slug_role, specs_for_team
from .status import STATUS_REFRESH_SECONDS, ensure_templates, refresh_reports
from .tmux import Tmux, TmuxUnavailable, _session_name, shell_command

IDLE_SECONDS = 30 * 60
IDLE_PROMPT_SECONDS = 30 * 60
AUDITOR_SPAWN_SECONDS = 60
SINGLETON_SPAWN_ROLES = {
    "Architect",
    "Auditor",
    "Verifier",
    "Goal Planner",
    "Lane Scout",
    "Dependency Mapper",
    "Conflict Resolver",
    "Reproducer",
    "Prompt/Protocol Maintainer",
    "Narrative Summarizer",
}
JANITOR_SECONDS = 60 * 60
LOW_RESOURCE_SECONDS = 60
HIGH_RESOURCE_SECONDS = 30
INTEGRATION_BACKPRESSURE_SECONDS = 20 * 60
INTEGRATION_READY_STATUSES = ("ready_for_integration",)
INTEGRATION_RECOVERY_ACTIVE_LIMIT = 3
REPORT_ONLY_DEVELOPER_ACTIVE_LIMIT = 1
REPORT_ONLY_CARD_MARKERS = ("read-only", "read only", "no source edits", "no_source_edits", ".harness/reports", "focused replay")
INTEGRATION_RESOLUTION_CARD_RE = re.compile(r"card #(\d+)")
INTEGRATION_BRANCH_RE = re.compile(r"(?m)^Branch:\s*(\S+)")
SUPPORT_ROLES = {"Manhole", "Status reporter", "Janitor"}
DEFAULT_DEVELOPMENT_MD = """# Development Guide

This starter file was created by the harness because DEVELOPMENT.md was missing.
Edit it with project-specific commands and conventions for future agents.

## Build

- Inspect the repository before choosing commands.
- Prefer the smallest command that verifies the current change.

## Test

- Run focused tests for the files or behavior you changed.
- Run broader suites only when the change requires it or an integrator asks.

## Agent workflow

- Work on one assigned worklane at a time.
- Keep edits narrow and preserve existing style.
- Avoid creating a single huge file with the entire project.
- Avoid fragmenting every tiny thing into its own file or function.
- Write intention-led docblocks for most functions and types you create; explain why they exist.
- Document what/how only when it is not obvious from the code.
- Commit reasonably often to preserve progress.
- Report structured status through the harness MCP `agent_report` tool.
- Record meaningful status through the harness MCP tools.
- Avoid unsupported claims of completion; cite tests, files, commits, or other evidence.
- Leave unrelated files untouched.
"""


class HarnessScheduler:
    """Own the durable event loop instead of trusting worker agents to self-manage."""

    def __init__(self, root: str | Path = ".", tmux: Tmux | None = None):
        self.root = Path(root).resolve()
        self.paths = db.bootstrap(self.root)
        self.tmux = tmux or Tmux()

    def init_project(self, goal: str | None = None) -> int:
        """Initialize or repair harness state without starting the resident team."""

        def initialize(conn: sqlite3.Connection) -> int:
            self.ensure_git_repo(conn)
            self.check_gh(conn)
            self.check_project_context(conn)
            ensure_templates(self.root)
            self.write_role_prompt_files(conn)
            self.ensure_goal(conn, goal)
            self.check_local_tools(conn)
            conn.commit()
            if not self.check_harness_mcp(conn):
                return 1
            self.initialize_index(conn)
            refresh_reports(conn, self.root)
            db.set_meta(conn, "initialized_at", db.utc_now())
            db.set_meta(conn, "initialized_version", "refined")
            db.set_meta(conn, "resident_team_default", "building")
            db.log_event(conn, "init", "Harness initialized or repaired")
            return 0

        return self.with_retrying_db("init", initialize)

    def run(self, goal: str | None = None, team: str = "auto", once: bool = False) -> int:
        """Start or resume the harness, then keep monitoring worker state."""

        def start(conn: sqlite3.Connection) -> int:
            if not self.validate_initialized(conn):
                return 1
            db.set_meta(conn, "scheduler_pid", str(os.getpid()))
            self.ensure_git_repo(conn)
            self.check_gh(conn)
            self.check_project_context(conn)
            conn.commit()
            if not self.check_codex_mcp(conn):
                db.set_meta(conn, "scheduler_pid", "")
                return 1
            db.set_meta(conn, "harness_stopped", "0")
            if db.get_meta(conn, "red_banner") == "Harness stopped.":
                db.set_meta(conn, "red_banner", "")
            self.start_support_windows(conn)
            refresh_reports(conn, self.root)
            effective_team = self.effective_team(conn, team)
            db.log_event(conn, "scheduler", f"Harness run started with team preset {effective_team}")
            return 0

        try:
            start_code = self.with_retrying_db("startup", start)
        except Exception as exc:
            if not db.is_disk_io_error(exc):
                raise
            print(
                "\033[31mHarness database disk I/O error during startup. Free disk space, repair the filesystem/database, "
                "or run with HARNESS_DB_DRIVER=turso.\033[0m",
                file=sys.stderr,
            )
            return 1
        if start_code:
            return start_code

        if once:
            def tick(conn: sqlite3.Connection) -> None:
                self.tick_once(conn, self.effective_team(conn, team))
                refresh_reports(conn, self.root)
                db.set_meta(conn, "scheduler_pid", "")

            self.with_retrying_db("one-shot tick", tick)
            return 0

        print("Harness scheduler running. Press Ctrl-C to stop; workers remain inspectable in tmux.", flush=True)
        last_db_warning = ""
        try:
            while True:
                def tick(conn: sqlite3.Connection) -> None:
                    self.tick_once(conn, self.effective_team(conn, team))
                    if self.status_refresh_due(conn):
                        refresh_reports(conn, self.root)

                try:
                    self.with_retrying_db("scheduler tick", tick)
                    last_db_warning = ""
                except Exception as exc:
                    if not db.is_disk_io_error(exc):
                        raise
                    warning = (
                        "Harness database disk I/O error during scheduler tick; keeping the scheduler alive. "
                        "Free disk space, repair the filesystem/database, or run with HARNESS_DB_DRIVER=turso."
                    )
                    if warning != last_db_warning:
                        print(f"\033[31m{warning}\033[0m", file=sys.stderr)
                        last_db_warning = warning
                time.sleep(5)
        except KeyboardInterrupt:
            print("Harness scheduler stopped by user; agent tmux windows remain available.", flush=True)

            def mark_stopped(conn: sqlite3.Connection) -> None:
                db.log_event(conn, "scheduler", "Harness scheduler stopped by user")
                db.set_meta(conn, "scheduler_pid", "")

            self.with_retrying_db("shutdown", mark_stopped)
            return 130

    def with_retrying_db(self, label: str, action):
        """Run scheduler database work through SQLite/Turso's busy handler."""

        last_error: Exception | None = None
        for attempt in range(8):
            try:
                with db.connect(self.paths.db) as conn:
                    db.init_db(conn)
                    concurrent = db.begin_concurrent(conn)
                    try:
                        result = action(conn)
                        if concurrent and conn.in_transaction:
                            conn.commit()
                        return result
                    except Exception:
                        if concurrent and conn.in_transaction:
                            conn.rollback()
                        raise
            except Exception as exc:
                if not db.is_retryable_error(exc):
                    raise
                last_error = exc
                if attempt == 0:
                    print(f"\033[33mSQLite/Turso write conflict during {label} ({exc}); retrying immediately.\033[0m", file=sys.stderr)
        if last_error is not None:
            raise last_error
        raise RuntimeError(f"{label} did not run")

    def status_refresh_due(self, conn: sqlite3.Connection) -> bool:
        """Throttle deterministic status publishing to the renderer interval."""

        last = float(db.get_meta(conn, "last_status_refresh_epoch", "0") or 0)
        return time.time() - last >= STATUS_REFRESH_SECONDS

    def validate_initialized(self, conn: sqlite3.Connection) -> bool:
        """Require explicit initialization before resident sessions are started."""

        if db.get_meta(conn, "initialized_at"):
            return True
        message = "Harness is not initialized. Run ./harness init first."
        db.set_meta(conn, "red_banner", message)
        db.log_event(conn, "init_required", message)
        print(f"\033[31m{message}\033[0m", file=sys.stderr)
        return False

    def write_role_prompt_files(self, conn: sqlite3.Connection) -> None:
        """Materialize reusable role prompts for inspection and repair."""

        roles_dir = self.paths.prompts / "roles"
        roles_dir.mkdir(parents=True, exist_ok=True)
        goal = db.get_goal(conn)
        goal_text = goal["text"] if goal else ""
        for role in ("Manhole", "Coordinator", "Developer", "Integrator", "Auditor", "Goal Planner", "Architect"):
            prompt = prompt_for_role(role, slug_role(role), goal_text, str(self.paths.db), str(self.root))
            path = roles_dir / f"{slug_role(role)}.md"
            if not path.exists():
                path.write_text(prompt)

    def check_local_tools(self, conn: sqlite3.Connection) -> None:
        """Record availability of local tools init depends on without starting agents."""

        tmux_available = self.tmux.available() if hasattr(self.tmux, "available") else True
        if tmux_available:
            db.set_meta(conn, "tmux_status", "available")
        else:
            db.set_meta(conn, "tmux_status", "missing")
            db.log_event(conn, "warning", "tmux is missing; run can still track state but cannot start inspectable windows")
            print("\033[31mtmux is not installed or not on PATH.\033[0m", file=sys.stderr)
        try:
            codex = subprocess.run(["codex", "--version"], cwd=self.root, text=True, capture_output=True, timeout=5, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            db.set_meta(conn, "codex_status", "missing")
            db.log_event(conn, "warning", f"Codex CLI unavailable: {exc}")
            print(f"\033[31mCodex CLI unavailable: {exc}\033[0m", file=sys.stderr)
            return
        db.set_meta(conn, "codex_status", "available" if codex.returncode == 0 else "unknown")

    def check_harness_mcp(self, conn: sqlite3.Connection) -> bool:
        """Verify the harness stdio MCP itself before Codex receives it."""

        harness = self.harness_executable()
        if not harness.exists():
            db.log_event(conn, "mcp_failed", f"Harness executable not found for MCP: {harness}")
            return False
        try:
            server = subprocess.run(
                [str(harness), "--root", str(self.root), "mcp"],
                input='{"jsonrpc":"2.0","id":1,"method":"initialize"}\n{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n',
                cwd=self.root,
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            db.log_event(conn, "mcp_failed", f"Harness MCP server did not start: {exc}")
            return False
        ok = server.returncode == 0 and "memory_query" in server.stdout and "agent_report" in server.stdout
        db.set_meta(conn, "harness_mcp_status", "available" if ok else "failed")
        if ok:
            db.log_event(conn, "mcp", "Harness MCP server passed init preflight")
        else:
            detail = (server.stderr or server.stdout or "no MCP output").strip()
            db.log_event(conn, "mcp_failed", f"Harness MCP server did not expose required tools: {detail}")
        return ok

    def initialize_index(self, conn: sqlite3.Connection) -> None:
        """Prime the code index when possible without blocking future worktrees."""

        try:
            count = refresh_index(conn, self.root)
        except Exception as exc:
            db.log_event(conn, "index_failed", f"Initial code index unavailable; agents can fall back to search: {exc}")
            return
        db.log_event(conn, "index", f"Indexed {count} files during init")

    def stop(self) -> dict[str, int]:
        """Stop harness-owned runtime processes and mark durable state inactive."""

        with db.connect(self.paths.db) as conn:
            db.init_db(conn)
            pids = self.scheduler_pids(conn)
            windows = self.harness_windows(conn)
            sessions = self.harness_sessions(conn, windows)
            db.set_meta(conn, "harness_stopped", "1")
            db.set_meta(conn, "scheduler_pid", "")
            db.set_meta(conn, "tmux_session", "")
            db.set_meta(conn, "tmux_attach", "")
            db.set_meta(conn, "red_banner", "Harness stopped.")
            conn.commit()
            signaled = self.signal_processes(pids)
            killed_windows = 0
            killed_sessions = 0
            for session, session_windows in sessions.items():
                if session.startswith("llm-harness-"):
                    try:
                        if self.tmux.kill_session(session):
                            killed_sessions += 1
                            killed_windows += len(session_windows)
                    except Exception:
                        pass
                    continue
                for window in sorted(session_windows):
                    try:
                        if self.tmux.kill_window(session, window):
                            killed_windows += 1
                    except Exception:
                        continue
            terminal_statuses = db.AGENT_TERMINAL_STATUSES
            status_placeholders = ",".join("?" for _ in terminal_statuses)
            stopped_agents = conn.execute(
                f"SELECT COUNT(*) AS count FROM agents WHERE current_status NOT IN ({status_placeholders})",
                terminal_statuses,
            ).fetchone()["count"]
            conn.execute(
                f"""
                UPDATE agents
                SET current_status = 'stopped', ended_at = ?, last_seen_at = ?, notes = 'Stopped by harness stop'
                WHERE current_status NOT IN ({status_placeholders})
                """,
                (db.utc_now(), db.utc_now(), *terminal_statuses),
            )
            cancelled_spawns = conn.execute("UPDATE spawn_requests SET status = 'cancelled' WHERE status = 'queued'").rowcount
            cancelled_messages = conn.execute("UPDATE messages SET status = 'cancelled' WHERE status = 'queued'").rowcount
            db.log_event(
                conn,
                "stop",
                "Stopped harness runtime",
                payload={
                    "tmux_windows": killed_windows,
                    "tmux_sessions": killed_sessions,
                    "agents": int(stopped_agents),
                    "spawn_requests": int(cancelled_spawns),
                    "messages": int(cancelled_messages),
                    "scheduler_processes": signaled,
                },
            )
        return {
            "tmux_windows": killed_windows,
            "tmux_sessions": killed_sessions,
            "agents": int(stopped_agents),
            "spawn_requests": int(cancelled_spawns),
            "messages": int(cancelled_messages),
            "scheduler_processes": signaled,
        }

    def reset_counters(self) -> dict[str, int]:
        """Clear stale harness telemetry after upgrading control-plane fixes."""

        with db.connect(self.paths.db) as conn:
            db.init_db(conn)
            terminal_statuses = db.AGENT_TERMINAL_STATUSES
            status_placeholders = ",".join("?" for _ in terminal_statuses)
            terminal_agents = conn.execute(
                f"SELECT COUNT(*) AS count FROM agents WHERE current_status IN ({status_placeholders})",
                terminal_statuses,
            ).fetchone()["count"]
            conn.execute(f"DELETE FROM agents WHERE current_status IN ({status_placeholders})", terminal_statuses)

            test_results = conn.execute("DELETE FROM test_results").rowcount
            test_runs = conn.execute("DELETE FROM test_runs").rowcount
            bug_reports = conn.execute("DELETE FROM bug_reports WHERE status = 'open'").rowcount
            issues = conn.execute("DELETE FROM issues WHERE source = 'test-loop' AND status = 'open'").rowcount

            now = db.utc_now()
            integration_lanes = conn.execute(
                """
                UPDATE worklanes
                SET status = 'ready_for_integration',
                    stage = 'integration',
                    integration_queue = 'ready_fast_path',
                    last_activity_at = ?,
                    notes = trim(notes || char(10) || 'Reset from integration_failed after harness upgrade; retry integration.')
                WHERE status = 'integration_failed'
                """,
                (now,),
            ).rowcount
            spawn_requests = conn.execute("UPDATE spawn_requests SET status = 'cancelled' WHERE status = 'queued'").rowcount
            messages = conn.execute("UPDATE messages SET status = 'cancelled' WHERE status = 'queued'").rowcount
            for key in (
                "red_banner",
                "integration_backpressure_active",
                "integration_backlog_since",
                "last_progress_stall_alert",
                "low_resource_since",
                "last_low_resource_prompt",
                "high_resource_since",
            ):
                db.set_meta(conn, key, "")
            db.log_event(
                conn,
                "reset_counters",
                "Reset stale harness telemetry after upgrade",
                payload={
                    "terminal_agents": int(terminal_agents),
                    "test_runs": int(test_runs),
                    "test_results": int(test_results),
                    "bug_reports": int(bug_reports),
                    "issues": int(issues),
                    "integration_lanes": int(integration_lanes),
                    "spawn_requests": int(spawn_requests),
                    "messages": int(messages),
                },
            )
        return {
            "terminal_agents": int(terminal_agents),
            "test_runs": int(test_runs),
            "test_results": int(test_results),
            "bug_reports": int(bug_reports),
            "issues": int(issues),
            "integration_lanes": int(integration_lanes),
            "spawn_requests": int(spawn_requests),
            "messages": int(messages),
        }

    def harness_sessions(self, conn: sqlite3.Connection, windows: set[str]) -> dict[str, set[str]]:
        """Find tmux sessions containing harness windows, even after metadata was cleared."""

        candidates = {db.get_meta(conn, "tmux_session", "")}
        if hasattr(self.tmux, "current_session"):
            candidates.add(self.tmux.current_session())
        candidates.discard("")
        if not hasattr(self.tmux, "list_sessions") or not hasattr(self.tmux, "list_windows"):
            return {session: set(windows) for session in candidates}

        expected_harness_session = _session_name(self.root)
        session_names = set(candidates)
        session_names.update(self.tmux.list_sessions())
        sessions: dict[str, set[str]] = {}
        for session in session_names:
            session_windows = self.tmux.list_windows(session)
            matched = {
                window
                for window, cwd in session_windows.items()
                if window in windows and _path_belongs_to_root(cwd, self.root)
            }
            if session == expected_harness_session or (session.startswith("llm-harness-") and session in candidates):
                sessions[session] = set(session_windows)
            elif matched:
                sessions[session] = matched
        return sessions

    def harness_windows(self, conn: sqlite3.Connection) -> set[str]:
        """Return tmux windows owned by this harness run."""

        windows = {"manhole", "status", "updater", "tests", "integration"}
        for row in conn.execute("SELECT tmux_window FROM agents WHERE tmux_window != ''"):
            windows.add(str(row["tmux_window"]))
        return windows

    def scheduler_pids(self, conn: sqlite3.Connection) -> list[int]:
        """Find running harness scheduler/watchdog processes for this repository."""

        pids: set[int] = set()
        current = os.getpid()
        recorded = int(db.get_meta(conn, "scheduler_pid", "0") or 0)
        if recorded > 0 and recorded != current:
            pids.add(recorded)
        for pid, command in _process_rows():
            if pid == current:
                continue
            command_lower = command.lower()
            if "harness" not in command_lower:
                continue
            if " run" not in command_lower and " watchdog" not in command_lower:
                continue
            if _process_cwd(pid) == self.root:
                pids.add(pid)
        return sorted(pids)

    def signal_processes(self, pids: list[int]) -> int:
        """Terminate scheduler processes without failing stop cleanup."""

        signaled = 0
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
                signaled += 1
            except OSError:
                continue
        return signaled

    def tick_once(self, conn: sqlite3.Connection, team: str = "building") -> None:
        """Perform one scheduler pass: resources, requested spawns, liveness, janitor."""

        sample = sample_resources(self.root)
        db.record_resource_sample(conn, sample)
        self.reconcile_missing_tmux_agents(conn)
        self.handle_resource_pressure(conn, sample)
        self.handle_spawn_requests(conn, team)
        reviewed = db.review_ready_cards(conn)
        if reviewed:
            db.log_event(conn, "review", f"Accepted {reviewed} review cards")
        self.repair_control_plane_cards(conn)
        self.requeue_developers_without_cards(conn)
        self.ensure_team(conn, team)
        self.check_agent_liveness(conn)
        self.check_progress_stall(conn)
        self.maybe_run_janitor(conn)

    def effective_team(self, conn: sqlite3.Connection, requested: str) -> str:
        """Choose a resident team without blocking on a global planning phase."""

        if requested != "auto":
            return requested
        return "building"

    def ensure_git_repo(self, conn: sqlite3.Connection) -> None:
        """Initialize git when needed because work lanes rely on branches/worktrees."""

        if (self.root / ".git").exists():
            return
        subprocess.run(["git", "init"], cwd=self.root, check=False, text=True, capture_output=True)
        db.log_event(conn, "git", "Initialized git repository because none existed")

    def check_gh(self, conn: sqlite3.Connection) -> None:
        """Record GitHub CLI availability without blocking local harness startup."""

        try:
            gh = subprocess.run(["gh", "auth", "status"], cwd=self.root, text=True, capture_output=True, check=False)
        except OSError:
            gh = subprocess.CompletedProcess(["gh", "auth", "status"], returncode=127, stdout="", stderr="gh unavailable")
        if gh.returncode == 0:
            db.set_meta(conn, "gh_status", "authorized")
            return
        db.set_meta(conn, "gh_status", "missing_or_unauthorized")
        db.log_event(conn, "warning", "GitHub CLI is missing or unauthorized; continuing without automatic pushes/pages")
        print("\033[31mGitHub CLI is missing or unauthorized; continuing locally.\033[0m", file=sys.stderr)

    def check_project_context(self, conn: sqlite3.Connection) -> None:
        """Create the project context file agents expect before they start."""

        development_md = self.root / "DEVELOPMENT.md"
        if development_md.exists():
            return
        development_md.write_text(DEFAULT_DEVELOPMENT_MD)
        message = "DEVELOPMENT.md was absent, so the harness created a starter one. Edit it with project-specific build/test guidance."
        db.log_event(conn, "project_context", message)
        print(f"\033[33m{message}\033[0m", file=sys.stderr)

    def check_codex_mcp(self, conn: sqlite3.Connection) -> bool:
        """Fail startup if Codex will not expose the harness MCP memory tools."""

        harness = self.harness_executable()
        if not harness.exists():
            return self._mcp_preflight_failed(conn, f"Harness executable not found for MCP: {harness}")
        try:
            server = subprocess.run(
                [str(harness), "--root", str(self.root), "mcp"],
                input='{"jsonrpc":"2.0","id":1,"method":"initialize"}\n{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n',
                cwd=self.root,
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return self._mcp_preflight_failed(conn, f"Harness MCP server did not start: {exc}")
        if server.returncode != 0 or "memory_query" not in server.stdout:
            detail = (server.stderr or server.stdout or "no MCP output").strip()
            return self._mcp_preflight_failed(conn, f"Harness MCP server did not expose memory tools: {detail}")

        try:
            codex = subprocess.run(
                ["codex", *codex_mcp_config_args(self.root, self.paths.db, harness), "mcp", "list"],
                cwd=self.root,
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return self._mcp_preflight_failed(conn, f"Codex MCP preflight failed: {exc}")
        if codex.returncode != 0 or MCP_SERVER_NAME not in codex.stdout:
            detail = (codex.stderr or codex.stdout or "no Codex MCP output").strip()
            return self._mcp_preflight_failed(conn, f"Codex did not accept the harness MCP config: {detail}")
        db.log_event(conn, "mcp", "Harness MCP tools exposed to Codex workers")
        return True

    def _mcp_preflight_failed(self, conn: sqlite3.Connection, message: str) -> bool:
        db.set_meta(conn, "red_banner", "Harness MCP unavailable; refusing to start agents.")
        db.log_event(conn, "mcp_failed", message)
        print(f"\033[31m{message}\033[0m", file=sys.stderr)
        return False

    def harness_executable(self) -> Path:
        """Resolve the harness executable agents should use for MCP stdio."""

        candidates = [
            Path(sys.argv[0]).resolve(),
            self.root / "harness",
            Path(__file__).resolve().parents[1] / "harness",
        ]
        for candidate in candidates:
            if candidate.name == "harness" and candidate.exists():
                return candidate
        return self.root / "harness"

    def ensure_goal(self, conn: sqlite3.Connection, provided: str | None) -> None:
        """Capture the initial goal seed during init, then let Coordinator refine lanes."""

        if db.get_goal(conn) is not None:
            return
        goal = provided or os.environ.get("HARNESS_GOAL") or ""
        if not goal and sys.stdin.isatty():
            goal = input("Describe the goal for this harness run: ").strip()
        if not goal:
            goal = "Goal not captured yet; Coordinator or Goal Planner must ask the user for the real goal."
        measure = "Coordinator must maintain a deterministic success metric and acceptance criteria."
        db.set_goal(conn, goal, measure=measure, status="active", auditor_summary="Auditor/Verifier must prefer deterministic metric evidence over freeform claims.")
        self.write_initial_plan_stub(goal)

    def write_initial_plan_stub(self, goal: str) -> None:
        """Create PLAN.md so restarted agents have a concrete planning artifact."""

        plan = self.root / "PLAN.md"
        if plan.exists():
            return
        plan.write_text(
            "# Plan\n\n"
            f"Goal: {goal}\n\n"
            "Initial backlog seed: Coordinator or Goal Planner must refine this into measurable worklanes without blocking useful development.\n"
        )

    def start_support_windows(self, conn: sqlite3.Connection) -> None:
        """Start manhole, status, updater, and test-loop tmux windows when tmux exists."""

        try:
            session = self.tmux.current_or_create_session(self.root)
        except TmuxUnavailable as exc:
            db.log_event(conn, "warning", str(exc))
            print(f"\033[31m{exc}; agent windows cannot be started.\033[0m", file=sys.stderr)
            return
        db.set_meta(conn, "tmux_session", session)
        attach = f"tmux attach -t {shlex.quote(session)}"
        db.set_meta(conn, "tmux_attach", attach)
        print(f"tmux session: {session} ({attach})", flush=True)

        manhole_prompt = self.paths.prompts / "manhole.md"
        manhole_prompt.write_text(
            prompt_for_role(
                "Manhole",
                "manhole",
                (db.get_goal(conn) or {"text": ""})["text"],
                str(self.paths.db),
                str(self.root),
                extra=(
                    "The user may ask you to inspect tmux panes or harness memory. "
                    "Stay read-only until the user explicitly authorizes a concrete action."
                ),
            )
        )
        manhole_command = build_codex_command(manhole_prompt, self.root, self.root, self.paths.db, self.harness_executable())
        self.tmux.ensure_window(session, "manhole", manhole_command)
        status_command = "watch -c -n 5 ./harness status"
        self.tmux.ensure_window(session, "status", status_command)
        updater_command = "while true; do ./harness update-status; sleep 900; done"
        self.tmux.ensure_window(session, "updater", updater_command)
        integration_command = "while true; do ./harness integrate; sleep 30; done"
        self.tmux.ensure_window(session, "integration", integration_command)
        tests_command = "while true; do ./harness test-loop --once; sleep 5; done"
        self.tmux.ensure_window(session, "tests", tests_command)
        db.log_event(conn, "tmux", "Support windows ready", payload={"session": session, "attach": attach})

    def ensure_team(self, conn: sqlite3.Connection, team: str) -> None:
        """Keep the small resident team alive while respecting integration backpressure."""

        specs = specs_for_team(team)
        for spec in specs:
            active = self.active_agent_count(conn, spec.name)
            target = spec.min_count
            if spec.name == "Developer" and self.integration_backpressure(conn) and self.stabilization_planned_worklanes(conn) <= 0:
                target = min(target, max(1, active))
            if spec.name == "Developer":
                queued = self.queued_developer_worklanes(conn)
                target = min(target, active + queued) if queued else active
            missing = max(0, target - active)
            for _ in range(missing):
                self.spawn_agent(conn, spec.name, title=f"Maintain {spec.name} capacity")

    def queued_developer_worklanes(self, conn: sqlite3.Connection) -> int:
        """Count planned implementation cards without flooding workers with reports."""

        rows = self.planned_developer_candidates(conn)
        product = [row for row in rows if not self.is_report_only_developer_card(row)]
        if product:
            return len(product)
        report_only = [row for row in rows if self.is_report_only_developer_card(row)]
        if report_only and self.active_report_only_developer_cards(conn) < REPORT_ONLY_DEVELOPER_ACTIVE_LIMIT:
            return 1
        return 0

    def planned_developer_candidates(self, conn: sqlite3.Connection) -> list[sqlite3.Row]:
        """Return planned Developer-capacity cards in deterministic claim order."""

        roles = tuple(sorted(db.CODE_PRODUCING_ROLES))
        placeholders = ",".join("?" for _ in roles)
        allow_recovery = self.integration_recovery_slots(conn) > 0
        rows = list(
            conn.execute(
                f"""
                SELECT *
                FROM worklanes
                WHERE stage = 'planned'
                  AND role_type IN ({placeholders})
                ORDER BY priority ASC, id ASC
                """,
                roles,
            ).fetchall()
        )
        if allow_recovery:
            return sorted(rows, key=lambda row: (0 if self.is_integration_recovery_card(row) else 1, int(row["priority"]), int(row["id"])))
        return [row for row in rows if not self.is_integration_recovery_card(row)]

    def is_integration_recovery_card(self, row: sqlite3.Row) -> bool:
        """Return whether a card is an integration-failure resolver."""

        return str(row["source_key"] or "").startswith("integration-failure:") or str(row["title"] or "").startswith("Resolve integration failure for card #")

    def is_report_only_developer_card(self, row: sqlite3.Row) -> bool:
        """Identify old Developer cards that only ask for replay/report evidence."""

        if str(row["role_type"] or "") != "Developer":
            return False
        text = " ".join(str(row[key] or "") for key in ("title", "description", "goal", "acceptance_criteria", "notes", "source_key")).lower()
        return any(marker in text for marker in REPORT_ONLY_CARD_MARKERS)

    def active_report_only_developer_cards(self, conn: sqlite3.Connection) -> int:
        """Count live report-only Developer leases so replay work has a side quota."""

        rows = conn.execute(
            """
            SELECT w.*
            FROM worklanes w
            JOIN agents a ON a.id = w.owner_agent_id
            WHERE w.stage = 'development'
              AND w.status = 'assigned'
              AND a.current_status NOT IN ('crash', 'success', 'stopped')
              AND a.ended_at IS NULL
              AND w.role_type = 'Developer'
            """
        ).fetchall()
        return len([row for row in rows if self.is_report_only_developer_card(row)])

    def integration_recovery_slots(self, conn: sqlite3.Connection) -> int:
        """Return how many more integration-recovery cards may be active."""

        active = int(
            conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM worklanes w
                JOIN agents a ON a.id = w.owner_agent_id
                WHERE w.stage = 'development'
                  AND a.current_status NOT IN ('crash', 'success', 'stopped')
                  AND (
                    w.source_key LIKE 'integration-failure:%'
                    OR w.title LIKE 'Resolve integration failure for card #%'
                  )
                """
            ).fetchone()["count"]
        )
        return max(0, INTEGRATION_RECOVERY_ACTIVE_LIMIT - active)

    def claim_developer_card(self, conn: sqlite3.Connection, agent_name: str, worktree: str, branch: str) -> sqlite3.Row | None:
        """Assign the next Developer card while preserving product-work capacity."""

        rows = self.planned_developer_candidates(conn)
        product = [row for row in rows if not self.is_report_only_developer_card(row)]
        if product:
            return db.assign_card(conn, int(product[0]["id"]), agent_name, worktree, branch)
        if self.active_report_only_developer_cards(conn) >= REPORT_ONLY_DEVELOPER_ACTIVE_LIMIT:
            return None
        report_only = [row for row in rows if self.is_report_only_developer_card(row)]
        if not report_only:
            return None
        return db.assign_card(conn, int(report_only[0]["id"]), agent_name, worktree, branch)

    def stabilization_planned_worklanes(self, conn: sqlite3.Connection) -> int:
        """Count planned repair cards that should still get Developers under backpressure."""

        return int(
            conn.execute(
                """
                SELECT COUNT(*) AS count FROM worklanes
                WHERE stage = 'planned'
                  AND role_type IN ('Developer', 'Designer', 'Conflict Resolver', 'Reproducer')
                  AND (
                    source_key LIKE 'test-failure:%'
                    OR source_key LIKE 'integration-failure:%'
                    OR title LIKE 'Fix failing tests from run %'
                    OR title = 'Fix global test suite failures'
                    OR role_type = 'Conflict Resolver'
                  )
                """
            ).fetchone()["count"]
        )

    def repair_control_plane_cards(self, conn: sqlite3.Connection) -> None:
        """Repair card state left behind by older harness control-plane bugs."""

        self.retire_capacity_cards(conn)
        self.retire_non_actionable_development_cards(conn)
        self.requeue_cards_from_terminal_agents(conn)
        self.dedupe_global_test_failure_cards(conn)
        self.retire_unrecoverable_integration_failures(conn)
        self.dedupe_integration_resolution_cards(conn)

    def retire_unrecoverable_integration_failures(self, conn: sqlite3.Connection) -> int:
        """Close failed integration cards whose branch state is already settled."""

        rows = conn.execute(
            """
            SELECT *
            FROM worklanes
            WHERE stage = 'integration'
              AND status = 'integration_failed'
              AND branch_name != ''
              AND source_key NOT LIKE 'integration-failure:%'
              AND title NOT LIKE 'Resolve integration failure for card #%'
            """
        ).fetchall()
        repaired = 0
        for row in rows:
            state = branch_integration_state(self.root, str(row["branch_name"]))
            if state == "pending":
                continue
            note = ""
            if state == "merged":
                db.complete_card(conn, int(row["id"]), f"{row['branch_name']} is already contained in origin mainline.")
                note = f"Retired because original card#{row['id']} is already integrated."
            elif state == "missing":
                db.retire_card(conn, int(row["id"]), f"Candidate branch is missing: {row['branch_name']}", reason="candidate_missing")
                note = f"Retired because original card#{row['id']} has no candidate branch."
            else:
                continue
            self.retire_resolution_cards_for_target(conn, int(row["id"]), note)
            repaired += 1
        if repaired:
            db.log_event(conn, "integration_queue_repair", f"Settled {repaired} stale integration-failed originals")
        return repaired

    def retire_capacity_cards(self, conn: sqlite3.Connection) -> int:
        """Retire fake Developer capacity cards created by older scheduler versions."""

        now = db.utc_now()
        retired = conn.execute(
            """
            UPDATE worklanes
            SET stage = 'done',
                status = 'stale',
                owner_agent_id = NULL,
                done_at = COALESCE(done_at, ?),
                last_activity_at = ?,
                notes = trim(notes || char(10) || 'Retired obsolete capacity-maintenance card after scheduler repair.')
            WHERE stage != 'done'
              AND role_type = 'Developer'
              AND title = 'Maintain Developer capacity'
            """,
            (now, now),
        ).rowcount
        if retired:
            db.log_event(conn, "card_repair", f"Retired {retired} obsolete Developer capacity cards")
        return int(retired)

    def retire_non_actionable_development_cards(self, conn: sqlite3.Connection) -> int:
        """Retire cards that older releases left assigned after no-op reports."""

        rows = conn.execute(
            """
            SELECT w.id, w.owner_agent_id, a.name AS agent_name, r.status, r.report_json
            FROM worklanes w
            JOIN agent_reports r ON r.id = (
                SELECT r2.id
                FROM agent_reports r2
                WHERE r2.card_id = w.id OR r2.worklane_id = w.id
                ORDER BY r2.id DESC
                LIMIT 1
            )
            LEFT JOIN agents a ON a.id = w.owner_agent_id
            WHERE w.stage = 'development'
            """
        ).fetchall()
        now = db.utc_now()
        for row in rows:
            if not db.is_non_actionable_report_status(str(row["status"])):
                continue
            db.retire_card(conn, int(row["id"]), f"Retired after non-actionable report: {row['status']}", reason=str(row["status"]), agent_name=str(row["agent_name"] or ""))
            if row["agent_name"]:
                conn.execute(
                    "UPDATE agents SET current_status = 'running', ended_at = NULL, last_seen_at = ?, notes = ? WHERE name = ?",
                    (now, f"Awaiting reassignment after non-actionable card#{row['id']}: {row['status']}", row["agent_name"]),
                )
        retired = len([row for row in rows if db.is_non_actionable_report_status(str(row["status"]))])
        if retired:
            db.log_event(conn, "card_repair", f"Retired {retired} non-actionable development cards")
        return retired

    def requeue_cards_from_terminal_agents(self, conn: sqlite3.Connection) -> int:
        """Return development cards owned by terminal workers to planned."""

        placeholders = ",".join("?" for _ in db.AGENT_TERMINAL_STATUSES)
        rows = conn.execute(
            f"""
            SELECT w.id, a.name
            FROM worklanes w
            JOIN agents a ON w.owner_agent_id = a.id
            WHERE w.stage = 'development'
              AND (a.current_status IN ({placeholders}) OR a.ended_at IS NOT NULL)
            """,
            db.AGENT_TERMINAL_STATUSES,
        ).fetchall()
        for row in rows:
            db.requeue_card(conn, int(row["id"]), f"Requeued after terminal worker {row['name']} was found during scheduler repair.")
        if rows:
            db.log_event(conn, "card_repair", f"Requeued {len(rows)} cards owned by terminal workers")
        return len(rows)

    def dedupe_global_test_failure_cards(self, conn: sqlite3.Connection) -> int:
        """Keep one global full-suite stabilization card and retire older duplicates."""

        rows = conn.execute(
            """
            SELECT
                w.id,
                CASE WHEN EXISTS (
                    SELECT 1 FROM agents a
                    WHERE a.current_status NOT IN ('crash', 'success', 'stopped')
                      AND (
                        w.owner_agent_id = a.id
                        OR (a.branch != '' AND w.branch_name = a.branch)
                        OR (a.worktree != '' AND w.worktree_path = a.worktree)
                      )
                ) THEN 1 ELSE 0 END AS active_owner
            FROM worklanes w
            WHERE w.stage != 'done'
              AND w.role_type = 'Developer'
              AND (
                w.source_key LIKE 'test-failure:%:global-suite'
                OR w.title LIKE 'Fix failing tests from run %'
                OR w.title = 'Fix global test suite failures'
              )
            ORDER BY active_owner DESC,
                     CASE w.stage WHEN 'development' THEN 0 WHEN 'planned' THEN 1 ELSE 2 END,
                     w.id DESC
            """
        ).fetchall()
        if len(rows) <= 1:
            return 0
        keep = int(rows[0]["id"])
        stale_ids = [int(row["id"]) for row in rows[1:]]
        now = db.utc_now()
        conn.executemany(
            """
            UPDATE worklanes
            SET stage = 'done',
                status = 'stale',
                owner_agent_id = NULL,
                done_at = COALESCE(done_at, ?),
                last_activity_at = ?,
                notes = trim(notes || char(10) || ?)
            WHERE id = ?
            """,
            [(now, now, f"Superseded by global full-suite stabilization card#{keep}.", card_id) for card_id in stale_ids],
        )
        db.log_event(conn, "card_repair", f"Retired {len(stale_ids)} duplicate global test-failure cards", payload={"kept_card_id": keep, "retired_card_ids": stale_ids[:50]})
        return len(stale_ids)

    def dedupe_integration_resolution_cards(self, conn: sqlite3.Connection) -> int:
        """Keep one active resolver per integration failure and retire competing cards."""

        rows = conn.execute(
            """
            SELECT
                w.*,
                a.name AS agent_name,
                a.role AS agent_role,
                a.current_status AS agent_status
            FROM worklanes w
            LEFT JOIN agents a ON a.id = w.owner_agent_id
            WHERE w.stage != 'done'
              AND (
                w.source_key LIKE 'integration-failure:%'
                OR w.title LIKE 'Resolve integration failure for card #%'
              )
            ORDER BY w.id
            """
        ).fetchall()
        groups: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            key = _integration_resolution_key(row)
            if key:
                groups.setdefault(key, []).append(row)

        retired = 0
        for key, cards in groups.items():
            keep_id: int | None = self.integration_resolution_card_to_keep(cards)
            target_ids = {target_id for row in cards for target_id in [_integration_resolution_target_id(row)] if target_id is not None}
            if len(target_ids) == 1:
                original = conn.execute("SELECT stage, status FROM worklanes WHERE id = ?", (next(iter(target_ids)),)).fetchone()
                if original and (original["stage"] == "done" or original["status"] in {"integrated", "stale", "abandoned", "cancelled"}):
                    keep_id = None
            for row in cards:
                if int(row["id"]) == keep_id:
                    continue
                note = (
                    f"Superseded by integration-resolution card#{keep_id} for {key}."
                    if keep_id is not None
                    else f"Superseded because the original integration target for {key} is already done."
                )
                self.retire_card_and_release_agent(
                    conn,
                    row,
                    note,
                )
                retired += 1
        if retired:
            db.log_event(conn, "card_repair", f"Retired {retired} overlapping integration-resolution cards")
        return retired

    def integration_resolution_card_to_keep(self, cards: list[sqlite3.Row]) -> int | None:
        """Choose the one card that should continue for an integration failure."""

        if not cards:
            return None

        def score(row: sqlite3.Row) -> tuple[int, int, int, int, int]:
            active = 1 if row["agent_status"] and db.is_active_agent_status(str(row["agent_status"])) else 0
            active_resolver = 1 if active and row["agent_role"] == "Conflict Resolver" else 0
            resolver_card = 1 if row["role_type"] == "Conflict Resolver" else 0
            development = 1 if row["stage"] == "development" else 0
            return (active_resolver, active, resolver_card, development, int(row["id"]))

        return int(max(cards, key=score)["id"])

    def retire_card_and_release_agent(self, conn: sqlite3.Connection, row: sqlite3.Row, note: str) -> None:
        """Retire a duplicate card and leave Developers available for new work."""

        agent_name = str(row["agent_name"] or "")
        agent_role = str(row["agent_role"] or "")
        active_agent = bool(row["agent_status"] and db.is_active_agent_status(str(row["agent_status"])))
        db.retire_card(conn, int(row["id"]), note, reason="superseded", agent_name=agent_name)
        if not agent_name or not active_agent:
            return
        now = db.utc_now()
        if agent_role == "Developer":
            conn.execute(
                "UPDATE agents SET current_status = 'running', ended_at = NULL, last_seen_at = ?, notes = ? WHERE name = ?",
                (now, f"Awaiting reassignment after duplicate card#{row['id']} was retired", agent_name),
            )
        else:
            conn.execute(
                "UPDATE agents SET current_status = 'success', ended_at = ?, last_seen_at = ?, notes = ? WHERE name = ?",
                (now, now, f"Completed duplicate card#{row['id']}: superseded", agent_name),
            )

    def retire_resolution_cards_for_target(self, conn: sqlite3.Connection, target_id: int, note: str) -> int:
        """Retire all non-done integration-resolution cards for one original card."""

        rows = conn.execute(
            """
            SELECT
                w.*,
                a.name AS agent_name,
                a.role AS agent_role,
                a.current_status AS agent_status
            FROM worklanes w
            LEFT JOIN agents a ON a.id = w.owner_agent_id
            WHERE w.stage != 'done'
              AND (
                w.source_key LIKE ?
                OR w.title LIKE ?
              )
            """,
            (f"integration-failure:{target_id}:%", f"Resolve integration failure for card #{target_id}:%"),
        ).fetchall()
        for row in rows:
            self.retire_card_and_release_agent(conn, row, note)
        return len(rows)

    def requeue_developers_without_cards(self, conn: sqlite3.Connection) -> None:
        """Reassign idle Developers, and stop panes with ambiguous card ownership."""

        developers = conn.execute(
            "SELECT * FROM agents WHERE role = 'Developer' AND current_status NOT IN ('crash', 'success', 'stopped')"
        ).fetchall()
        for agent in developers:
            rows = conn.execute(
                """
                SELECT id FROM worklanes
                WHERE stage = 'development'
                  AND (
                    owner_agent_id = ?
                    OR (? != '' AND branch_name = ?)
                    OR (? != '' AND worktree_path = ?)
                  )
                """,
                (agent["id"], agent["branch"], agent["branch"], agent["worktree"], agent["worktree"]),
            ).fetchall()
            if len(rows) == 1:
                continue
            if len(rows) == 0:
                lane = self.claim_developer_card(conn, agent["name"], agent["worktree"], agent["branch"])
                if lane is not None:
                    target = _tmux_target(agent)
                    try:
                        if not target:
                            raise RuntimeError("tmux target was not recorded")
                        self.tmux.send_prompt(target, self.card_prompt(conn, int(lane["id"]), lane["title"], ""))
                        db.log_event(
                            conn,
                            "developer_reassigned",
                            f"Reassigned {agent['name']} to card#{lane['id']}",
                            agent_name=agent["name"],
                            payload={"card_id": int(lane["id"]), "worklane_id": int(lane["id"])},
                        )
                        continue
                    except Exception:
                        db.requeue_card(conn, int(lane["id"]), f"Requeued after prompt failed while reassigning {agent['name']}.")
                        db.update_agent_status(conn, agent["name"], "crash", "tmux prompt failed during reassignment", ended=True)
                        db.log_event(conn, "agent_missing", f"{agent['name']} tmux prompt failed during reassignment", agent_name=agent["name"])
                        continue
            db.update_agent_status(conn, agent["name"], "stopped", "Stopped because no assigned development card was found", ended=True)
            target = _tmux_target(agent)
            if target:
                try:
                    self.tmux.kill_window(agent["tmux_session"], agent["tmux_window"])
                except Exception:
                    pass
            db.log_event(conn, "uncarded_agent_stopped", f"Stopped {agent['name']} because it had {len(rows)} development cards", agent_name=agent["name"])

    def developer_spawn_blocker(self, conn: sqlite3.Connection, team: str) -> str:
        """Return why another Developer would be unstable, or an empty string if allowed."""

        queued = self.queued_developer_worklanes(conn)
        if queued <= 0:
            return "no queued Developer worklane is available"
        cap = next((spec.min_count for spec in specs_for_team(team) if spec.name == "Developer"), 0)
        active = self.active_agent_count(conn, "Developer")
        if cap and active >= cap:
            return f"Developer capacity is already full ({active}/{cap})"
        return ""

    def integration_backpressure(self, conn: sqlite3.Connection) -> bool:
        """Detect when integration queues are too full to justify more feature work."""

        placeholders = ",".join("?" for _ in INTEGRATION_READY_STATUSES)
        ready = conn.execute(
            f"SELECT COUNT(*) AS count FROM worklanes WHERE stage = 'integration' AND status IN ({placeholders})",
            INTEGRATION_READY_STATUSES,
        ).fetchone()["count"]
        failed = conn.execute("SELECT COUNT(*) AS count FROM worklanes WHERE status = 'integration_failed'").fetchone()["count"]
        developers = max(1, self.active_agent_count(conn, "Developer"))
        now = time.time()
        if ready > developers or failed > 0:
            since = float(db.get_meta(conn, "integration_backlog_since", "0") or 0)
            if since == 0:
                db.set_meta(conn, "integration_backlog_since", str(now))
                db.set_meta(conn, "integration_backpressure_active", "0")
                return False
            active = failed > 0 or now - since >= INTEGRATION_BACKPRESSURE_SECONDS
            db.set_meta(conn, "integration_backpressure_active", "1" if active else "0")
            return active
        db.set_meta(conn, "integration_backlog_since", "0")
        db.set_meta(conn, "integration_backpressure_active", "0")
        return False

    def active_agent_count(self, conn: sqlite3.Connection, role: str) -> int:
        """Count live agents for capacity, including non-terminal self-reported statuses."""

        active = 0
        for agent in conn.execute("SELECT * FROM agents WHERE role = ?", (role,)):
            if not db.is_active_agent_status(agent["current_status"]):
                continue
            if agent["ended_at"]:
                self.mark_agent_missing(conn, agent, "agent row already ended")
                continue
            target = _tmux_target(agent)
            if not target:
                self.mark_agent_missing(conn, agent, "tmux target was not recorded")
                continue
            if hasattr(self.tmux, "target_exists"):
                if not self.tmux.target_exists(target):
                    self.mark_agent_missing(conn, agent, "tmux pane no longer exists")
                    continue
                active += 1
                continue
            active += 1
        return active

    def handle_spawn_requests(self, conn: sqlite3.Connection, team: str = "building") -> None:
        """Accept MCP spawn requests by starting agents through scheduler-owned code."""

        for request in db.next_spawn_requests(conn):
            role = "Coordinator" if request["role"] == "Manager" else request["role"]
            card_id = int(request["card_id"] or 0)
            if role == "Developer":
                reason = self.developer_spawn_blocker(conn, team)
                if reason:
                    db.mark_spawn_request(conn, request["id"], "rejected", "")
                    db.log_event(conn, "spawn_rejected", f"Rejected Developer spawn request: {reason}", payload={"request_id": request["id"], "title": request["title"]})
                    continue
            existing: sqlite3.Row | None = None
            if role in SINGLETON_SPAWN_ROLES or role == "Coordinator":
                existing = self.running_role_agent(conn, role)
            if existing:
                if self.agent_development_card_count(conn, existing) > 0:
                    db.mark_spawn_request(conn, request["id"], "deferred", existing["name"])
                    db.log_event(conn, "spawn_deferred", f"Deferred {role} work because {existing['name']} already has a card", agent_name=existing["name"], payload={"request_id": request["id"], "card_id": card_id})
                    continue
                if card_id:
                    db.assign_card(conn, card_id, existing["name"])
                try:
                    self.tmux.send_prompt(
                        existing["tmux_pane"],
                        self.card_prompt(conn, card_id, request["title"], request["prompt"]),
                    )
                except Exception:
                    self.requeue_agent_cards(conn, existing, "tmux prompt failed")
                    db.update_agent_status(conn, existing["name"], "crash", "tmux prompt failed", ended=True)
                    db.log_event(conn, "agent_missing", f"{existing['name']} tmux prompt failed", agent_name=existing["name"])
                    db.mark_spawn_request(conn, request["id"], "failed", existing["name"])
                    continue
                db.mark_spawn_request(conn, request["id"], "started", existing["name"])
                db.log_event(conn, "spawn_coalesced", f"Reused {existing['name']} for {role}: {request['title']}", agent_name=existing["name"], payload={"card_id": card_id})
                continue
            name = self.spawn_agent(conn, role, request["title"], extra=request["prompt"], card_id=card_id or None)
            db.mark_spawn_request(conn, request["id"], "started" if name else "failed", name or "")

    def running_role_agent(self, conn: sqlite3.Connection, role: str) -> sqlite3.Row | None:
        """Return one reachable active agent for a singleton role."""

        agents = [
            agent
            for agent in conn.execute("SELECT * FROM agents WHERE role = ? ORDER BY id", (role,))
            if db.is_active_agent_status(agent["current_status"]) and not agent["ended_at"]
        ]
        for agent in agents:
            target = _tmux_target(agent)
            if not target:
                self.mark_agent_missing(conn, agent, "tmux target was not recorded")
                continue
            if hasattr(self.tmux, "target_exists") and not self.tmux.target_exists(target):
                self.mark_agent_missing(conn, agent, "tmux pane no longer exists")
                continue
            return agent
        return None

    def agent_development_card_count(self, conn: sqlite3.Connection, agent: sqlite3.Row) -> int:
        """Count development cards attached to an active agent."""

        return int(
            conn.execute(
                """
                SELECT COUNT(*) AS count FROM worklanes
                WHERE stage = 'development'
                  AND (
                    owner_agent_id = ?
                    OR (? != '' AND branch_name = ?)
                    OR (? != '' AND worktree_path = ?)
                  )
                """,
                (agent["id"], agent["branch"], agent["branch"], agent["worktree"], agent["worktree"]),
            ).fetchone()["count"]
        )

    def requeue_agent_cards(self, conn: sqlite3.Connection, agent: sqlite3.Row, reason: str) -> int:
        """Return cards owned by a dead worker to planned so Python can reassign them."""

        rows = conn.execute(
            """
            SELECT id FROM worklanes
            WHERE stage = 'development'
              AND (
                owner_agent_id = ?
                OR (? != '' AND branch_name = ?)
                OR (? != '' AND worktree_path = ?)
              )
            """,
            (agent["id"], agent["branch"], agent["branch"], agent["worktree"], agent["worktree"]),
        ).fetchall()
        for row in rows:
            db.requeue_card(conn, int(row["id"]), f"Requeued after {agent['name']} ended without an accepted report: {reason}")
        if rows:
            db.log_event(conn, "card_requeued", f"Requeued {len(rows)} cards from {agent['name']}", agent_name=agent["name"], payload={"reason": reason})
        return len(rows)

    def mark_agent_missing(self, conn: sqlite3.Connection, agent: sqlite3.Row, reason: str) -> None:
        """Mark a supposedly active worker dead when Python cannot reach its tmux pane."""

        self.requeue_agent_cards(conn, agent, reason)
        db.update_agent_status(conn, agent["name"], "crash", reason, ended=True)
        self.kill_agent_window(agent)
        db.log_event(conn, "agent_missing", f"{agent['name']} {reason}", agent_name=agent["name"])

    def reconcile_missing_tmux_agents(self, conn: sqlite3.Connection) -> int:
        """Crash active DB agents whose recorded tmux target no longer exists."""

        repaired = 0
        for agent in db.list_agents(conn):
            target = _tmux_target(agent)
            if agent["ended_at"] or not db.is_active_agent_status(agent["current_status"]):
                if agent["ended_at"] and db.is_active_agent_status(agent["current_status"]):
                    self.mark_agent_missing(conn, agent, "agent row already ended")
                    repaired += 1
                elif target and self.target_exists(target):
                    self.kill_agent_window(agent)
                    db.log_event(conn, "terminal_agent_window_closed", f"Closed terminal {agent['name']} tmux window", agent_name=agent["name"])
                    repaired += 1
                continue
            if not target:
                self.mark_agent_missing(conn, agent, "tmux target was not recorded")
                repaired += 1
                continue
            if not self.target_exists(target):
                self.mark_agent_missing(conn, agent, "tmux pane no longer exists")
                repaired += 1
        repaired += self.reconcile_singleton_agent_duplicates(conn)
        return repaired

    def reconcile_singleton_agent_duplicates(self, conn: sqlite3.Connection) -> int:
        """Keep one Coordinator/Manager control-plane worker and stop duplicates."""

        rows = [
            agent
            for agent in conn.execute(
                """
                SELECT * FROM agents
                WHERE role IN ('Coordinator', 'Manager')
                  AND ended_at IS NULL
                ORDER BY id
                """
            )
            if db.is_active_agent_status(agent["current_status"]) and _tmux_target(agent) and self.target_exists(_tmux_target(agent))
        ]
        if len(rows) <= 1:
            return 0
        kept = rows[0]
        stopped = 0
        for agent in rows[1:]:
            self.requeue_agent_cards(conn, agent, f"duplicate singleton worker; kept {kept['name']}")
            db.update_agent_status(conn, agent["name"], "stopped", f"Stopped duplicate Coordinator; kept {kept['name']}", ended=True)
            self.kill_agent_window(agent)
            stopped += 1
        if stopped:
            db.log_event(conn, "duplicate_singleton_stopped", f"Stopped {stopped} duplicate Coordinator workers", payload={"kept": kept["name"]})
        return stopped

    def target_exists(self, target: str) -> bool:
        """Return whether a tmux target is reachable, treating tmux errors as absent."""

        try:
            return self.tmux.target_exists(target) if hasattr(self.tmux, "target_exists") else True
        except Exception:
            return False

    def kill_agent_window(self, agent: sqlite3.Row) -> None:
        """Best-effort close of one harness-owned agent window."""

        session = str(agent["tmux_session"] or "")
        window = str(agent["tmux_window"] or "")
        if not session or not window:
            return
        try:
            self.tmux.kill_window(session, window)
        except Exception:
            return

    def card_prompt(self, conn: sqlite3.Connection, card_id: int, title: str, prompt: str) -> str:
        """Build the bounded prompt for a concrete card-backed assignment."""

        if card_id:
            card = conn.execute("SELECT * FROM worklanes WHERE id = ?", (card_id,)).fetchone()
            if card:
                return (
                    f"Assigned card #{card['id']}: {card['title']}\n"
                    f"Stage: {card['stage']}\n"
                    f"Goal: {card['goal'] or card['description'] or card['notes'] or prompt}\n"
                    f"Acceptance criteria: {card['acceptance_criteria'] or 'Produce structured evidence and report stage/status.'}\n"
                    "Report with agent_report including card_id, worklane_id, stage, status, summary, evidence, and next_action."
                )
        return f"Assigned card-backed work: {title}\n\n{prompt}"

    def prompt_running_role(self, conn: sqlite3.Connection, role: str, message: str) -> str:
        """Deliver work to one reachable active agent for singleton specialist roles."""

        agents = [
            agent
            for agent in conn.execute("SELECT * FROM agents WHERE role = ? ORDER BY id", (role,))
            if db.is_active_agent_status(agent["current_status"])
        ]
        for agent in agents:
            target = _tmux_target(agent)
            if not target:
                continue
            if hasattr(self.tmux, "target_exists") and not self.tmux.target_exists(target):
                db.update_agent_status(conn, agent["name"], "crash", "tmux pane no longer exists", ended=True)
                db.log_event(conn, "agent_missing", f"{agent['name']} tmux pane no longer exists", agent_name=agent["name"])
                continue
            try:
                self.tmux.send_prompt(target, message)
                return str(agent["name"])
            except Exception:
                self.requeue_agent_cards(conn, agent, "tmux prompt failed")
                db.update_agent_status(conn, agent["name"], "crash", "tmux prompt failed", ended=True)
                db.log_event(conn, "agent_missing", f"{agent['name']} tmux prompt failed", agent_name=agent["name"])
        return ""

    def spawn_agent(self, conn: sqlite3.Connection, role: str, title: str, extra: str = "", card_id: int | None = None) -> str:
        """Create a Codex tmux window and agent row, using worktrees for developers."""

        if role == "Developer" and card_id is None and self.queued_developer_worklanes(conn) <= 0:
            db.log_event(conn, "spawn_rejected", "Rejected Developer spawn because no planned implementation card is available", payload={"role": role, "title": title})
            return ""
        if role not in SUPPORT_ROLES and role != "Developer" and card_id is None and not (role == "Coordinator" and title.startswith("Maintain ")):
            card_id = db.create_card(
                conn,
                title,
                role_type=role,
                description=extra,
                priority=50,
                integration_required=db.role_requires_integration(role),
                source_key=f"spawn-direct:{role}:{title}",
            )

        session = db.get_meta(conn, "tmux_session")
        if not session:
            try:
                session = self.tmux.current_or_create_session(self.root)
                db.set_meta(conn, "tmux_session", session)
            except TmuxUnavailable as exc:
                db.log_event(conn, "spawn_failed", str(exc), payload={"role": role, "title": title})
                return ""

        index = conn.execute("SELECT COUNT(*) AS count FROM agents WHERE role = ?", (role,)).fetchone()["count"] + 1
        name = f"{slug_role(role)}-{index}"
        cwd, branch = self.agent_cwd(role, name)
        goal = db.get_goal(conn)
        goal_text = goal["text"] if goal else ""
        assignment = self.card_prompt(conn, card_id, title, extra) if card_id else f"Assigned work: {title}\n\n{extra}"
        prompt = prompt_for_role(role, name, goal_text, str(self.paths.db), str(self.root), extra=assignment)
        prompt_file = self.paths.prompts / f"{name}.md"
        prompt_file.write_text(prompt)
        window = name[:40]
        command = build_codex_command(prompt_file, cwd, self.root, self.paths.db, self.harness_executable())
        try:
            pane = self.tmux.ensure_window(session, window, command)
        except Exception as exc:  # tmux can fail independently of the scheduler.
            db.log_event(conn, "spawn_failed", f"Failed to start {name}: {exc}", agent_name=name)
            return ""
        db.upsert_agent(
            conn,
            name=name,
            role=role,
            current_status="running",
            tmux_session=pane.session,
            tmux_window=pane.window,
            tmux_pane=pane.pane,
            cwd=str(cwd),
            worktree=str(cwd) if role == "Developer" else "",
            branch=branch,
            notes=title,
        )
        if role == "Developer":
            lane = db.assign_card(conn, card_id, name, str(cwd), branch) if card_id else self.claim_developer_card(conn, name, str(cwd), branch)
            if lane is None:
                db.update_agent_status(conn, name, "stopped", "Stopped because no planned implementation card was available", ended=True)
                return ""
            db.record_worktree(conn, str(cwd), branch=branch, owner_agent=name, worklane_id=lane["id"] if lane else None, base_commit=self.current_head())
            if lane:
                try:
                    self.tmux.send_prompt(
                        pane.pane,
                        self.card_prompt(conn, int(lane["id"]), lane["title"], extra),
                    )
                except Exception:
                    pass
        elif role not in SUPPORT_ROLES and card_id:
            db.assign_card(conn, card_id, name)
        db.log_event(conn, "agent_started", f"Started {name} for {title}", agent_name=name, payload={"role": role, "window": window})
        return name

    def agent_cwd(self, role: str, name: str) -> tuple[Path, str]:
        """Use dedicated git worktrees for developers when HEAD exists."""

        if role != "Developer" or not self.has_head():
            return self.root, ""
        branch = f"work/{name}"
        worktree = self.paths.worktrees / name
        if worktree.exists():
            return worktree, branch
        result = subprocess.run(
            ["git", "worktree", "add", "-B", branch, str(worktree), "HEAD"],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            return self.root, ""
        return worktree, branch

    def has_head(self) -> bool:
        """Return whether git has an initial commit to base worktrees on."""

        result = subprocess.run(["git", "rev-parse", "--verify", "HEAD"], cwd=self.root, text=True, capture_output=True, check=False)
        return result.returncode == 0

    def current_head(self) -> str:
        """Return the current base commit for worktree bookkeeping."""

        result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.root, text=True, capture_output=True, check=False)
        return result.stdout.strip() if result.returncode == 0 else ""

    def check_agent_liveness(self, conn: sqlite3.Connection) -> None:
        """Detect crashed, idle, or suspicious agents and route correction prompts."""

        agents = [
            agent
            for agent in db.list_agents(conn)
            if db.is_active_agent_status(agent["current_status"])
        ]
        now = time.time()
        suspicious_agents: list[str] = []
        idle_agents: list[str] = []
        for agent in agents:
            target = _tmux_target(agent)
            if not target:
                self.mark_agent_missing(conn, agent, "tmux target was not recorded")
                continue
            if hasattr(self.tmux, "target_exists") and not self.tmux.target_exists(target):
                self.mark_agent_missing(conn, agent, "tmux pane no longer exists")
                continue
            pane_text = ""
            try:
                pane_text = self.tmux.capture(target, lines=120)
            except Exception:
                pane_text = ""
            if "sleep 3600" in pane_text or "sleep 600" in pane_text or "sleep infinity" in pane_text:
                suspicious_agents.append(agent["name"])
            last_seen = _parse_epoch(agent["last_seen_at"])
            if last_seen and now - last_seen > IDLE_SECONDS:
                last_prompt = _parse_epoch(agent["last_prompt_at"])
                if not last_prompt or now - last_prompt > IDLE_PROMPT_SECONDS:
                    idle_agents.append(agent["name"])
                    conn.execute("UPDATE agents SET last_prompt_at = ? WHERE name = ?", (db.utc_now(), agent["name"]))
        conn.commit()
        if suspicious_agents:
            self.prompt_auditor(conn, f"Investigate suspicious sleep commands in {_agent_list(suspicious_agents)} and get them back to measurable work.")
        if idle_agents:
            self.prompt_auditor(conn, f"{len(idle_agents)} agents appear idle for more than {IDLE_SECONDS // 60} minutes: {_agent_list(idle_agents)}. Diagnose and force progress toward the metric.")

    def check_progress_stall(self, conn: sqlite3.Connection) -> None:
        """Raise a visible alert if the progress metric has not increased in 30 minutes."""

        metric = db.latest_metric(conn)
        if metric is None:
            return
        percent = float(metric["percent_ready"])
        best = float(db.get_meta(conn, "best_progress_percent", "0") or 0)
        now = time.time()
        if percent > best:
            db.set_meta(conn, "best_progress_percent", str(percent))
            db.set_meta(conn, "last_progress_increase_epoch", str(now))
            db.set_meta(conn, "red_banner", "")
            return
        last_increase = float(db.get_meta(conn, "last_progress_increase_epoch", "0") or 0)
        if last_increase == 0:
            db.set_meta(conn, "last_progress_increase_epoch", str(now))
            return
        last_alert = float(db.get_meta(conn, "last_progress_stall_alert", "0") or 0)
        if now - last_increase >= 30 * 60 and now - last_alert >= 30 * 60:
            banner = "PROGRESS STALLED: metric has not increased for 30 minutes."
            db.set_meta(conn, "red_banner", banner)
            db.set_meta(conn, "last_progress_stall_alert", str(now))
            db.log_event(conn, "progress_stalled", banner, payload={"percent_ready": percent})
            self.prompt_coordinator(conn, banner + " Reorganize work so progress resumes.")

    def prompt_auditor(self, conn: sqlite3.Connection, message: str) -> None:
        """Ask a reachable auditor to intervene, or start one alert auditor."""

        card_id = db.find_or_create_card(
            conn,
            source_key=f"auditor-alert:{message[:120]}",
            title="Investigate scheduler alert",
            role_type="Auditor",
            description=message,
            priority=10,
            integration_required=False,
        )
        auditors = [
            agent
            for agent in conn.execute("SELECT * FROM agents WHERE role = 'Auditor' ORDER BY id")
            if db.is_active_agent_status(agent["current_status"])
        ]
        for auditor in auditors:
            target = _tmux_target(auditor)
            if not target:
                continue
            if hasattr(self.tmux, "target_exists") and not self.tmux.target_exists(target):
                self.requeue_agent_cards(conn, auditor, "tmux pane no longer exists")
                db.update_agent_status(conn, auditor["name"], "crash", "tmux pane no longer exists", ended=True)
                db.log_event(conn, "agent_missing", f"{auditor['name']} tmux pane no longer exists", agent_name=auditor["name"])
                continue
            if self.agent_development_card_count(conn, auditor) > 0:
                db.log_event(conn, "auditor_prompt_deferred", message, agent_name=auditor["name"], payload={"card_id": card_id})
                return
            db.assign_card(conn, card_id, auditor["name"])
            try:
                self.tmux.send_prompt(target, self.card_prompt(conn, card_id, "Investigate scheduler alert", message))
                db.log_event(conn, "auditor_prompt", message, agent_name=auditor["name"], payload={"card_id": card_id})
                return
            except Exception:
                self.requeue_agent_cards(conn, auditor, "tmux prompt failed")
                db.update_agent_status(conn, auditor["name"], "crash", "tmux prompt failed", ended=True)
                db.log_event(conn, "agent_missing", f"{auditor['name']} tmux prompt failed", agent_name=auditor["name"])
        last_spawn = float(db.get_meta(conn, "last_auditor_spawn_epoch", "0") or 0)
        now = time.time()
        if now - last_spawn < AUDITOR_SPAWN_SECONDS:
            db.log_event(conn, "auditor_prompt_throttled", message)
            return
        db.set_meta(conn, "last_auditor_spawn_epoch", str(now))
        self.spawn_agent(conn, "Auditor", "Investigate scheduler alert", extra=message, card_id=card_id)

    def handle_resource_pressure(self, conn: sqlite3.Connection, sample: dict[str, Any]) -> None:
        """Escalate sustained low/high resource use to Manager or Janitor."""

        cpu = float(sample.get("cpu_percent", 0))
        ram = float(sample.get("ram_percent", 0))
        now = time.time()
        if cpu < 60 and ram < 60:
            since = float(db.get_meta(conn, "low_resource_since", "0") or 0)
            last_prompt = float(db.get_meta(conn, "last_low_resource_prompt", "0") or 0)
            if since == 0:
                db.set_meta(conn, "low_resource_since", str(now))
            elif now - since >= LOW_RESOURCE_SECONDS and now - last_prompt >= 5 * 60:
                if self.integration_backpressure(conn):
                    self.prompt_coordinator(conn, "CPU and RAM are underused, but integration is backed up. Add integration or conflict-resolution support instead of more feature Developers.")
                else:
                    self.prompt_coordinator(conn, "CPU and RAM have stayed below 60% and integration is healthy; consider increasing useful concurrency.")
                db.set_meta(conn, "last_low_resource_prompt", str(now))
        else:
            db.set_meta(conn, "low_resource_since", "0")
        if cpu >= 95 or ram >= 95:
            since = float(db.get_meta(conn, "high_resource_since", "0") or 0)
            if since == 0:
                db.set_meta(conn, "high_resource_since", str(now))
            elif now - since >= HIGH_RESOURCE_SECONDS:
                killed = self.kill_problematic_codex(sample)
                run_janitor(conn, self.root)
                db.log_event(
                    conn,
                    "resource_warning",
                    "CPU or RAM stayed around 95%+; killed problematic Codex process and ran Janitor" if killed else "CPU or RAM stayed around 95%+; ran Janitor",
                    payload={**sample, "killed_pid": killed},
                )
                db.set_meta(conn, "high_resource_since", str(now))
        else:
            db.set_meta(conn, "high_resource_since", "0")

    def prompt_coordinator(self, conn: sqlite3.Connection, message: str) -> None:
        """Ask the Coordinator to reorganize work when deterministic monitors fire."""

        card_id = db.find_or_create_card(
            conn,
            source_key=f"coordinator-alert:{message[:120]}",
            title="Respond to scheduler alert",
            role_type="Coordinator",
            description=message,
            priority=10,
            integration_required=False,
        )
        coordinator = next(
            (
                agent
                for agent in conn.execute("SELECT * FROM agents WHERE role IN ('Coordinator', 'Manager') ORDER BY CASE role WHEN 'Coordinator' THEN 0 ELSE 1 END, id")
                if db.is_active_agent_status(agent["current_status"])
            ),
            None,
        )
        if coordinator and coordinator["tmux_pane"]:
            if self.agent_development_card_count(conn, coordinator) > 0:
                db.log_event(conn, "coordinator_prompt_deferred", message, agent_name=coordinator["name"], payload={"card_id": card_id})
                return
            db.assign_card(conn, card_id, coordinator["name"])
            try:
                self.tmux.send_prompt(coordinator["tmux_pane"], self.card_prompt(conn, card_id, "Respond to scheduler alert", message))
                db.log_event(conn, "coordinator_prompt", message, agent_name=coordinator["name"], payload={"card_id": card_id})
                return
            except Exception:
                self.requeue_agent_cards(conn, coordinator, "tmux prompt failed")
                db.update_agent_status(conn, coordinator["name"], "crash", "tmux prompt failed", ended=True)
                db.log_event(conn, "agent_missing", f"{coordinator['name']} tmux prompt failed", agent_name=coordinator["name"])
        self.spawn_agent(conn, "Coordinator", "Respond to scheduler alert", extra=message, card_id=card_id)

    def prompt_manager(self, conn: sqlite3.Connection, message: str) -> None:
        """Compatibility wrapper for older tests and queued Manager requests."""

        self.prompt_coordinator(conn, message)

    def kill_problematic_codex(self, sample: dict[str, Any]) -> int:
        """Terminate the hottest Codex process when sustained resource pressure is unsafe."""

        for process in sample.get("processes", []):
            command = str(process.get("command", "")).lower()
            pid = int(process.get("pid", 0) or 0)
            if pid > 0 and "codex" in command:
                try:
                    os.kill(pid, signal.SIGTERM)
                    return pid
                except OSError:
                    return 0
        return 0

    def maybe_run_janitor(self, conn: sqlite3.Connection) -> None:
        """Run deterministic cleanup at startup and at least once per hour."""

        last = float(db.get_meta(conn, "last_janitor_epoch", "0") or 0)
        now = time.time()
        if now - last >= JANITOR_SECONDS:
            run_janitor(conn, self.root)
            db.set_meta(conn, "last_janitor_epoch", str(now))
            conn.commit()

    def poke(self, message: str, target: str = "broadcast") -> None:
        """Queue and deliver a user prompt, then leave status rendering to the CLI."""

        with db.connect(self.paths.db) as conn:
            db.init_db(conn)
            message_id = db.queue_message(conn, message, target)
            delivered = 0
            if target == "broadcast":
                agents = [
                    agent
                    for agent in db.list_agents(conn)
                    if db.is_active_agent_status(agent["current_status"])
                ]
            else:
                agents = list(conn.execute("SELECT * FROM agents WHERE name = ? OR role = ?", (target, target)))
            for agent in agents:
                if not agent["tmux_pane"]:
                    continue
                try:
                    self.tmux.send_prompt(agent["tmux_pane"], message)
                    delivered += 1
                except Exception:
                    continue
            db.mark_message(conn, message_id, "delivered" if delivered else "queued")
            db.log_event(conn, "poke_delivered", f"Delivered poke to {delivered} agents", payload={"target": target})


def _parse_epoch(value: str) -> float:
    """Convert the UTC ISO strings in SQLite to epoch seconds for liveness checks."""

    from datetime import datetime

    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _agent_list(names: list[str], limit: int = 10) -> str:
    """Format a bounded agent list for one batched scheduler alert."""

    shown = names[:limit]
    suffix = f", and {len(names) - limit} more" if len(names) > limit else ""
    return ", ".join(shown) + suffix


def _integration_resolution_key(row: sqlite3.Row) -> str:
    """Return the deterministic overlap key for an integration-resolution card."""

    description = str(row["description"] or "")
    branch = INTEGRATION_BRANCH_RE.search(description)
    if branch:
        return f"branch:{branch.group(1)}"
    source_key = str(row["source_key"] or "")
    if source_key.startswith("integration-failure:"):
        parts = source_key.split(":", 2)
        if len(parts) >= 2 and parts[1]:
            return f"card:{parts[1]}"
    for value in (row["title"], description, row["notes"]):
        match = INTEGRATION_RESOLUTION_CARD_RE.search(str(value or ""))
        if match:
            return f"card:{match.group(1)}"
    return ""


def _integration_resolution_target_id(row: sqlite3.Row) -> int | None:
    """Extract the original card id an integration-resolution card is about."""

    source_key = str(row["source_key"] or "")
    if source_key.startswith("integration-failure:"):
        parts = source_key.split(":", 2)
        if len(parts) >= 2 and parts[1].isdigit():
            return int(parts[1])
    for value in (row["title"], row["description"], row["notes"]):
        match = INTEGRATION_RESOLUTION_CARD_RE.search(str(value or ""))
        if match:
            return int(match.group(1))
    return None


def _tmux_target(agent: sqlite3.Row) -> str:
    """Resolve an agent row to a concrete tmux target, if one was recorded."""

    if agent["tmux_pane"]:
        return str(agent["tmux_pane"])
    if agent["tmux_session"] and agent["tmux_window"]:
        return f"{agent['tmux_session']}:{agent['tmux_window']}"
    return ""


def _process_rows() -> list[tuple[int, str]]:
    """Read process ids and commands for conservative stop-process matching."""

    try:
        result = subprocess.run(["ps", "-eo", "pid=,command="], text=True, capture_output=True, check=False)
    except OSError:
        return []
    rows: list[tuple[int, str]] = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(maxsplit=1)
        if not parts:
            continue
        try:
            rows.append((int(parts[0]), parts[1] if len(parts) > 1 else ""))
        except ValueError:
            continue
    return rows


def _process_cwd(pid: int) -> Path | None:
    """Resolve a process cwd on Linux; return None when unavailable."""

    try:
        return Path(f"/proc/{pid}/cwd").resolve()
    except OSError:
        return None


def _path_belongs_to_root(path: str, root: Path) -> bool:
    """Return whether a tmux pane path is inside this harness repository."""

    if not path:
        return False
    try:
        Path(path).resolve().relative_to(root)
        return True
    except (OSError, ValueError):
        return False


def watchdog_loop(root: str | Path, once: bool = False) -> int:
    """Keep the scheduler restartable from service managers such as systemd."""

    scheduler = HarnessScheduler(root)
    while True:
        if _harness_is_stopped(scheduler.paths.db):
            if once:
                return 0
            time.sleep(10)
            continue
        code = scheduler.run(once=True)
        if once:
            return code
        time.sleep(10)


def _harness_is_stopped(db_path: Path) -> bool:
    """Return whether ./harness stop intentionally paused watchdog restarts."""

    try:
        with db.connect(db_path) as conn:
            db.init_db(conn)
            return db.get_meta(conn, "harness_stopped") == "1"
    except sqlite3.Error:
        return False


def watchdog_service(root: str | Path) -> str:
    """Generate a systemd user unit that restarts the harness watchdog."""

    harness = Path(root).resolve() / "harness"
    return f"""[Unit]
Description=LLM Harness watchdog
After=network-online.target

[Service]
Type=simple
WorkingDirectory={Path(root).resolve()}
ExecStart={harness} watchdog
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
"""


def nixos_service(root: str | Path) -> str:
    """Generate a NixOS-compatible service snippet for the same watchdog loop."""

    harness = Path(root).resolve() / "harness"
    return f"""systemd.user.services.llm-harness-watchdog = {{
  Unit.Description = \"LLM Harness watchdog\";
  Service = {{
    WorkingDirectory = \"{Path(root).resolve()}\";
    ExecStart = \"{harness} watchdog\";
    Restart = \"always\";
    RestartSec = 5;
  }};
  Install.WantedBy = [ \"default.target\" ];
}};
"""
