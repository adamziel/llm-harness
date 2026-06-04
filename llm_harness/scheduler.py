"""Deterministic scheduler that owns Codex worker process lifecycle."""

from __future__ import annotations

import os
import signal
import shlex
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import db
from .codex import build_codex_command
from .janitor import run_janitor
from .resources import sample_resources
from .roles import prompt_for_role, slug_role, specs_for_team
from .status import refresh_reports
from .tmux import Tmux, TmuxUnavailable, shell_command

IDLE_SECONDS = 5 * 60
JANITOR_SECONDS = 60 * 60
LOW_RESOURCE_SECONDS = 60
HIGH_RESOURCE_SECONDS = 30


class HarnessScheduler:
    """Own the durable event loop instead of trusting worker agents to self-manage."""

    def __init__(self, root: str | Path = ".", tmux: Tmux | None = None):
        self.root = Path(root).resolve()
        self.paths = db.bootstrap(self.root)
        self.tmux = tmux or Tmux()

    def run(self, goal: str | None = None, team: str = "auto", once: bool = False) -> int:
        """Start or resume the harness, then keep monitoring worker state."""

        with db.connect(self.paths.db) as conn:
            db.init_db(conn)
            self.ensure_git_repo(conn)
            self.check_gh(conn)
            self.ensure_goal(conn, goal)
            self.start_support_windows(conn)
            refresh_reports(conn, self.root)
            effective_team = self.effective_team(conn, team)
            db.log_event(conn, "scheduler", f"Harness run started with team preset {effective_team}")

        if once:
            with db.connect(self.paths.db) as conn:
                db.init_db(conn)
                self.tick_once(conn, self.effective_team(conn, team))
                refresh_reports(conn, self.root)
            return 0

        print("Harness scheduler running. Press Ctrl-C to stop; workers remain inspectable in tmux.", flush=True)
        try:
            while True:
                with db.connect(self.paths.db) as conn:
                    db.init_db(conn)
                    self.tick_once(conn, self.effective_team(conn, team))
                    refresh_reports(conn, self.root)
                time.sleep(5)
        except KeyboardInterrupt:
            print("Harness scheduler stopped by user; agent tmux windows remain available.", flush=True)
            with db.connect(self.paths.db) as conn:
                db.log_event(conn, "scheduler", "Harness scheduler stopped by user")
            return 130

    def tick_once(self, conn: sqlite3.Connection, team: str = "building") -> None:
        """Perform one scheduler pass: resources, requested spawns, liveness, janitor."""

        sample = sample_resources(self.root)
        db.record_resource_sample(conn, sample)
        self.handle_resource_pressure(conn, sample)
        self.handle_spawn_requests(conn)
        self.ensure_team(conn, team)
        self.check_agent_liveness(conn)
        self.check_progress_stall(conn)
        self.maybe_run_janitor(conn)

    def effective_team(self, conn: sqlite3.Connection, requested: str) -> str:
        """Keep first runs in planning until the goal has a real metric and plan."""

        if requested != "auto":
            return requested
        goal = db.get_goal(conn)
        if goal is None or goal["status"] == "planning":
            return "planning"
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

    def ensure_goal(self, conn: sqlite3.Connection, provided: str | None) -> None:
        """Capture the goal once and leave refinement to the Goal Planner agent."""

        if db.get_goal(conn) is not None:
            return
        goal = provided or os.environ.get("HARNESS_GOAL") or ""
        if not goal and sys.stdin.isatty():
            goal = input("Describe the goal for this harness run: ").strip()
        if not goal:
            goal = "Goal not captured yet; Goal Planner must ask the user for the real goal."
        measure = "Goal Planner must define a deterministic success metric before building."
        db.set_goal(conn, goal, measure=measure, status="planning", auditor_summary="Auditor must verify metric quality before build work is accepted.")
        self.write_initial_plan_stub(goal)

    def write_initial_plan_stub(self, goal: str) -> None:
        """Create PLAN.md so restarted agents have a concrete planning artifact."""

        plan = self.root / "PLAN.md"
        if plan.exists():
            return
        plan.write_text(
            "# Plan\n\n"
            f"Goal: {goal}\n\n"
            "The Goal Planner must refine this into milestones, a deterministic success metric, and work lanes.\n"
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
                "Manager",
                "manhole",
                (db.get_goal(conn) or {"text": ""})["text"],
                str(self.paths.db),
                str(self.root),
                extra=(
                    "You are the user's manhole session. You may inspect tmux panes, "
                    "route corrections through ./harness poke, request agents through MCP, "
                    "and help the user course-correct any part of the harness."
                ),
            )
        )
        manhole_command = build_codex_command(manhole_prompt, self.root)
        self.tmux.ensure_window(session, "manhole", manhole_command)
        status_command = "watch -n 5 ./harness status"
        self.tmux.ensure_window(session, "status", status_command)
        updater_command = "while true; do ./harness update-status; sleep 900; done"
        self.tmux.ensure_window(session, "updater", updater_command)
        tests_command = "while true; do ./harness test-loop --once; sleep 900; done"
        self.tmux.ensure_window(session, "tests", tests_command)
        self.tmux.switch_to(session, "status")
        db.log_event(conn, "tmux", "Support windows ready", payload={"session": session, "attach": attach})

    def ensure_team(self, conn: sqlite3.Connection, team: str) -> None:
        """Keep at least the preset minimum number of live agents per role."""

        specs = specs_for_team(team)
        for spec in specs:
            running = conn.execute(
                "SELECT COUNT(*) AS count FROM agents WHERE role = ? AND current_status = 'running'",
                (spec.name,),
            ).fetchone()["count"]
            missing = max(0, spec.min_count - int(running))
            for _ in range(missing):
                self.spawn_agent(conn, spec.name, title=f"Maintain {spec.name} capacity")

    def handle_spawn_requests(self, conn: sqlite3.Connection) -> None:
        """Accept MCP spawn requests by starting agents through scheduler-owned code."""

        for request in db.next_spawn_requests(conn):
            name = self.spawn_agent(conn, request["role"], request["title"], extra=request["prompt"])
            db.mark_spawn_request(conn, request["id"], "started" if name else "failed", name or "")

    def spawn_agent(self, conn: sqlite3.Connection, role: str, title: str, extra: str = "") -> str:
        """Create a Codex tmux window and agent row, using worktrees for developers."""

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
        prompt = prompt_for_role(role, name, goal_text, str(self.paths.db), str(self.root), extra=f"Assigned work: {title}\n\n{extra}")
        prompt_file = self.paths.prompts / f"{name}.md"
        prompt_file.write_text(prompt)
        window = name[:40]
        command = build_codex_command(prompt_file, cwd)
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

    def check_agent_liveness(self, conn: sqlite3.Connection) -> None:
        """Detect crashed, idle, or suspicious agents and route correction prompts."""

        agents = db.list_agents(conn, "running")
        now = time.time()
        for agent in agents:
            target = agent["tmux_pane"] or f"{agent['tmux_session']}:{agent['tmux_window']}"
            pane_text = ""
            if target:
                try:
                    pane_text = self.tmux.capture(target, lines=120)
                except Exception:
                    pane_text = ""
            if "sleep 3600" in pane_text or "sleep 600" in pane_text or "sleep infinity" in pane_text:
                self.prompt_auditor(conn, f"Investigate suspicious sleep command in {agent['name']} and get it back to measurable work.")
            last_seen = _parse_epoch(agent["last_seen_at"])
            if last_seen and now - last_seen > IDLE_SECONDS:
                self.prompt_auditor(conn, f"{agent['name']} appears idle for more than 5 minutes. Diagnose and force progress toward the metric.")

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
            self.prompt_manager(conn, banner + " Reorganize work so progress resumes.")

    def prompt_auditor(self, conn: sqlite3.Connection, message: str) -> None:
        """Ask an existing auditor to intervene, or start one if none is running."""

        auditor = conn.execute("SELECT * FROM agents WHERE role = 'Auditor' AND current_status = 'running' ORDER BY id LIMIT 1").fetchone()
        if auditor and auditor["tmux_pane"]:
            try:
                self.tmux.send_prompt(auditor["tmux_pane"], message)
                db.log_event(conn, "auditor_prompt", message, agent_name=auditor["name"])
                return
            except Exception:
                pass
        self.spawn_agent(conn, "Auditor", "Investigate scheduler alert", extra=message)

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
                self.prompt_manager(conn, "CPU and RAM have stayed below 60%; consider more work-intense Codex sessions or more lanes.")
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

    def prompt_manager(self, conn: sqlite3.Connection, message: str) -> None:
        """Ask the Manager to reorganize work when deterministic monitors fire."""

        manager = conn.execute("SELECT * FROM agents WHERE role = 'Manager' AND current_status = 'running' ORDER BY id LIMIT 1").fetchone()
        if manager and manager["tmux_pane"]:
            try:
                self.tmux.send_prompt(manager["tmux_pane"], message)
                db.log_event(conn, "manager_prompt", message, agent_name=manager["name"])
                return
            except Exception:
                pass
        self.spawn_agent(conn, "Manager", "Respond to scheduler resource alert", extra=message)

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
                agents = db.list_agents(conn, "running")
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


def watchdog_loop(root: str | Path, once: bool = False) -> int:
    """Keep the scheduler restartable from service managers such as systemd."""

    scheduler = HarnessScheduler(root)
    while True:
        code = scheduler.run(once=True)
        if once:
            return code
        time.sleep(10)


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
