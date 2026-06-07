from __future__ import annotations

import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from llm_harness import __version__, db
from llm_harness.cli import main
from llm_harness.codex import CODEX_MODEL, CODEX_REASONING_EFFORT, build_codex_command
from llm_harness import integration as integration_mod
from llm_harness.integration import integrate_once
from llm_harness.mcp_server import HarnessMCP, serve
from llm_harness.roles import developer_count_for_building, specs_for_team
from llm_harness.scheduler import AUDITOR_SPAWN_SECONDS, IDLE_PROMPT_SECONDS, IDLE_SECONDS, HarnessScheduler, watchdog_loop
from llm_harness.status import collect_status, dashboard, refresh_reports
from llm_harness.testing_loop import discover_test_command, parse_test_output, run_tests_once
from llm_harness.tmux import Tmux, TmuxPane


class FakeTmux:
    def __init__(self):
        self.commands = []
        self.sent = []
        self.killed_windows = []
        self.killed_sessions = []
        self.windows = {}

    def current_or_create_session(self, root):
        return "fake-session"

    def ensure_window(self, session, window, command):
        self.commands.append((session, window, command))
        self.windows.setdefault(session, {})[window] = ""
        return TmuxPane(session, window, f"%{window}")

    def switch_to(self, session, window):
        self.commands.append((session, f"switch:{window}", ""))

    def capture(self, target, lines=200):
        return "working"

    def target_exists(self, target):
        return True

    def send_prompt(self, target, message):
        self.sent.append((target, message))

    def current_session(self):
        return ""

    def list_sessions(self):
        return list(self.windows)

    def list_windows(self, session):
        return self.windows.get(session, {})

    def kill_window(self, session, window):
        self.killed_windows.append((session, window))
        return True

    def kill_session(self, session):
        self.killed_sessions.append(session)
        return True


class HarnessTests(unittest.TestCase):
    def test_tmux_uses_harness_session_even_inside_user_tmux(self):
        calls = []

        def runner(args, check=True, text=True, capture_output=True):
            calls.append(args)
            if args[:2] == ["tmux", "has-session"]:
                return subprocess.CompletedProcess(args, 1, "", "")
            return subprocess.CompletedProcess(args, 0, "", "")

        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.dict(os.environ, {"TMUX": "/tmp/tmux-1000/default,1,0"}),
                mock.patch("llm_harness.tmux.shutil_which", return_value="/usr/bin/tmux"),
            ):
                session = Tmux(runner=runner).current_or_create_session(tmp)

        self.assertTrue(session.startswith("llm-harness-"))
        self.assertTrue(any(call[:3] == ["tmux", "new-session", "-d"] for call in calls))
        self.assertFalse(any("display-message" in call for call in calls))

    def test_db_schema_tracks_required_agent_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                db.set_goal(conn, "Build the harness", measure="tests pass")
                db.upsert_agent(
                    conn,
                    name="developer-1",
                    role="Developer",
                    current_status="running",
                    tmux_session="s",
                    tmux_window="w",
                    tmux_pane="%1",
                    cwd=tmp,
                    worktree=str(Path(tmp) / ".harness" / "worktrees" / "developer-1"),
                    notes="focused lane",
                )
                row = db.list_agents(conn)[0]
                self.assertEqual(row["current_status"], "running")
                self.assertEqual(row["tmux_pane"], "%1")
                self.assertIn("worktrees", row["worktree"])

    def test_db_connect_prefers_turso_mvcc_then_wal(self):
        class FakeCursor:
            def __init__(self, value):
                self.value = value

            def fetchone(self):
                return (self.value,)

        class FakeConnection:
            def __init__(self):
                self.executed = []
                self.row_factory = None
                self.closed = False

            def execute(self, sql):
                self.executed.append(sql)
                if sql == "PRAGMA journal_mode = mvcc":
                    return FakeCursor("delete")
                if sql == "PRAGMA journal_mode = wal":
                    return FakeCursor("wal")
                return FakeCursor("")

            def close(self):
                self.closed = True

        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeConnection()
            with mock.patch.dict("os.environ", {"HARNESS_DB_DRIVER": "sqlite"}), mock.patch("llm_harness.db.sqlite3.connect", return_value=fake):
                with db.connect(Path(tmp) / "missing-parent" / "harness.sqlite3") as conn:
                    self.assertIs(conn, fake)
            self.assertIn("PRAGMA journal_mode = mvcc", fake.executed)
            self.assertIn("PRAGMA journal_mode = wal", fake.executed)
            self.assertTrue(fake.closed)

    def test_db_connect_prefers_pyturso_when_available(self):
        class FakeCursor:
            def __init__(self, value=""):
                self.value = value

            def fetchone(self):
                return (self.value,)

            def fetchall(self):
                return []

        class FakeTursoConnection:
            def __init__(self):
                self.executed = []
                self.row_factory = None
                self.closed = False

            def execute(self, sql):
                self.executed.append(sql)
                if sql == "PRAGMA journal_mode = mvcc":
                    return FakeCursor("mvcc")
                return FakeCursor("")

            def close(self):
                self.closed = True

        FakeTursoConnection.__module__ = "turso"

        class FakeTurso:
            Row = object

            def __init__(self, conn):
                self.conn = conn
                self.paths = []
                self.kwargs = {}

            def connect(self, path, **kwargs):
                self.paths.append(path)
                self.kwargs = kwargs
                return self.conn

        with tempfile.TemporaryDirectory() as tmp:
            fake_conn = FakeTursoConnection()
            fake_turso = FakeTurso(fake_conn)
            with mock.patch.dict("os.environ", {"HARNESS_DB_DRIVER": "auto"}), mock.patch("llm_harness.db._import_turso", return_value=fake_turso), mock.patch("llm_harness.db.sqlite3.connect") as sqlite_connect:
                with db.connect(Path(tmp) / "harness.sqlite3") as conn:
                    self.assertIs(conn, fake_conn)
            sqlite_connect.assert_not_called()
            self.assertEqual(db.connection_driver(fake_conn), "turso")
            self.assertEqual(fake_turso.kwargs["experimental_features"], "views,triggers,generated_columns")
            self.assertEqual(fake_conn.row_factory, FakeTurso.Row)
            self.assertIn("PRAGMA journal_mode = mvcc", fake_conn.executed)
            self.assertNotIn("PRAGMA journal_mode = wal", fake_conn.executed)
            self.assertTrue(fake_conn.closed)

    def test_db_connect_forced_turso_requires_pyturso(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict("os.environ", {"HARNESS_DB_DRIVER": "turso"}), mock.patch("llm_harness.db._import_turso", side_effect=ModuleNotFoundError("turso")):
                with self.assertRaisesRegex(RuntimeError, "pyturso"):
                    with db.connect(Path(tmp) / "harness.sqlite3"):
                        pass

    def test_db_connect_ignores_sqlite_journal_disk_io(self):
        class FakeCursor:
            def __init__(self, value=""):
                self.value = value

            def fetchone(self):
                return (self.value,)

        class FakeConnection:
            def __init__(self):
                self.executed = []
                self.row_factory = None
                self.closed = False

            def execute(self, sql):
                self.executed.append(sql)
                if sql.startswith("PRAGMA journal_mode"):
                    raise sqlite3.OperationalError("disk I/O error")
                return FakeCursor("")

            def close(self):
                self.closed = True

        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeConnection()
            with mock.patch.dict("os.environ", {"HARNESS_DB_DRIVER": "sqlite"}), mock.patch("llm_harness.db.sqlite3.connect", return_value=fake):
                with db.connect(Path(tmp) / "harness.sqlite3") as conn:
                    self.assertIs(conn, fake)
            self.assertIn("PRAGMA journal_mode = mvcc", fake.executed)
            self.assertIn("PRAGMA journal_mode = wal", fake.executed)
            self.assertTrue(fake.closed)

    def test_remove_autoincrement_tables_preserves_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "harness.sqlite3"
            with sqlite3.connect(path) as conn:
                conn.row_factory = sqlite3.Row
                conn.executescript(
                    """
                    CREATE TABLE events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts TEXT NOT NULL,
                        type TEXT NOT NULL,
                        message TEXT NOT NULL,
                        agent_name TEXT,
                        payload_json TEXT NOT NULL DEFAULT '{}'
                    );
                    INSERT INTO events(ts, type, message, payload_json)
                    VALUES ('now', 'old', 'old', '{}');
                    """
                )
                db.remove_autoincrement_tables(conn)
                conn.execute("INSERT INTO events(ts, type, message, payload_json) VALUES ('now', 'new', 'new', '{}')")
                rows = conn.execute("SELECT id, type FROM events ORDER BY id").fetchall()
                schema = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'events'").fetchone()

            self.assertEqual([(row["id"], row["type"]) for row in rows], [(1, "old"), (2, "new")])
            self.assertNotIn("AUTOINCREMENT", schema["sql"])

    def test_turso_conflicts_are_retryable(self):
        class DatabaseError(Exception):
            pass

        DatabaseError.__module__ = "turso.lib"

        self.assertTrue(db.is_retryable_error(DatabaseError("Transaction conflict")))
        self.assertFalse(db.is_retryable_error(DatabaseError("Parse error")))

    def test_forced_turso_missing_prints_clear_cli_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            stderr = io.StringIO()
            with mock.patch.dict("os.environ", {"HARNESS_DB_DRIVER": "turso"}), mock.patch("llm_harness.db._import_turso", side_effect=ModuleNotFoundError("turso")), mock.patch("sys.stderr", stderr):
                self.assertEqual(main(["--root", tmp, "doctor"]), 1)

            self.assertIn("requires the pyturso package", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())

    def test_db_disk_io_prints_clear_cli_error(self):
        stderr = io.StringIO()
        with mock.patch("llm_harness.db.bootstrap", side_effect=sqlite3.OperationalError("disk I/O error")), mock.patch("sys.stderr", stderr):
            self.assertEqual(main(["doctor"]), 1)

        self.assertIn("Harness database disk I/O error", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_db_connect_keeps_turso_mvcc_when_available(self):
        class FakeCursor:
            def __init__(self, value):
                self.value = value

            def fetchone(self):
                return (self.value,)

        class FakeConnection:
            def __init__(self):
                self.executed = []
                self.row_factory = None

            def execute(self, sql):
                self.executed.append(sql)
                if sql == "PRAGMA journal_mode = mvcc":
                    return FakeCursor("mvcc")
                return FakeCursor("")

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeConnection()
            with mock.patch.dict("os.environ", {"HARNESS_DB_DRIVER": "sqlite"}), mock.patch("llm_harness.db.sqlite3.connect", return_value=fake):
                with db.connect(Path(tmp) / "harness.sqlite3"):
                    pass
            self.assertIn("PRAGMA journal_mode = mvcc", fake.executed)
            self.assertNotIn("PRAGMA journal_mode = wal", fake.executed)

    def test_begin_concurrent_starts_turso_mvcc_transaction(self):
        class FakeCursor:
            def __init__(self, value):
                self.value = value

            def fetchone(self):
                return (self.value,)

        class FakeConnection:
            def __init__(self):
                self.executed = []

            def execute(self, sql):
                self.executed.append(sql)
                if sql == "PRAGMA journal_mode":
                    return FakeCursor("mvcc")
                return FakeCursor("")

        fake = FakeConnection()
        self.assertTrue(db.begin_concurrent(fake))  # type: ignore[arg-type]
        self.assertEqual(fake.executed, ["PRAGMA journal_mode", "BEGIN CONCURRENT"])

    def test_codex_command_is_pinned_to_yolo_and_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            prompt = Path(tmp) / "prompt.md"
            prompt.write_text("hello")
            command = build_codex_command(prompt, tmp)
            self.assertIn("codex --yolo", command)
            self.assertIn(f"--model {CODEX_MODEL}", command)
            self.assertIn(f'model_reasoning_effort="{CODEX_REASONING_EFFORT}"', command)
            self.assertIn("mcp_servers.llm-harness.command", command)
            self.assertIn("mcp_servers.llm-harness.args", command)
            self.assertIn(str(prompt), command)

    def test_codex_command_uses_repo_harness_mcp_for_worktrees(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            worktree = root / ".harness" / "worktrees" / "developer-1"
            worktree.mkdir(parents=True)
            prompt = root / ".harness" / "prompts" / "developer-1.md"
            prompt.parent.mkdir(parents=True)
            prompt.write_text("hello")
            command = build_codex_command(prompt, worktree, root, root / ".harness" / "harness.sqlite3", root / "harness")
            root = root.resolve()
            worktree = worktree.resolve()
            self.assertIn(f'mcp_servers.llm-harness.command="{root / "harness"}"', command)
            self.assertIn(f'["--root","{root}","mcp"]', command)
            self.assertNotIn(f'mcp_servers.llm-harness.command="{worktree / "harness"}"', command)

    def test_status_reports_and_dashboard_render_from_sqlite(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                db.set_goal(conn, "Ship measurable work", measure="tests pass")
                db.record_metric(conn, "tests", 3, 4)
                db.record_resource_sample(conn, {"cpu_percent": 12, "ram_percent": 34, "disk_free_gb": 56, "load1": 1})
                worktree = str(Path(tmp) / ".harness" / "worktrees" / "developer-1")
                db.upsert_agent(conn, name="developer-1", role="Developer", current_status="working", cwd=tmp, worktree=worktree, branch="work/developer-1")
                db.upsert_agent(conn, name="developer-2", role="Developer", current_status="crash", cwd=tmp)
                db.upsert_agent(conn, name="developer-3", role="Developer", current_status="stopped", cwd=tmp)
                agent = conn.execute("SELECT id FROM agents WHERE name = 'developer-1'").fetchone()
                lane_id = db.queue_worklane(conn, "Fix parser lowering", status="assigned")
                conn.execute(
                    "UPDATE worklanes SET owner_agent_id = ?, branch_name = ?, worktree_path = ? WHERE id = ?",
                    (agent["id"], "work/developer-1", worktree, lane_id),
                )
                queued_lane_id = db.queue_worklane(conn, "Review queued runtime lane", status="queued")
                ready_lane_id = db.queue_worklane(conn, "Merge finished runtime lane", status="ready_for_integration")
                conn.execute("UPDATE worklanes SET branch_name = ? WHERE id = ?", ("work/developer-99", ready_lane_id))
                conn.commit()
                db.log_event(conn, "note", "status is alive")
                md, html = refresh_reports(conn, tmp)
                self.assertTrue(md.exists())
                self.assertTrue(html.exists())
                self.assertTrue((Path(tmp) / "progress.md").exists())
                self.assertTrue((Path(tmp) / "progress.html").exists())
                text = dashboard(conn)
                self.assertIn("Last generated", text)
                self.assertIn("Progress", text)
                self.assertIn("3 / 4 tests", text)
                self.assertIn("Agents: 1 active, 1 crashed, 3 tracked", text)
                self.assertIn("Active work (agents ↔ cards)", text)
                self.assertIn("developer-1 [Developer/working] → card#", text)
                self.assertIn("Fix parser lowering", text)
                self.assertIn("Unassigned cards", text)
                self.assertIn(f"card#{queued_lane_id} planned/queued/Developer: Review queued runtime lane", text)
                self.assertIn(f"card#{ready_lane_id} integration/ready_for_integration/Developer: Merge finished runtime lane", text)
                self.assertIn("status is alive", text)

    def test_dashboard_warns_when_recorded_scheduler_pid_is_dead(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                db.set_meta(conn, "scheduler_pid", "99999999")
                db.upsert_agent(conn, name="developer-1", role="Developer", current_status="running", tmux_pane="%1", cwd=tmp)
                text = dashboard(conn)

            self.assertIn("HARNESS SCHEDULER DEAD", text)

    def test_dashboard_splits_integration_queue_health(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                with_branch = db.queue_worklane(conn, "Ready with branch", status="ready_for_integration")
                conn.execute("UPDATE worklanes SET branch_name = 'work/ready' WHERE id = ?", (with_branch,))
                db.queue_worklane(conn, "Ready missing branch", status="ready_for_integration")
                original_id = db.queue_worklane(conn, "Failed original", status="integration_failed")
                db.create_card(
                    conn,
                    f"Resolve integration failure for card #{original_id}: queued",
                    role_type="Conflict Resolver",
                    source_key=f"integration-failure:{original_id}:merge_conflicts:work/failed",
                )
                text = dashboard(conn)

            self.assertIn("Integration: ready with branch=1, missing branch=1, failed originals=1, recovery cards=1", text)

    def test_dashboard_shows_metric_count_next_to_progress_percent(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                db.record_metric(conn, "passing checks", 71, 200)
                text = dashboard(conn)

            self.assertIn("35.5%", text)
            self.assertIn("71 / 200 passing checks", text)

    def test_dashboard_strips_terminal_control_sequences_from_db_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                db.log_event(conn, "note", "bad\x1b[200~paste")
                text = dashboard(conn)

            self.assertNotIn("\x1b[200~", text)
            self.assertNotIn("[200~", text)
            self.assertIn("badpaste", text)

    def test_dashboard_correlates_only_current_development_cards_to_active_agents(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            worktree = str(Path(tmp) / ".harness" / "worktrees" / "developer-1")
            branch = "work/developer-1"
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                db.upsert_agent(conn, name="developer-1", role="Developer", current_status="running", cwd=tmp, worktree=worktree, branch=branch)
                active_id = db.queue_worklane(conn, "Current implementation", status="queued")
                db.assign_card(conn, active_id, "developer-1", worktree, branch)
                done_id = db.queue_worklane(conn, "Already integrated duplicate", status="queued")
                db.assign_card(conn, done_id, "developer-1", worktree, branch)
                db.complete_card(conn, done_id, "Already integrated by a newer card.")
                failed_id = db.queue_worklane(conn, "Stale integration failure", status="integration_failed")
                agent = conn.execute("SELECT id FROM agents WHERE name = 'developer-1'").fetchone()
                conn.execute(
                    "UPDATE worklanes SET owner_agent_id = ?, branch_name = ?, worktree_path = ? WHERE id = ?",
                    (agent["id"], branch, worktree, failed_id),
                )
                conn.commit()
                data = collect_status(conn)
                text = dashboard(conn)

            developer_rows = [row for row in data["active_work"] if row["agent_name"] == "developer-1"]
            self.assertEqual([row["lane_id"] for row in developer_rows], [active_id])
            self.assertIn(f"developer-1 [Developer/running] → card#{active_id}", text)
            self.assertNotIn(f"developer-1 [Developer/running] → card#{done_id}", text)
            self.assertNotIn(f"developer-1 [Developer/running] → card#{failed_id}", text)

    def test_status_reconciles_missing_tmux_agents_before_rendering(self):
        class StatusScheduler:
            def __init__(self, root):
                self.root = root

            def reconcile_missing_tmux_agents(self, conn):
                db.update_agent_status(conn, "developer-1", "crash", "tmux pane no longer exists", ended=True)
                return 1

        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                db.upsert_agent(conn, name="developer-1", role="Developer", current_status="running", tmux_pane="%missing", cwd=tmp)

            stdout = io.StringIO()
            with mock.patch("llm_harness.cli.HarnessScheduler", StatusScheduler), mock.patch("sys.stdout", stdout):
                self.assertEqual(main(["--root", tmp, "status"]), 0)

            self.assertIn("Agents: 0 active, 1 crashed, 1 tracked", stdout.getvalue())

    def test_dashboard_warns_when_active_agents_have_no_scheduler_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                db.upsert_agent(conn, name="developer-1", role="Developer", current_status="running", tmux_pane="%1", cwd=tmp)
                text = dashboard(conn)

            self.assertIn("HARNESS SCHEDULER NOT RECORDED", text)

    def test_dashboard_does_not_warn_for_intentionally_stopped_harness(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                db.set_meta(conn, "harness_stopped", "1")
                db.set_meta(conn, "scheduler_pid", "99999999")
                db.upsert_agent(conn, name="developer-1", role="Developer", current_status="running", tmux_pane="%1", cwd=tmp)
                text = dashboard(conn)

            self.assertNotIn("HARNESS SCHEDULER DEAD", text)

    def test_status_update_commits_and_pushes_status_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as remote:
            root = Path(tmp) / "repo"
            root.mkdir()
            subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Harness Test"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "harness@example.test"], cwd=root, check=True)
            subprocess.run(["git", "commit", "--allow-empty", "-m", "Initial"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "init", "--bare", remote], check=True, capture_output=True)
            subprocess.run(["git", "remote", "add", "origin", remote], cwd=root, check=True)
            subprocess.run(["git", "push", "-u", "origin", "main"], cwd=root, check=True, capture_output=True)
            paths = db.bootstrap(root)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                db.set_goal(conn, "Publish status", measure="status committed")
                db.record_metric(conn, "status", 1, 1)
                refresh_reports(conn, root)

            last_subject = subprocess.check_output(["git", "log", "-1", "--pretty=%s"], cwd=root, text=True).strip()
            self.assertEqual(last_subject, "Update harness status")
            remote_status = subprocess.check_output(["git", f"--git-dir={remote}", "show", "main:STATUS.md"], text=True)
            self.assertIn("Publish status", remote_status)
            staged = subprocess.check_output(["git", "diff", "--cached", "--name-only"], cwd=root, text=True).strip()
            self.assertEqual(staged, "")

    def test_dashboard_marks_uncarded_active_specialist_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                db.upsert_agent(
                    conn,
                    name="architect-1",
                    role="Architect",
                    current_status="working",
                    cwd=tmp,
                    tmux_pane="%architect",
                    notes="Investigate repeated failures",
                )
                text = dashboard(conn)
            self.assertIn("Uncarded active work", text)
            self.assertIn("architect-1 [Architect] has no card", text)

    def test_integrate_once_merges_pushes_and_deletes_ready_branch(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as remote:
            root = Path(tmp) / "repo"
            root.mkdir()
            subprocess.run(["git", "init", "-b", "master"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Harness Test"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "harness@example.test"], cwd=root, check=True)
            (root / "compiler.rs").write_text("base\n")
            subprocess.run(["git", "add", "compiler.rs"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "Initial"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "init", "--bare", remote], check=True, capture_output=True)
            subprocess.run(["git", "remote", "add", "origin", remote], cwd=root, check=True)
            subprocess.run(["git", "push", "-u", "origin", "master"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "checkout", "-b", "work/developer-1"], cwd=root, check=True, capture_output=True)
            (root / "compiler.rs").write_text("base\nfeature\n")
            subprocess.run(["git", "commit", "-am", "Add feature"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "push", "-u", "origin", "work/developer-1"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "checkout", "master"], cwd=root, check=True, capture_output=True)

            paths = db.bootstrap(root)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                lane_id = db.queue_worklane(conn, "Integrate feature", status="needs_verification")
                conn.execute("UPDATE worklanes SET branch_name = ? WHERE id = ?", ("work/developer-1", lane_id))
                result = integrate_once(conn, root)
                lane = conn.execute("SELECT * FROM worklanes WHERE id = ?", (lane_id,)).fetchone()
                attempt = conn.execute("SELECT * FROM integration_attempts WHERE worklane_id = ?", (lane_id,)).fetchone()

            self.assertEqual(result["integrated"], 1)
            self.assertEqual(lane["status"], "integrated")
            self.assertEqual(lane["stage"], "done")
            self.assertEqual(attempt["status"], "integrated")
            self.assertEqual(attempt["remote_ref"], "origin/master")
            self.assertTrue(attempt["remote_sha"])
            remote_file = subprocess.check_output(["git", f"--git-dir={remote}", "show", "master:compiler.rs"], text=True)
            self.assertIn("feature", remote_file)
            branch_exists = subprocess.run(["git", f"--git-dir={remote}", "show-ref", "--verify", "refs/heads/work/developer-1"], capture_output=True)
            self.assertNotEqual(branch_exists.returncode, 0)

    def test_integrate_once_failed_push_keeps_card_out_of_done(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as remote:
            root = Path(tmp) / "repo"
            root.mkdir()
            subprocess.run(["git", "init", "-b", "master"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Harness Test"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "harness@example.test"], cwd=root, check=True)
            (root / "compiler.rs").write_text("base\n")
            subprocess.run(["git", "add", "compiler.rs"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "Initial"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "init", "--bare", remote], check=True, capture_output=True)
            subprocess.run(["git", "remote", "add", "origin", remote], cwd=root, check=True)
            subprocess.run(["git", "push", "-u", "origin", "master"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "checkout", "-b", "work/developer-1"], cwd=root, check=True, capture_output=True)
            (root / "compiler.rs").write_text("base\nfeature\n")
            subprocess.run(["git", "commit", "-am", "Add feature"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "push", "-u", "origin", "work/developer-1"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "checkout", "master"], cwd=root, check=True, capture_output=True)
            hook = Path(remote) / "hooks" / "pre-receive"
            hook.write_text("#!/bin/sh\necho rejected >&2\nexit 1\n")
            hook.chmod(0o755)

            paths = db.bootstrap(root)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                lane_id = db.queue_worklane(conn, "Integrate feature", status="ready_for_integration")
                conn.execute("UPDATE worklanes SET branch_name = ? WHERE id = ?", ("work/developer-1", lane_id))
                result = integrate_once(conn, root)
                lane = conn.execute("SELECT * FROM worklanes WHERE id = ?", (lane_id,)).fetchone()
                attempt = conn.execute("SELECT * FROM integration_attempts WHERE worklane_id = ?", (lane_id,)).fetchone()
                conflict_card = conn.execute("SELECT * FROM worklanes WHERE role_type = 'Conflict Resolver'").fetchone()

            self.assertEqual(result["failed"], 1)
            self.assertEqual((lane["stage"], lane["status"]), ("integration", "integration_failed"))
            self.assertEqual(attempt["merge_result"], "push_failed")
            self.assertEqual(attempt["remote_sha"], "")
            self.assertIn("rejected", attempt["push_result"])
            self.assertEqual((conflict_card["stage"], conflict_card["status"]), ("planned", "queued"))
            self.assertIn("push_failed", conflict_card["description"])

    def test_integrate_once_retires_status_only_branch_without_merging(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as remote:
            root = Path(tmp) / "repo"
            root.mkdir()
            subprocess.run(["git", "init", "-b", "master"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Harness Test"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "harness@example.test"], cwd=root, check=True)
            (root / "compiler.rs").write_text("base\n")
            (root / "STATUS.md").write_text("old status\n")
            subprocess.run(["git", "add", "compiler.rs", "STATUS.md"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "Initial"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "init", "--bare", remote], check=True, capture_output=True)
            subprocess.run(["git", "remote", "add", "origin", remote], cwd=root, check=True)
            subprocess.run(["git", "push", "-u", "origin", "master"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "checkout", "-b", "work/status-only"], cwd=root, check=True, capture_output=True)
            (root / "STATUS.md").write_text("new status\n")
            (root / "progress.md").write_text("new progress\n")
            subprocess.run(["git", "add", "STATUS.md", "progress.md"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "Update harness status"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "push", "-u", "origin", "work/status-only"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "checkout", "master"], cwd=root, check=True, capture_output=True)

            paths = db.bootstrap(root)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                lane_id = db.queue_worklane(conn, "Update harness status", status="ready_for_integration")
                conn.execute("UPDATE worklanes SET branch_name = ? WHERE id = ?", ("work/status-only", lane_id))
                result = integrate_once(conn, root)
                lane = conn.execute("SELECT stage, status, notes FROM worklanes WHERE id = ?", (lane_id,)).fetchone()

            subprocess.run(["git", "fetch", "origin"], cwd=root, check=True, capture_output=True)
            log = subprocess.check_output(["git", "log", "--oneline", "origin/master"], cwd=root, text=True)
            self.assertEqual(result["skipped"], 1)
            self.assertEqual((lane["stage"], lane["status"]), ("done", "stale"))
            self.assertIn("report-only", lane["notes"])
            self.assertNotIn("Integrate worklane", log)

    def test_integrate_once_records_conflicts_without_running_full_tests(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as remote:
            root = Path(tmp) / "repo"
            root.mkdir()
            subprocess.run(["git", "init", "-b", "master"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Harness Test"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "harness@example.test"], cwd=root, check=True)
            (root / "compiler.rs").write_text("base\n")
            subprocess.run(["git", "add", "compiler.rs"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "Initial"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "init", "--bare", remote], check=True, capture_output=True)
            subprocess.run(["git", "remote", "add", "origin", remote], cwd=root, check=True)
            subprocess.run(["git", "push", "-u", "origin", "master"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "checkout", "-b", "work/developer-1"], cwd=root, check=True, capture_output=True)
            (root / "compiler.rs").write_text("branch\n")
            subprocess.run(["git", "commit", "-am", "Branch edit"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "push", "-u", "origin", "work/developer-1"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "checkout", "master"], cwd=root, check=True, capture_output=True)
            (root / "compiler.rs").write_text("master\n")
            subprocess.run(["git", "commit", "-am", "Master edit"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "push", "origin", "master"], cwd=root, check=True, capture_output=True)

            paths = db.bootstrap(root)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                lane_id = db.queue_worklane(conn, "Conflicting feature", status="ready_for_integration")
                conn.execute("UPDATE worklanes SET branch_name = ? WHERE id = ?", ("work/developer-1", lane_id))
                result = integrate_once(conn, root)
                lane = conn.execute("SELECT * FROM worklanes WHERE id = ?", (lane_id,)).fetchone()
                attempt = conn.execute("SELECT * FROM integration_attempts WHERE worklane_id = ?", (lane_id,)).fetchone()
                conflict_card = conn.execute("SELECT * FROM worklanes WHERE role_type = 'Conflict Resolver'").fetchone()

            self.assertEqual(result["failed"], 1)
            self.assertEqual(lane["status"], "integration_failed")
            self.assertEqual(lane["stage"], "integration")
            self.assertEqual(attempt["merge_result"], "preflight_merge_conflicts")
            self.assertEqual(json.loads(attempt["tests_json"]), ["git merge-tree"])
            self.assertEqual((conflict_card["stage"], conflict_card["status"]), ("planned", "queued"))
            self.assertIn("merge_conflicts", conflict_card["description"])

    def test_integrate_once_preflights_conflicts_and_keys_recovery_by_branch(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as remote:
            root = Path(tmp) / "repo"
            root.mkdir()
            subprocess.run(["git", "init", "-b", "master"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Harness Test"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "harness@example.test"], cwd=root, check=True)
            (root / "compiler.rs").write_text("base\n")
            subprocess.run(["git", "add", "compiler.rs"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "Initial"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "init", "--bare", remote], check=True, capture_output=True)
            subprocess.run(["git", "remote", "add", "origin", remote], cwd=root, check=True)
            subprocess.run(["git", "push", "-u", "origin", "master"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "checkout", "-b", "work/developer-branch"], cwd=root, check=True, capture_output=True)
            (root / "compiler.rs").write_text("branch\n")
            subprocess.run(["git", "commit", "-am", "Branch edit"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "push", "-u", "origin", "work/developer-branch"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "checkout", "master"], cwd=root, check=True, capture_output=True)
            (root / "compiler.rs").write_text("master\n")
            subprocess.run(["git", "commit", "-am", "Master edit"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "push", "origin", "master"], cwd=root, check=True, capture_output=True)

            paths = db.bootstrap(root)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                lane_id = db.queue_worklane(conn, "Conflicting feature", status="ready_for_integration")
                conn.execute("UPDATE worklanes SET branch_name = ? WHERE id = ?", ("work/developer-branch", lane_id))
                result = integrate_once(conn, root)
                attempt = conn.execute("SELECT * FROM integration_attempts WHERE worklane_id = ?", (lane_id,)).fetchone()
                conflict_card = conn.execute("SELECT * FROM worklanes WHERE role_type = 'Conflict Resolver'").fetchone()

            self.assertEqual(result["failed"], 1)
            self.assertEqual(attempt["merge_result"], "preflight_merge_conflicts")
            self.assertIn("origin/work/developer-branch", conflict_card["source_key"])
            self.assertIn("Branch: work/developer-branch", conflict_card["description"])

    def test_mcp_tools_record_query_spawn_and_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "example.py").write_text("def answer():\n    return 42\n")
            paths = db.bootstrap(root)
            server = HarnessMCP(root, paths.db)
            result = server.call_tool("memory_record_event", {"type": "decision", "message": "use sqlite"})
            payload = json.loads(result["content"][0]["text"])
            self.assertGreater(payload["event_id"], 0)
            rows = server.call_tool("memory_query", {"sql": "SELECT type, message FROM events"})
            self.assertIn("use sqlite", rows["content"][0]["text"])
            spawn = server.call_tool("spawn_agent", {"role": "Developer", "title": "lane", "prompt": "do work"})
            self.assertIn("queued", spawn["content"][0]["text"])
            search = server.call_tool("code_search", {"query": "answer", "refresh": True})
            self.assertIn("example.py", search["content"][0]["text"])

    def test_mcp_memory_query_supports_common_schema_aliases(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            server = HarnessMCP(tmp, paths.db)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                db.log_event(conn, "decision", "inspect aliases", agent_name="developer-1")
                run_id = db.record_test_run(
                    conn,
                    "python -m unittest",
                    "failed",
                    "failure log",
                    summary={"failed": 1},
                    commit_sha="abc123",
                    results=[{"nodeid": "tests/test_x.py::test_a", "status": "failed"}],
                )
                db.note_failing_tests(conn, run_id, "abc123")
                conn.execute("UPDATE bug_reports SET root_cause = 'assertion failed' WHERE test_nodeid = ?", ("tests/test_x.py::test_a",))
                conn.commit()

            queries = [
                "SELECT id, status, command, summary, created_at, updated_at FROM test_runs ORDER BY updated_at DESC LIMIT 20",
                "SELECT id, status, severity, title, notes, created_at, updated_at FROM bug_reports ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END, severity DESC, updated_at DESC LIMIT 30",
                "SELECT id, type, agent_name, message, created_at FROM events ORDER BY id DESC LIMIT 30",
            ]
            for sql in queries:
                result = server.call_tool("memory_query", {"sql": sql})
                text = result["content"][0]["text"]
                self.assertIn("created_at", text)

    def test_init_repairs_setup_without_starting_resident_team(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scheduler = HarnessScheduler(root, tmux=FakeTmux())
            with (
                mock.patch.object(scheduler, "check_gh"),
                mock.patch.object(scheduler, "check_local_tools"),
                mock.patch.object(scheduler, "check_harness_mcp", return_value=True),
                mock.patch.object(scheduler, "initialize_index"),
            ):
                self.assertEqual(scheduler.init_project(goal="Ship refined harness"), 0)
            self.assertTrue((root / ".git").exists())
            self.assertTrue((root / "DEVELOPMENT.md").exists())
            self.assertTrue((root / "PLAN.md").exists())
            self.assertTrue((root / ".harness" / "STATUS_TEMPLATE.md").exists())
            self.assertTrue((root / ".harness" / "prompts" / "roles" / "coordinator.md").exists())
            self.assertEqual(scheduler.tmux.commands, [])
            with db.connect(root / ".harness" / "harness.sqlite3") as conn:
                self.assertTrue(db.get_meta(conn, "initialized_at"))

    def test_run_requires_init_before_resident_team_starts(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeTmux()
            scheduler = HarnessScheduler(tmp, tmux=fake)
            self.assertEqual(scheduler.run(goal="later", once=True), 1)
            self.assertEqual(fake.commands, [])

    def test_refined_schema_has_required_control_plane_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            with db.connect(paths.db) as conn:
                names = {
                    row["name"]
                    for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")
                }
                columns = {row["name"] for row in conn.execute("PRAGMA table_xinfo(worklanes)")}
            for name in {
                "runs",
                "agents",
                "events",
                "worklanes",
                "agent_messages",
                "agent_reports",
                "worktrees",
                "commits",
                "integration_attempts",
                "test_runs",
                "test_results",
                "issues",
                "resource_samples",
                "status_snapshots",
                "settings",
                "card_stage_transitions",
            }:
                self.assertIn(name, names)
            for column in {"card_type", "stage", "review_required", "integration_required", "planned_at", "review_ready_at", "reviewed_at", "done_at", "source_key"}:
                self.assertIn(column, columns)

    def test_init_db_migrates_legacy_worklanes_before_card_indexes(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.paths_for(tmp)
            db.ensure_dirs(paths)
            raw = sqlite3.connect(paths.db)
            raw.executescript(
                """
                CREATE TABLE worklanes (
                    id INTEGER PRIMARY KEY,
                    title TEXT NOT NULL,
                    role_type TEXT NOT NULL DEFAULT 'Developer',
                    priority INTEGER NOT NULL DEFAULT 100,
                    status TEXT NOT NULL DEFAULT 'queued',
                    expected_metric_impact REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    last_activity_at TEXT,
                    notes TEXT NOT NULL DEFAULT ''
                );
                INSERT INTO worklanes(title, status, created_at, last_activity_at)
                VALUES ('legacy lane', 'queued', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00');

                CREATE TABLE agent_reports (
                    id INTEGER PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    agent_name TEXT NOT NULL DEFAULT '',
                    role TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT '',
                    report_json TEXT NOT NULL
                );

                CREATE TABLE spawn_requests (
                    id INTEGER PRIMARY KEY,
                    ts TEXT NOT NULL,
                    requester TEXT NOT NULL DEFAULT '',
                    role TEXT NOT NULL,
                    title TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    agent_name TEXT NOT NULL DEFAULT '',
                    notes TEXT NOT NULL DEFAULT ''
                );
                """
            )
            raw.close()

            with db.connect(paths.db) as conn:
                db.init_db(conn)
                lane = conn.execute("SELECT stage, source_key FROM worklanes WHERE title = 'legacy lane'").fetchone()
                conn.execute("INSERT INTO work_lanes(title, role, status) VALUES ('compat lane', 'Developer', 'queued')")
                compat = conn.execute("SELECT stage FROM worklanes WHERE title = 'compat lane'").fetchone()

            self.assertEqual((lane["stage"], lane["source_key"]), ("planned", ""))
            self.assertEqual(compat["stage"], "planned")

    def test_work_lanes_compat_view_creation_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                with mock.patch("llm_harness.db._sqlite_object_type", return_value=""):
                    db.ensure_worklane_compat(conn)
                    db.ensure_worklane_compat(conn)
                conn.execute("INSERT INTO work_lanes(title, role, status) VALUES ('lane', 'Developer', 'queued')")
                lane = conn.execute("SELECT * FROM worklanes WHERE title = 'lane'").fetchone()
            self.assertEqual(lane["status"], "queued")

    def test_agent_report_moves_development_card_to_review_then_integration(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                lane_id = db.queue_worklane(conn, "Implement focused fix", acceptance_criteria="Focused test passes")
                db.upsert_agent(conn, name="developer-1", role="Developer", current_status="running", cwd=tmp)
                agent = conn.execute("SELECT * FROM agents WHERE name = 'developer-1'").fetchone()
                db.assign_card(conn, lane_id, "developer-1")
            server = HarnessMCP(tmp, paths.db)
            result = server.call_tool(
                "agent_report",
                {
                    "agent_id": "developer-1",
                    "card_id": str(lane_id),
                    "worklane_id": str(lane_id),
                    "stage": "development",
                    "status": "ready_for_review",
                    "summary": "Implemented and tested",
                    "files_changed": ["x.py"],
                    "commits": ["abc"],
                    "tests_run": ["python -m unittest"],
                    "test_result": "pass",
                    "blockers": [],
                    "next_action": "review",
                },
            )
            self.assertIn('"ok": true', result["content"][0]["text"])
            with db.connect(paths.db) as conn:
                lane = conn.execute("SELECT * FROM worklanes WHERE id = ?", (lane_id,)).fetchone()
                self.assertEqual((lane["stage"], lane["status"]), ("review", "needs_verification"))
                self.assertEqual(conn.execute("SELECT COUNT(*) AS count FROM agent_reports").fetchone()["count"], 1)
                moved = db.review_ready_cards(conn)
                lane = conn.execute("SELECT * FROM worklanes WHERE id = ?", (lane_id,)).fetchone()
                self.assertEqual(moved, 1)
                self.assertEqual((lane["stage"], lane["status"]), ("integration", "ready_for_integration"))

    def test_non_code_card_moves_from_review_to_done_without_integration(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                card_id = db.create_card(
                    conn,
                    "Reconsider integration backpressure",
                    role_type="Architect",
                    integration_required=False,
                    acceptance_criteria="Report identifies follow-up cards.",
                )
                db.upsert_agent(conn, name="architect-1", role="Architect", current_status="running", cwd=tmp)
                db.assign_card(conn, card_id, "architect-1")
            server = HarnessMCP(tmp, paths.db)
            server.call_tool(
                "agent_report",
                {
                    "agent_id": "architect-1",
                    "card_id": str(card_id),
                    "worklane_id": str(card_id),
                    "stage": "development",
                    "status": "ready_for_review",
                    "summary": "Advisory report complete",
                    "next_action": "accept",
                },
            )
            with db.connect(paths.db) as conn:
                moved = db.review_ready_cards(conn)
                card = conn.execute("SELECT * FROM worklanes WHERE id = ?", (card_id,)).fetchone()
            self.assertEqual(moved, 1)
            self.assertEqual((card["stage"], card["status"], card["integration_required"]), ("done", "done", 0))

    def test_mcp_activity_status_keeps_agent_lifecycle_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            server = HarnessMCP(tmp, paths.db)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                db.upsert_agent(conn, name="developer-1", role="Developer", current_status="running", cwd=tmp)
            result = server.call_tool("memory_update_agent", {"name": "developer-1", "status": "working", "notes": "claimed lane"})
            self.assertIn('"ok": true', result["content"][0]["text"])
            with db.connect(paths.db) as conn:
                agent = conn.execute("SELECT * FROM agents WHERE name = 'developer-1'").fetchone()
            self.assertEqual(agent["current_status"], "running")
            self.assertEqual(agent["notes"], "working: claimed lane")

    def test_mcp_activity_status_does_not_revive_ended_agents(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            server = HarnessMCP(tmp, paths.db)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                db.upsert_agent(conn, name="developer-1", role="Developer", current_status="running", cwd=tmp)
                db.update_agent_status(conn, "developer-1", "crash", "tmux pane no longer exists", ended=True)

            result = server.call_tool("memory_update_agent", {"name": "developer-1", "status": "working", "notes": "late heartbeat"})
            self.assertIn('"ok": false', result["content"][0]["text"])
            with db.connect(paths.db) as conn:
                agent = conn.execute("SELECT * FROM agents WHERE name = 'developer-1'").fetchone()

            self.assertEqual(agent["current_status"], "crash")
            self.assertIn("tmux pane no longer exists", agent["notes"])
            self.assertTrue(agent["ended_at"])

    def test_stdio_mcp_initialize_and_tools_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            stdin = io.StringIO('{"jsonrpc":"2.0","id":1,"method":"initialize"}\n{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n')
            stdout = io.StringIO()
            serve(tmp, paths.db, stdin=stdin, stdout=stdout)
            output = stdout.getvalue()
            self.assertIn("protocolVersion", output)
            self.assertIn(__version__, output)
            self.assertIn("memory_query", output)

    def test_public_help_only_lists_requested_commands(self):
        root = Path(__file__).resolve().parents[1]
        completed = subprocess.run([sys.executable, str(root / "harness"), "--help"], text=True, capture_output=True, check=True)
        self.assertIn("{init,run,status,stop,reset-counters,poke,doctor,logs,lanes,agents}", completed.stdout)
        self.assertNotIn("test-loop", completed.stdout)
        self.assertNotIn("update-status", completed.stdout)
        self.assertNotIn("integrate", completed.stdout)
        self.assertNotIn("mcp-config", completed.stdout)

    def test_agents_command_includes_card_attachment(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                db.upsert_agent(conn, name="developer-1", role="Developer", current_status="running", cwd=tmp, branch="work/developer-1")
                card_id = db.queue_worklane(conn, "Card-backed work")
                db.assign_card(conn, card_id, "developer-1", branch="work/developer-1")
            root = Path(__file__).resolve().parents[1]
            completed = subprocess.run([sys.executable, str(root / "harness"), "--root", tmp, "agents"], text=True, capture_output=True, check=True)
            agents = json.loads(completed.stdout)
            self.assertEqual(agents[0]["card_id"], card_id)
            self.assertEqual(agents[0]["card_stage"], "development")
            self.assertEqual(agents[0]["card_title"], "Card-backed work")

    def test_version_flag_prints_package_version(self):
        root = Path(__file__).resolve().parents[1]
        completed = subprocess.run([sys.executable, str(root / "harness"), "-v"], text=True, capture_output=True, check=True)
        self.assertEqual(completed.stdout.strip(), f"harness {__version__}")

    def test_package_version_matches_pyproject(self):
        root = Path(__file__).resolve().parents[1]
        match = re.search(r'^version = "([^"]+)"$', (root / "pyproject.toml").read_text(), re.MULTILINE)
        self.assertIsNotNone(match)
        self.assertEqual(__version__, match.group(1))

    def test_building_team_uses_six_developer_cap(self):
        self.assertEqual(developer_count_for_building(8), 6)
        self.assertEqual(developer_count_for_building(6), 6)
        self.assertEqual(developer_count_for_building(1), 1)
        specs = {spec.name: spec.min_count for spec in specs_for_team("building")}
        self.assertEqual(specs["Coordinator"], 1)
        self.assertEqual(specs["Developer"], min(6, __import__("os").cpu_count() or 1))
        self.assertEqual(set(specs), {"Coordinator", "Developer"})

    def test_missing_development_md_is_created_before_agents_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = db.bootstrap(root)
            scheduler = HarnessScheduler(root, tmux=FakeTmux())
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                scheduler.check_project_context(conn)
                event = conn.execute("SELECT * FROM events WHERE type = 'project_context' ORDER BY id DESC LIMIT 1").fetchone()
            self.assertIn("created a starter one", event["message"])
            self.assertIn("# Development Guide", (root / "DEVELOPMENT.md").read_text())

    def test_mcp_preflight_fails_when_harness_executable_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            scheduler = HarnessScheduler(tmp, tmux=FakeTmux())
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                with mock.patch.object(scheduler, "harness_executable", return_value=Path(tmp) / "missing-harness"):
                    self.assertFalse(scheduler.check_codex_mcp(conn))
                self.assertEqual(db.get_meta(conn, "red_banner"), "Harness MCP unavailable; refusing to start agents.")

    def test_scheduler_retries_transient_sqlite_locks(self):
        with tempfile.TemporaryDirectory() as tmp:
            scheduler = HarnessScheduler(tmp, tmux=FakeTmux())
            calls = 0

            def action(conn):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise sqlite3.OperationalError("database is locked")
                return "ok"

            self.assertEqual(scheduler.with_retrying_db("test", action), "ok")
            self.assertEqual(calls, 2)

    def test_scheduler_does_not_retry_disk_io_errors_as_concurrency(self):
        with tempfile.TemporaryDirectory() as tmp:
            scheduler = HarnessScheduler(tmp, tmux=FakeTmux())

            def action(conn):
                raise sqlite3.OperationalError("disk I/O error")

            with self.assertRaises(sqlite3.OperationalError):
                scheduler.with_retrying_db("test", action)

    def test_run_startup_disk_io_prints_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            scheduler = HarnessScheduler(tmp, tmux=FakeTmux())
            stderr = io.StringIO()
            with mock.patch.object(scheduler, "with_retrying_db", side_effect=sqlite3.OperationalError("disk I/O error")), mock.patch("sys.stderr", stderr):
                self.assertEqual(scheduler.run(team="minimal"), 1)

            self.assertIn("Harness database disk I/O error during startup", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())

    def test_testing_loop_records_run_and_parses_failures(self):
        parsed = parse_test_output("tests/test_x.py::test_a PASSED\ntests/test_x.py::test_b FAILED\n")
        self.assertEqual(parsed[1]["status"], "failed")
        unittest_rows = parse_test_output("test_a (tests.test_x.Case.test_a) ... ok\n")
        self.assertEqual(unittest_rows[0]["status"], "passed")
        cargo_rows = parse_test_output("test native_invocation_cleanup::frees_magic_args ... \x1b[31mFAILED\x1b[0m\n")
        self.assertEqual(cargo_rows[0]["nodeid"], "native_invocation_cleanup::frees_magic_args")
        self.assertEqual(cargo_rows[0]["status"], "failed")
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            subprocess.run(["git", "init"], cwd=tmp, check=True, capture_output=True)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                run_id = run_tests_once(conn, tmp, command=[sys.executable, "-c", "print('ok')"])
                row = conn.execute("SELECT * FROM test_runs WHERE id = ?", (run_id,)).fetchone()
                self.assertEqual(row["status"], "passed")

    def test_testing_loop_queues_failure_lane_and_resolves_bug(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                fail_id = db.record_test_run(
                    conn,
                    command="manual",
                    status="failed",
                    full_log="tests/test_x.py::test_a FAILED",
                    results=[{"nodeid": "tests/test_x.py::test_a", "status": "failed"}],
                )
                db.note_failing_tests(conn, fail_id, "bad")
                from llm_harness.testing_loop import queue_test_fix_lane, resolve_fixed_tests

                queue_test_fix_lane(conn, fail_id, [{"nodeid": "tests/test_x.py::test_a", "status": "failed"}], "bad")
                second_fail_id = db.record_test_run(
                    conn,
                    command="manual",
                    status="failed",
                    full_log="tests/test_x.py::test_a FAILED again",
                    results=[{"nodeid": "tests/test_x.py::test_a", "status": "failed"}],
                )
                queue_test_fix_lane(conn, second_fail_id, [{"nodeid": "tests/test_x.py::test_a", "status": "failed"}], "bad")
                self.assertEqual(conn.execute("SELECT COUNT(*) AS count FROM work_lanes").fetchone()["count"], 1)
                resolve_fixed_tests(conn, [{"nodeid": "tests/test_x.py::test_a", "status": "passed"}], "good")
                bug = conn.execute("SELECT * FROM bug_reports WHERE test_nodeid = 'tests/test_x.py::test_a'").fetchone()
                self.assertEqual(bug["status"], "fixed")

    def test_global_test_loop_uses_one_stabilization_card_for_different_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                from llm_harness.testing_loop import queue_test_fix_lane

                first = db.record_test_run(
                    conn,
                    command="python -m unittest discover -s tests -v",
                    status="failed",
                    full_log="tests/test_a.py::test_one FAILED",
                    results=[{"nodeid": "tests/test_a.py::test_one", "status": "failed"}],
                )
                queue_test_fix_lane(conn, first, [{"nodeid": "tests/test_a.py::test_one", "status": "failed"}], "bad")
                second = db.record_test_run(
                    conn,
                    command="python -m unittest discover -s tests -v",
                    status="failed",
                    full_log="tests/test_b.py::test_two FAILED",
                    results=[{"nodeid": "tests/test_b.py::test_two", "status": "failed"}],
                )
                queue_test_fix_lane(conn, second, [{"nodeid": "tests/test_b.py::test_two", "status": "failed"}], "bad")
                cards = conn.execute("SELECT * FROM worklanes WHERE status != 'stale'").fetchall()
            self.assertEqual(len(cards), 1)
            self.assertEqual(cards[0]["title"], "Fix global test suite failures")

    def test_global_test_loop_uses_one_stabilization_card_across_full_suite_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                from llm_harness.testing_loop import queue_test_fix_lane

                first = db.record_test_run(
                    conn,
                    command="python -m unittest discover -s tests -v",
                    status="failed",
                    full_log="tests/test_a.py::test_one FAILED",
                    results=[{"nodeid": "tests/test_a.py::test_one", "status": "failed"}],
                )
                queue_test_fix_lane(conn, first, [{"nodeid": "tests/test_a.py::test_one", "status": "failed"}], "bad")
                second = db.record_test_run(
                    conn,
                    command="tools/run-tests.sh",
                    status="failed",
                    full_log="tests/test_b.py::test_two FAILED",
                    results=[{"nodeid": "tests/test_b.py::test_two", "status": "failed"}],
                )
                queue_test_fix_lane(conn, second, [{"nodeid": "tests/test_b.py::test_two", "status": "failed"}], "bad")
                cards = conn.execute("SELECT * FROM worklanes WHERE status != 'stale'").fetchall()
            self.assertEqual(len(cards), 1)
            self.assertEqual(cards[0]["source_key"], "test-failure:global-suite")

    def test_discover_test_command_prefers_repo_run_tests_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tests").mkdir()
            (root / "tools").mkdir()
            script = root / "tools" / "run-tests.sh"
            script.write_text("#!/bin/sh\nexit 0\n")
            script.chmod(0o755)

            self.assertEqual(discover_test_command(root), ["tools/run-tests.sh"])

    def test_test_loop_records_public_phpt_metric_from_full_gate_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                run_tests_once(
                    conn,
                    tmp,
                    command=[
                        sys.executable,
                        "-c",
                        "print('accepted_public_phpt_passes = 7873 / 20294 = 38.79%')",
                    ],
                )
                metric = db.latest_metric(conn)

            self.assertIsNotNone(metric)
            self.assertEqual(metric["metric_name"], "accepted_public_phpt_passes")
            self.assertEqual(metric["value"], 7873)
            self.assertEqual(metric["target"], 20294)

    def test_global_test_loop_throttles_repeated_status_noise(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                from llm_harness.testing_loop import queue_test_fix_lane

                for index in range(3):
                    run_id = db.record_test_run(
                        conn,
                        command="python -m unittest discover -s tests -v",
                        status="failed",
                        full_log=f"tests/test_{index}.py::test_failure FAILED",
                        results=[{"nodeid": f"tests/test_{index}.py::test_failure", "status": "failed"}],
                    )
                    queue_test_fix_lane(conn, run_id, [{"nodeid": f"tests/test_{index}.py::test_failure", "status": "failed"}], "bad")
                dedupe_events = conn.execute("SELECT * FROM events WHERE type = 'worklane_deduplicated'").fetchall()

            self.assertEqual(len(dedupe_events), 1)

    def test_test_loop_throttles_repeated_failure_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                for _ in range(2):
                    run_tests_once(conn, tmp, command=[sys.executable, "-c", "import sys; sys.exit(1)"])
                failure_events = conn.execute("SELECT * FROM events WHERE type = 'tests_failed'").fetchall()

            self.assertEqual(len(failure_events), 1)

    def test_failed_global_gate_with_metric_is_soft_known_red_and_keeps_product_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tools").mkdir()
            script = root / "tools" / "run-tests.sh"
            script.write_text(
                "#!/bin/sh\n"
                "echo 'tests/php_a.phpt FAILED'\n"
                "echo 'tests/php_b.phpt FAILED'\n"
                "echo 'accepted_public_phpt_passes = 9998 / 10000'\n"
                "exit 1\n"
            )
            script.chmod(0o755)
            paths = db.bootstrap(root)
            scheduler = HarnessScheduler(root, tmux=FakeTmux())
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                run_tests_once(conn, root)
                db.create_card(conn, "Add next compatibility slice", role_type="Developer", source_key="feature:next")
                candidates = scheduler.planned_developer_candidates(conn)
                gate_mode = db.get_meta(conn, "test_gate_mode")
                known_failures = json.loads(db.get_meta(conn, "test_gate_failures_json"))
                rendered = dashboard(conn)

            self.assertEqual(gate_mode, "soft_known_red")
            self.assertEqual(known_failures, ["tests/php_a.phpt", "tests/php_b.phpt"])
            self.assertIn("feature:next", [row["source_key"] for row in candidates])
            self.assertIn("Gate: SOFT KNOWN-RED", rendered)

    def test_failed_global_gate_without_metric_is_hard_blocker_and_pauses_feature_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tools").mkdir()
            script = root / "tools" / "run-tests.sh"
            script.write_text("#!/bin/sh\necho 'runtime crashed before per-test rows'\nexit 1\n")
            script.chmod(0o755)
            paths = db.bootstrap(root)
            scheduler = HarnessScheduler(root, tmux=FakeTmux())
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                run_tests_once(conn, root)
                feature_card = db.create_card(conn, "Add next compatibility slice", role_type="Developer", source_key="feature:next")
                gate_card = db.create_card(conn, "Fix metric-producing gate", role_type="Developer", source_key="test-failure:manual-gate", status="ready_for_integration", stage="integration")
                conn.execute("UPDATE worklanes SET branch_name = 'work/feature' WHERE id = ?", (feature_card,))
                conn.execute("UPDATE worklanes SET branch_name = 'work/gate' WHERE id = ?", (gate_card,))
                db.update_worklane_status(conn, feature_card, "ready_for_integration")
                candidates = scheduler.planned_developer_candidates(conn)
                ready_lanes = integration_mod._ready_lanes(conn, 10)
                gate_mode = db.get_meta(conn, "test_gate_mode")
                rendered = dashboard(conn)

            self.assertEqual(gate_mode, "hard_blocker")
            self.assertTrue(candidates)
            self.assertTrue(all(str(row["source_key"]).startswith("test-failure:") for row in candidates))
            self.assertEqual([row["id"] for row in ready_lanes], [gate_card])
            self.assertIn("Gate: HARD BLOCKER", rendered)

    def test_failed_global_gate_with_small_parsed_failures_is_quarantined_known_red(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tools").mkdir()
            script = root / "tools" / "run-tests.sh"
            script.write_text(
                "#!/bin/sh\n"
                "echo 'tests/php_a.phpt FAILED'\n"
                "echo 'tests/php_b.phpt FAILED'\n"
                "exit 1\n"
            )
            script.chmod(0o755)
            paths = db.bootstrap(root)
            scheduler = HarnessScheduler(root, tmux=FakeTmux())
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                run_tests_once(conn, root)
                feature_card = db.create_card(conn, "Add next compatibility slice", role_type="Developer", source_key="feature:next")
                conn.execute("UPDATE worklanes SET branch_name = 'work/feature' WHERE id = ?", (feature_card,))
                db.update_worklane_status(conn, feature_card, "ready_for_integration")
                db.create_card(conn, "Another product slice", role_type="Developer", source_key="feature:planned")
                candidates = scheduler.planned_developer_candidates(conn)
                ready_lanes = integration_mod._ready_lanes(conn, 10)
                gate_mode = db.get_meta(conn, "test_gate_mode")
                known_failures = json.loads(db.get_meta(conn, "test_gate_failures_json"))
                rendered = dashboard(conn)

            self.assertEqual(gate_mode, "quarantined_known_red")
            self.assertEqual(known_failures, ["tests/php_a.phpt", "tests/php_b.phpt"])
            self.assertIn("feature:planned", [row["source_key"] for row in candidates])
            self.assertIn(feature_card, [row["id"] for row in ready_lanes])
            self.assertIn("Gate: KNOWN-RED QUARANTINE", rendered)

    def test_failed_global_gate_with_cargo_failures_is_quarantined_known_red(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tools").mkdir()
            script = root / "tools" / "run-tests.sh"
            script.write_text(
                "#!/bin/sh\n"
                "echo 'test cleanup::method_invocation ... FAILED'\n"
                "echo 'test cleanup::static_invocation ... FAILED'\n"
                "echo 'test result: FAILED. 432 passed; 2 failed; 0 ignored; 0 measured; 0 filtered out'\n"
                "exit 1\n"
            )
            script.chmod(0o755)
            paths = db.bootstrap(root)
            scheduler = HarnessScheduler(root, tmux=FakeTmux())
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                run_tests_once(conn, root)
                db.create_card(conn, "Continue compatibility work", role_type="Developer", source_key="feature:next")
                candidates = scheduler.planned_developer_candidates(conn)
                gate_mode = db.get_meta(conn, "test_gate_mode")
                known_failures = json.loads(db.get_meta(conn, "test_gate_failures_json"))

            self.assertEqual(gate_mode, "quarantined_known_red")
            self.assertEqual(known_failures, ["cleanup::method_invocation", "cleanup::static_invocation"])
            self.assertIn("feature:next", [row["source_key"] for row in candidates])

    def test_quarantined_gate_becomes_hard_when_new_failure_appears(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                first_run = db.record_test_run(
                    conn,
                    command="tools/run-tests.sh",
                    status="failed",
                    full_log="tests/php_a.phpt FAILED",
                    results=[{"nodeid": "tests/php_a.phpt", "status": "failed"}],
                )
                from llm_harness.testing_loop import update_test_gate_state

                update_test_gate_state(conn, first_run, "tools/run-tests.sh", "failed", [{"nodeid": "tests/php_a.phpt", "status": "failed"}], False)
                second_results = [
                    {"nodeid": "tests/php_a.phpt", "status": "failed"},
                    {"nodeid": "tests/php_new.phpt", "status": "failed"},
                ]
                second_run = db.record_test_run(
                    conn,
                    command="tools/run-tests.sh",
                    status="failed",
                    full_log="tests/php_a.phpt FAILED\ntests/php_new.phpt FAILED",
                    results=second_results,
                )
                update_test_gate_state(conn, second_run, "tools/run-tests.sh", "failed", second_results, False)
                gate_mode = db.get_meta(conn, "test_gate_mode")
                reason = db.get_meta(conn, "test_gate_reason")

            self.assertEqual(gate_mode, "hard_blocker")
            self.assertIn("New failures appeared", reason)

    def test_failed_global_gate_requeues_stale_failed_gate_card(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tools").mkdir()
            script = root / "tools" / "run-tests.sh"
            script.write_text("#!/bin/sh\necho 'tests/php_a.phpt FAILED'\nexit 1\n")
            script.chmod(0o755)
            paths = db.bootstrap(root)
            scheduler = HarnessScheduler(root, tmux=FakeTmux())
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                stale_card = db.create_card(
                    conn,
                    "Fix global test suite failures",
                    role_type="Developer",
                    source_key="test-failure:global-suite",
                    status="integration_failed",
                    stage="integration",
                )
                run_tests_once(conn, root)
                card = conn.execute("SELECT stage, status, owner_agent_id FROM worklanes WHERE id = ?", (stale_card,)).fetchone()
                candidates = scheduler.planned_developer_candidates(conn)

            self.assertEqual((card["stage"], card["status"], card["owner_agent_id"]), ("planned", "queued", None))
            self.assertEqual([row["id"] for row in candidates], [stale_card])

    def test_scheduler_repair_retires_bad_capacity_and_duplicate_global_cards(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            scheduler = HarnessScheduler(tmp, tmux=FakeTmux())
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                capacity_id = db.create_card(conn, "Maintain Developer capacity", role_type="Developer", status="integration_failed", stage="integration")
                stale_owner_card = db.queue_worklane(conn, "Requeue terminal owner")
                duplicate_one = db.create_card(conn, "Fix failing tests from run 1", role_type="Developer", source_key="test-failure:python -m unittest discover -s tests -v:old-a")
                duplicate_two = db.create_card(conn, "Fix failing tests from run 2", role_type="Developer", source_key="test-failure:python -m unittest discover -s tests -v:old-b")
                db.upsert_agent(conn, name="developer-1", role="Developer", current_status="crash", cwd=tmp)
                db.assign_card(conn, stale_owner_card, "developer-1")
                scheduler.repair_control_plane_cards(conn)
                capacity = conn.execute("SELECT stage, status FROM worklanes WHERE id = ?", (capacity_id,)).fetchone()
                stale_owner = conn.execute("SELECT stage, status, owner_agent_id FROM worklanes WHERE id = ?", (stale_owner_card,)).fetchone()
                duplicate_states = [dict(row) for row in conn.execute("SELECT id, stage, status FROM worklanes WHERE id IN (?, ?) ORDER BY id", (duplicate_one, duplicate_two))]

            self.assertEqual((capacity["stage"], capacity["status"]), ("done", "stale"))
            self.assertEqual((stale_owner["stage"], stale_owner["status"], stale_owner["owner_agent_id"]), ("planned", "queued", None))
            self.assertEqual(sum(1 for row in duplicate_states if row["status"] != "stale"), 1)

    def test_scheduler_once_starts_interactive_windows_and_supervised_loops(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            paths = db.bootstrap(root)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                db.set_meta(conn, "red_banner", "Harness stopped.")
                db.set_meta(conn, "harness_stopped", "1")
                db.set_meta(conn, "initialized_at", db.utc_now())
                conn.commit()
            fake = FakeTmux()
            scheduler = HarnessScheduler(root, tmux=fake)
            stdout = io.StringIO()
            with mock.patch("sys.stdout", stdout):
                code = scheduler.run(goal="Build something", team="minimal", once=True)
            self.assertEqual(code, 0)
            window_names = {window for _, window, _ in fake.commands}
            self.assertIn("manhole", window_names)
            self.assertIn("status", window_names)
            self.assertNotIn("updater", window_names)
            self.assertNotIn("integration", window_names)
            self.assertNotIn("tests", window_names)
            self.assertNotIn("switch:status", window_names)
            self.assertIn(("fake-session", "status", "watch -c -n 5 ./harness status"), fake.commands)
            codex_commands = [command for _, window, command in fake.commands if window not in {"manhole", "status"} and not window.startswith("switch:")]
            self.assertTrue(codex_commands)
            self.assertTrue(all("--yolo" in command and f"--model {CODEX_MODEL}" in command and f'model_reasoning_effort="{CODEX_REASONING_EFFORT}"' in command for command in codex_commands))
            self.assertIn("Default to supervisor/read-only mode", (root / ".harness" / "prompts" / "manhole.md").read_text())
            output = stdout.getvalue()
            self.assertIn("[scheduler]", output)
            self.assertIn("[status]", output)
            self.assertIn("[integration]", output)
            self.assertIn("[tests]", output)
            with db.connect(root / ".harness" / "harness.sqlite3") as conn:
                agents = db.list_agents(conn)
                self.assertEqual([agent["role"] for agent in agents], ["Coordinator"])
                self.assertEqual(db.get_meta(conn, "red_banner"), "")
                self.assertEqual(db.get_meta(conn, "harness_stopped"), "0")

    def test_supervisor_once_isolates_loop_failures_and_continues(self):
        with tempfile.TemporaryDirectory() as tmp:
            scheduler = HarnessScheduler(tmp, tmux=FakeTmux())
            stdout = io.StringIO()
            with (
                mock.patch.object(scheduler, "scheduler_tick_action", return_value="tick ok") as scheduler_tick,
                mock.patch.object(scheduler, "status_refresh_action", return_value="status ok") as status_refresh,
                mock.patch.object(scheduler, "integration_action", side_effect=RuntimeError("boom")) as integration_action,
                mock.patch.object(scheduler, "test_loop_action", return_value="tests ok") as test_loop,
                mock.patch("sys.stdout", stdout),
            ):
                result = scheduler.run_deterministic_once("minimal")

            self.assertTrue(scheduler_tick.called)
            self.assertTrue(status_refresh.called)
            self.assertTrue(integration_action.called)
            self.assertTrue(test_loop.called)
            self.assertEqual(result["scheduler"], "ok")
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["tests"], "ok")
            self.assertEqual(result["integration"], "error")
            output = stdout.getvalue()
            self.assertIn("[integration] ERROR boom", output)
            self.assertIn("[tests] tests ok", output)

    def test_team_capacity_counts_live_non_terminal_developers(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            fake = FakeTmux()
            scheduler = HarnessScheduler(tmp, tmux=fake)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                db.upsert_agent(conn, name="coordinator-1", role="Coordinator", current_status="working", tmux_pane="%coordinator", cwd=tmp)
                for index, status in enumerate(["working", "idle", "waiting", "running"], start=1):
                    db.upsert_agent(conn, name=f"developer-{index}", role="Developer", current_status=status, tmux_pane=f"%developer-{index}", cwd=tmp)
                db.upsert_agent(conn, name="integrator-1", role="Integrator", current_status="merging", tmux_pane="%integrator", cwd=tmp)
                with mock.patch("llm_harness.roles.os.cpu_count", return_value=4):
                    scheduler.ensure_team(conn, "building")
            spawned_developers = [window for _, window, _ in fake.commands if window.startswith("developer-")]
            self.assertEqual(spawned_developers, [])

    def test_team_capacity_replaces_only_terminal_or_missing_developers(self):
        class MissingOneTmux(FakeTmux):
            def target_exists(self, target):
                return target != "%missing"

        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            fake = MissingOneTmux()
            scheduler = HarnessScheduler(tmp, tmux=fake)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                db.upsert_agent(conn, name="coordinator-1", role="Coordinator", current_status="running", tmux_pane="%coordinator", cwd=tmp)
                db.upsert_agent(conn, name="integrator-1", role="Integrator", current_status="running", tmux_pane="%integrator", cwd=tmp)
                db.upsert_agent(conn, name="developer-1", role="Developer", current_status="working", tmux_pane="%live", cwd=tmp)
                db.upsert_agent(conn, name="developer-2", role="Developer", current_status="success", tmux_pane="%done", cwd=tmp)
                db.upsert_agent(conn, name="developer-3", role="Developer", current_status="working", tmux_pane="%missing", cwd=tmp)
                for index in range(3):
                    db.queue_worklane(conn, f"Queued lane {index}")
                with mock.patch("llm_harness.roles.os.cpu_count", return_value=4):
                    scheduler.ensure_team(conn, "building")
                missing = conn.execute("SELECT current_status FROM agents WHERE name = 'developer-3'").fetchone()["current_status"]
            spawned_developers = [window for _, window, _ in fake.commands if window.startswith("developer-")]
            self.assertEqual(spawned_developers, ["developer-4", "developer-5", "developer-6"])
            self.assertEqual(missing, "crash")

    def test_team_capacity_replaces_active_developer_without_tmux_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            fake = FakeTmux()
            scheduler = HarnessScheduler(tmp, tmux=fake)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                db.upsert_agent(conn, name="coordinator-1", role="Coordinator", current_status="running", tmux_pane="%coordinator", cwd=tmp)
                lane_id = db.queue_worklane(conn, "Recover stale card")
                db.upsert_agent(conn, name="developer-1", role="Developer", current_status="running", cwd=tmp)
                db.assign_card(conn, lane_id, "developer-1")
                db.queue_worklane(conn, "Queued lane")
                scheduler.ensure_team(conn, "building")
                stale = conn.execute("SELECT current_status FROM agents WHERE name = 'developer-1'").fetchone()["current_status"]
                lane = conn.execute("SELECT owner_agent_id, stage FROM worklanes WHERE id = ?", (lane_id,)).fetchone()
                capacity_cards = conn.execute("SELECT COUNT(*) AS count FROM worklanes WHERE title = 'Maintain Developer capacity'").fetchone()["count"]
            spawned_developers = [window for _, window, _ in fake.commands if window.startswith("developer-")]
            self.assertGreaterEqual(len(spawned_developers), 1)
            self.assertEqual(stale, "crash")
            self.assertEqual(lane["stage"], "development")
            self.assertIsNotNone(lane["owner_agent_id"])
            self.assertEqual(capacity_cards, 0)

    def test_team_capacity_does_not_spawn_developers_without_queued_lanes(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            fake = FakeTmux()
            scheduler = HarnessScheduler(tmp, tmux=fake)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                db.upsert_agent(conn, name="coordinator-1", role="Coordinator", current_status="running", tmux_pane="%coordinator", cwd=tmp)
                db.upsert_agent(conn, name="integrator-1", role="Integrator", current_status="running", tmux_pane="%integrator", cwd=tmp)
                scheduler.ensure_team(conn, "building")
            spawned_developers = [window for _, window, _ in fake.commands if window.startswith("developer-")]
            self.assertEqual(spawned_developers, [])

    def test_developer_spawn_requests_create_a_card_before_starting_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            fake = FakeTmux()
            scheduler = HarnessScheduler(tmp, tmux=fake)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                request_id = db.queue_spawn_request(conn, role="Developer", title="extra", prompt="do work")
                scheduler.handle_spawn_requests(conn, "building")
                request = conn.execute("SELECT * FROM spawn_requests WHERE id = ?", (request_id,)).fetchone()
                card = conn.execute("SELECT * FROM worklanes WHERE id = ?", (request["card_id"],)).fetchone()
            spawned_developers = [window for _, window, _ in fake.commands if window.startswith("developer-")]
            self.assertEqual(spawned_developers, ["developer-1"])
            self.assertEqual(request["status"], "started")
            self.assertEqual((card["stage"], card["status"]), ("development", "assigned"))

    def test_developer_spawn_requests_respect_team_capacity(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            fake = FakeTmux()
            scheduler = HarnessScheduler(tmp, tmux=fake)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                for index in range(1, 7):
                    db.upsert_agent(conn, name=f"developer-{index}", role="Developer", current_status="running", tmux_pane=f"%developer-{index}", cwd=tmp)
                db.queue_worklane(conn, "Queued lane")
                request_id = db.queue_spawn_request(conn, role="Developer", title="extra", prompt="do work")
                scheduler.handle_spawn_requests(conn, "building")
                request = conn.execute("SELECT * FROM spawn_requests WHERE id = ?", (request_id,)).fetchone()
                event = conn.execute("SELECT * FROM events WHERE type = 'spawn_rejected' ORDER BY id DESC LIMIT 1").fetchone()
            spawned_developers = [window for _, window, _ in fake.commands if window.startswith("developer-")]
            self.assertEqual(spawned_developers, [])
            self.assertEqual(request["status"], "rejected")
            self.assertIn("capacity is already full", event["message"])

    def test_integration_backpressure_prevents_developer_scaleup(self):
        old = str(__import__("time").time() - 21 * 60)
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            fake = FakeTmux()
            scheduler = HarnessScheduler(tmp, tmux=fake)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                db.upsert_agent(conn, name="coordinator-1", role="Coordinator", current_status="running", tmux_pane="%coordinator", cwd=tmp)
                db.upsert_agent(conn, name="integrator-1", role="Integrator", current_status="running", tmux_pane="%integrator", cwd=tmp)
                db.upsert_agent(conn, name="developer-1", role="Developer", current_status="running", tmux_pane="%developer", cwd=tmp)
                for index in range(3):
                    db.queue_worklane(conn, f"Ready lane {index}", status="ready_for_integration")
                db.set_meta(conn, "integration_backlog_since", old)
                scheduler.ensure_team(conn, "building")
                self.assertEqual(db.get_meta(conn, "integration_backpressure_active"), "1")
            spawned_developers = [window for _, window, _ in fake.commands if window.startswith("developer-")]
            self.assertEqual(spawned_developers, [])

    def test_integration_backpressure_allows_stabilization_developers(self):
        old = str(__import__("time").time() - 21 * 60)
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            fake = FakeTmux()
            scheduler = HarnessScheduler(tmp, tmux=fake)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                db.upsert_agent(conn, name="coordinator-1", role="Coordinator", current_status="running", tmux_pane="%coordinator", cwd=tmp)
                db.upsert_agent(conn, name="developer-1", role="Developer", current_status="running", tmux_pane="%developer-1", cwd=tmp)
                for index in range(3):
                    db.queue_worklane(conn, f"Ready lane {index}", status="ready_for_integration")
                for index in range(5):
                    db.create_card(conn, f"Stabilize failure {index}", role_type="Developer", source_key=f"test-failure:focused:{index}")
                db.set_meta(conn, "integration_backlog_since", old)
                scheduler.ensure_team(conn, "building")
                self.assertEqual(db.get_meta(conn, "integration_backpressure_active"), "1")
            spawned_developers = [window for _, window, _ in fake.commands if window.startswith("developer-")]
            self.assertEqual(spawned_developers, ["developer-2", "developer-3", "developer-4", "developer-5", "developer-6"])

    def test_integration_backpressure_uses_developers_for_conflict_cards(self):
        old = str(__import__("time").time() - 21 * 60)
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            fake = FakeTmux()
            scheduler = HarnessScheduler(tmp, tmux=fake)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                db.upsert_agent(conn, name="developer-1", role="Developer", current_status="running", tmux_pane="%developer-1", cwd=tmp)
                db.queue_worklane(conn, "Failed integration", status="integration_failed")
                for index in range(5):
                    db.create_card(conn, f"Resolve integration failure {index}", role_type="Conflict Resolver", source_key=f"integration-failure:{index}:merge")
                db.set_meta(conn, "integration_backlog_since", old)
                scheduler.ensure_team(conn, "building")
                self.assertEqual(db.get_meta(conn, "integration_backpressure_active"), "1")
                assigned = conn.execute("SELECT COUNT(*) AS count FROM worklanes WHERE stage = 'development'").fetchone()["count"]
            spawned_developers = [window for _, window, _ in fake.commands if window.startswith("developer-")]
            self.assertEqual(spawned_developers, ["developer-2", "developer-3", "developer-4"])
            self.assertEqual(assigned, 3)

    def test_ready_report_releases_current_card_lease_for_reassignment(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            fake = FakeTmux()
            scheduler = HarnessScheduler(tmp, tmux=fake)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                done_card_id = db.queue_worklane(conn, "Finished code card")
                next_card_id = db.queue_worklane(conn, "Next code card")
                db.upsert_agent(conn, name="developer-1", role="Developer", current_status="running", tmux_pane="%developer-1", cwd=tmp)
                db.assign_card(conn, done_card_id, "developer-1")
                db.record_agent_report(
                    conn,
                    {
                        "agent_id": "developer-1",
                        "card_id": done_card_id,
                        "worklane_id": done_card_id,
                        "stage": "development",
                        "status": "ready_for_review",
                        "summary": "Code and tests ready.",
                    },
                )
                scheduler.requeue_developers_without_cards(conn)
                done_card = conn.execute("SELECT stage, status, owner_agent_id FROM worklanes WHERE id = ?", (done_card_id,)).fetchone()
                next_card = conn.execute("SELECT stage, status, owner_agent_id FROM worklanes WHERE id = ?", (next_card_id,)).fetchone()
                agent = conn.execute("SELECT id FROM agents WHERE name = 'developer-1'").fetchone()

            self.assertEqual((done_card["stage"], done_card["status"], done_card["owner_agent_id"]), ("review", "needs_verification", None))
            self.assertEqual((next_card["stage"], next_card["status"], next_card["owner_agent_id"]), ("development", "assigned", agent["id"]))
            self.assertEqual(len(fake.sent), 1)
            self.assertIn(f"Assigned card #{next_card_id}", fake.sent[0][1])

    def test_blocked_duplicate_report_retires_card_and_reassigns_developer(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            fake = FakeTmux()
            scheduler = HarnessScheduler(tmp, tmux=fake)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                duplicate_id = db.queue_worklane(conn, "Duplicate conflict resolver")
                fresh_id = db.queue_worklane(conn, "Fresh implementation")
                db.upsert_agent(conn, name="developer-1", role="Developer", current_status="running", tmux_pane="%developer-1", cwd=tmp)
                db.assign_card(conn, duplicate_id, "developer-1")
                db.record_agent_report(
                    conn,
                    {
                        "agent_id": "developer-1",
                        "card_id": duplicate_id,
                        "worklane_id": duplicate_id,
                        "stage": "development",
                        "status": "blocked",
                        "summary": "Duplicate of canonical live worklane 1795 under developer-103; no source edits.",
                        "next_action": "Assign developer-1 a non-duplicate Developer card.",
                    },
                )
                scheduler.repair_control_plane_cards(conn)
                scheduler.requeue_developers_without_cards(conn)
                duplicate = conn.execute("SELECT stage, status, owner_agent_id FROM worklanes WHERE id = ?", (duplicate_id,)).fetchone()
                fresh = conn.execute("SELECT stage, status, owner_agent_id FROM worklanes WHERE id = ?", (fresh_id,)).fetchone()
                agent = conn.execute("SELECT id, current_status FROM agents WHERE name = 'developer-1'").fetchone()

            self.assertEqual((duplicate["stage"], duplicate["status"], duplicate["owner_agent_id"]), ("done", "stale", None))
            self.assertEqual((fresh["stage"], fresh["status"], fresh["owner_agent_id"]), ("development", "assigned", agent["id"]))
            self.assertEqual(agent["current_status"], "running")
            self.assertEqual(len(fake.sent), 1)
            self.assertIn(f"Assigned card #{fresh_id}", fake.sent[0][1])

    def test_claim_developer_card_prefers_implementation_over_report_only_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            scheduler = HarnessScheduler(tmp, tmux=FakeTmux())
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                report_id = db.queue_worklane(
                    conn,
                    "Focused replay: secondary extension regression rows",
                    priority=0,
                    goal="Read-only replay lane. Write .harness/reports/focused-replay.md. No source edits.",
                )
                product_id = db.queue_worklane(conn, "Implement runtime behavior", priority=100)
                db.upsert_agent(conn, name="developer-1", role="Developer", current_status="running", tmux_pane="%developer-1", cwd=tmp)
                lane = scheduler.claim_developer_card(conn, "developer-1", "", "")
                report = conn.execute("SELECT stage, status FROM worklanes WHERE id = ?", (report_id,)).fetchone()
                product = conn.execute("SELECT stage, status FROM worklanes WHERE id = ?", (product_id,)).fetchone()

            self.assertEqual(lane["id"], product_id)
            self.assertEqual((product["stage"], product["status"]), ("development", "assigned"))
            self.assertEqual((report["stage"], report["status"]), ("planned", "queued"))

    def test_report_only_developer_cards_are_limited_to_one_side_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            scheduler = HarnessScheduler(tmp, tmux=FakeTmux())
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                first = db.queue_worklane(conn, "Focused replay one", goal="Read-only report. No source edits.")
                second = db.queue_worklane(conn, "Focused replay two", goal="Read-only report. No source edits.")
                db.upsert_agent(conn, name="developer-1", role="Developer", current_status="running", tmux_pane="%developer-1", cwd=tmp)
                db.upsert_agent(conn, name="developer-2", role="Developer", current_status="running", tmux_pane="%developer-2", cwd=tmp)
                first_lane = scheduler.claim_developer_card(conn, "developer-1", "", "")
                second_lane = scheduler.claim_developer_card(conn, "developer-2", "", "")
                queued = scheduler.queued_developer_worklanes(conn)
                first_row = conn.execute("SELECT stage FROM worklanes WHERE id = ?", (first,)).fetchone()
                second_row = conn.execute("SELECT stage FROM worklanes WHERE id = ?", (second,)).fetchone()

            self.assertEqual(first_lane["id"], first)
            self.assertIsNone(second_lane)
            self.assertEqual(queued, 0)
            self.assertEqual(first_row["stage"], "development")
            self.assertEqual(second_row["stage"], "planned")

    def test_non_actionable_developer_report_retires_card_and_keeps_worker_reassignable(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                card_id = db.queue_worklane(conn, "Empty stale card")
                db.upsert_agent(conn, name="developer-1", role="Developer", current_status="running", tmux_pane="%developer-1", cwd=tmp)
                db.assign_card(conn, card_id, "developer-1")
                db.record_agent_report(
                    conn,
                    {
                        "agent_id": "developer-1",
                        "card_id": card_id,
                        "worklane_id": card_id,
                        "stage": "development",
                        "status": "reserve_no_source_edits",
                        "summary": "No concrete work was available.",
                    },
                )
                card = conn.execute("SELECT stage, status FROM worklanes WHERE id = ?", (card_id,)).fetchone()
                agent = conn.execute("SELECT current_status FROM agents WHERE name = 'developer-1'").fetchone()
            self.assertEqual((card["stage"], card["status"]), ("done", "stale"))
            self.assertEqual(agent["current_status"], "running")

    def test_non_actionable_status_variant_releases_developer_for_new_card(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            fake = FakeTmux()
            scheduler = HarnessScheduler(tmp, tmux=fake)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                stale_card_id = db.queue_worklane(conn, "Superseded conflict card")
                fresh_card_id = db.queue_worklane(conn, "Fresh non-overlapping card")
                db.upsert_agent(conn, name="developer-1", role="Developer", current_status="running", tmux_pane="%developer-1", cwd=tmp)
                db.assign_card(conn, stale_card_id, "developer-1")
                db.record_agent_report(
                    conn,
                    {
                        "agent_id": "developer-1",
                        "card_id": stale_card_id,
                        "worklane_id": stale_card_id,
                        "stage": "development",
                        "status": "superseded_by_active_resolver_no_source_edits",
                        "summary": "Another resolver owns this work now.",
                    },
                )
                scheduler.requeue_developers_without_cards(conn)
                stale_card = conn.execute("SELECT stage, status FROM worklanes WHERE id = ?", (stale_card_id,)).fetchone()
                fresh_card = conn.execute("SELECT stage, status, owner_agent_id FROM worklanes WHERE id = ?", (fresh_card_id,)).fetchone()
                agent = conn.execute("SELECT id, current_status FROM agents WHERE name = 'developer-1'").fetchone()

            self.assertEqual((stale_card["stage"], stale_card["status"]), ("done", "stale"))
            self.assertEqual((fresh_card["stage"], fresh_card["status"], fresh_card["owner_agent_id"]), ("development", "assigned", agent["id"]))
            self.assertEqual(agent["current_status"], "running")
            self.assertEqual(len(fake.sent), 1)
            self.assertIn(f"Assigned card #{fresh_card_id}", fake.sent[0][1])

    def test_scheduler_retires_overlapping_integration_resolver_cards(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            fake = FakeTmux()
            scheduler = HarnessScheduler(tmp, tmux=fake)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                original_id = db.queue_worklane(conn, "Original branch needing conflict resolution", status="integration_failed")
                competing_id = db.create_card(
                    conn,
                    f"Resolve integration failure for card #{original_id}: stale duplicate",
                    role_type="Developer",
                    source_key=f"integration-failure:{original_id}:merge_conflicts:developer",
                    description="Branch: work/same-conflict\nOld duplicate.",
                )
                active_id = db.create_card(
                    conn,
                    f"Resolve integration failure for card #{original_id}: active resolver",
                    role_type="Conflict Resolver",
                    source_key=f"integration-failure:{original_id}:merge_conflicts:resolver",
                    description="Branch: work/same-conflict\nActive resolver.",
                )
                fresh_id = db.queue_worklane(conn, "Fresh non-overlapping implementation")
                db.upsert_agent(conn, name="developer-1", role="Developer", current_status="running", tmux_pane="%developer-1", cwd=tmp)
                db.upsert_agent(conn, name="conflict-resolver-1", role="Conflict Resolver", current_status="running", tmux_pane="%resolver-1", cwd=tmp)
                db.assign_card(conn, competing_id, "developer-1")
                db.assign_card(conn, active_id, "conflict-resolver-1")

                scheduler.repair_control_plane_cards(conn)
                scheduler.requeue_developers_without_cards(conn)

                competing = conn.execute("SELECT stage, status FROM worklanes WHERE id = ?", (competing_id,)).fetchone()
                active = conn.execute("SELECT stage, status FROM worklanes WHERE id = ?", (active_id,)).fetchone()
                fresh = conn.execute("SELECT stage, status FROM worklanes WHERE id = ?", (fresh_id,)).fetchone()

            self.assertEqual((competing["stage"], competing["status"]), ("done", "stale"))
            self.assertEqual((active["stage"], active["status"]), ("development", "assigned"))
            self.assertEqual((fresh["stage"], fresh["status"]), ("development", "assigned"))
            self.assertEqual(len(fake.sent), 1)
            self.assertIn(f"Assigned card #{fresh_id}", fake.sent[0][1])

    def test_scheduler_retires_resolver_card_for_done_original_card(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            fake = FakeTmux()
            scheduler = HarnessScheduler(tmp, tmux=fake)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                original_id = db.queue_worklane(conn, "Already integrated branch", status="ready_for_integration")
                db.complete_card(conn, original_id, "Already integrated by a newer card.")
                stale_resolver_id = db.create_card(
                    conn,
                    f"Resolve integration failure for card #{original_id}: stale resolver",
                    role_type="Developer",
                    source_key=f"integration-failure:{original_id}:merge_conflicts",
                    description="Branch: work/already-merged\nThis branch is stale.",
                )
                fresh_id = db.queue_worklane(conn, "Fresh independent implementation")
                db.upsert_agent(conn, name="developer-1", role="Developer", current_status="running", tmux_pane="%developer-1", cwd=tmp)
                db.assign_card(conn, stale_resolver_id, "developer-1")

                scheduler.repair_control_plane_cards(conn)
                scheduler.requeue_developers_without_cards(conn)

                stale_resolver = conn.execute("SELECT stage, status FROM worklanes WHERE id = ?", (stale_resolver_id,)).fetchone()
                fresh = conn.execute("SELECT stage, status FROM worklanes WHERE id = ?", (fresh_id,)).fetchone()

            self.assertEqual((stale_resolver["stage"], stale_resolver["status"]), ("done", "stale"))
            self.assertEqual((fresh["stage"], fresh["status"]), ("development", "assigned"))
            self.assertEqual(len(fake.sent), 1)
            self.assertIn(f"Assigned card #{fresh_id}", fake.sent[0][1])

    def test_scheduler_completes_merged_integration_failure_and_retires_recovery_cards(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as remote:
            root = Path(tmp) / "repo"
            root.mkdir()
            subprocess.run(["git", "init", "-b", "master"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Harness Test"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "harness@example.test"], cwd=root, check=True)
            (root / "compiler.rs").write_text("base\n")
            subprocess.run(["git", "add", "compiler.rs"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "Initial"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "init", "--bare", remote], check=True, capture_output=True)
            subprocess.run(["git", "remote", "add", "origin", remote], cwd=root, check=True)
            subprocess.run(["git", "push", "-u", "origin", "master"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "checkout", "-b", "work/already-merged"], cwd=root, check=True, capture_output=True)
            (root / "compiler.rs").write_text("base\nfeature\n")
            subprocess.run(["git", "commit", "-am", "Feature"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "push", "-u", "origin", "work/already-merged"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "checkout", "master"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "merge", "--no-ff", "work/already-merged", "-m", "Merge feature"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "push", "origin", "master"], cwd=root, check=True, capture_output=True)

            paths = db.bootstrap(root)
            scheduler = HarnessScheduler(root, tmux=FakeTmux())
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                original_id = db.queue_worklane(conn, "Already merged integration failure", status="integration_failed")
                conn.execute("UPDATE worklanes SET branch_name = ? WHERE id = ?", ("work/already-merged", original_id))
                recovery_id = db.create_card(
                    conn,
                    f"Resolve integration failure for card #{original_id}: stale merged branch",
                    role_type="Conflict Resolver",
                    source_key=f"integration-failure:{original_id}:merge_conflicts:origin/work/already-merged",
                    description="Branch: work/already-merged\nAlready merged elsewhere.",
                )
                scheduler.repair_control_plane_cards(conn)
                original = conn.execute("SELECT stage, status FROM worklanes WHERE id = ?", (original_id,)).fetchone()
                recovery = conn.execute("SELECT stage, status FROM worklanes WHERE id = ?", (recovery_id,)).fetchone()

            self.assertEqual((original["stage"], original["status"]), ("done", "integrated"))
            self.assertEqual((recovery["stage"], recovery["status"]), ("done", "stale"))

    def test_scheduler_caps_integration_recovery_wip_and_assigns_fresh_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            fake = FakeTmux()
            scheduler = HarnessScheduler(tmp, tmux=fake)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                for index in range(1, 3):
                    original_id = db.queue_worklane(conn, f"Failed original {index}", status="integration_failed")
                    recovery_id = db.create_card(
                        conn,
                        f"Resolve integration failure for card #{original_id}: active {index}",
                        role_type="Conflict Resolver",
                        source_key=f"integration-failure:{original_id}:merge_conflicts:branch-{index}",
                        description=f"Branch: work/conflict-{index}\nActive.",
                    )
                    db.upsert_agent(conn, name=f"developer-{index}", role="Developer", current_status="running", tmux_pane=f"%developer-{index}", cwd=tmp)
                    db.assign_card(conn, recovery_id, f"developer-{index}")
                for index in range(3, 8):
                    original_id = db.queue_worklane(conn, f"Failed original {index}", status="integration_failed")
                    db.create_card(
                        conn,
                        f"Resolve integration failure for card #{original_id}: queued {index}",
                        role_type="Conflict Resolver",
                        source_key=f"integration-failure:{original_id}:merge_conflicts:branch-{index}",
                        description=f"Branch: work/conflict-{index}\nQueued.",
                    )
                for index in range(4):
                    db.queue_worklane(conn, f"Fresh implementation {index}", role_type="Developer")

                scheduler.ensure_team(conn, "building")
                active_recovery = conn.execute(
                    """
                    SELECT COUNT(*) AS count FROM worklanes
                    WHERE stage = 'development'
                      AND (source_key LIKE 'integration-failure:%' OR title LIKE 'Resolve integration failure for card #%')
                    """
                ).fetchone()["count"]
                active_fresh = conn.execute(
                    """
                    SELECT COUNT(*) AS count FROM worklanes
                    WHERE stage = 'development'
                      AND source_key NOT LIKE 'integration-failure:%'
                      AND title NOT LIKE 'Resolve integration failure for card #%'
                    """
                ).fetchone()["count"]

            self.assertEqual(active_recovery, 3)
            self.assertEqual(active_fresh, 3)

    def test_repair_retires_old_non_actionable_development_cards(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            scheduler = HarnessScheduler(tmp, tmux=FakeTmux())
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                card_id = db.queue_worklane(conn, "Old empty card")
                db.upsert_agent(conn, name="developer-1", role="Developer", current_status="running", tmux_pane="%developer-1", cwd=tmp)
                db.assign_card(conn, card_id, "developer-1")
                conn.execute(
                    """
                    INSERT INTO agent_reports(created_at, agent_name, card_id, worklane_id, role, stage, status, report_json)
                    VALUES (?, 'developer-1', ?, ?, 'Developer', 'development', 'reserve_no_source_edits', '{}')
                    """,
                    (db.utc_now(), card_id, card_id),
                )
                scheduler.repair_control_plane_cards(conn)
                card = conn.execute("SELECT stage, status FROM worklanes WHERE id = ?", (card_id,)).fetchone()
                agent = conn.execute("SELECT current_status FROM agents WHERE name = 'developer-1'").fetchone()
            self.assertEqual((card["stage"], card["status"]), ("done", "stale"))
            self.assertEqual(agent["current_status"], "running")

    def test_stop_cleans_harness_runtime_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = db.bootstrap(root)
            fake = FakeTmux()
            fake.windows["session"] = {"developer-1": str(root), "manhole": str(root)}
            scheduler = HarnessScheduler(root, tmux=fake)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                db.set_meta(conn, "tmux_session", "session")
                conn.commit()
                db.upsert_agent(conn, name="developer-1", role="Developer", current_status="working", tmux_session="session", tmux_window="developer-1", tmux_pane="%1", cwd=tmp)
                db.queue_spawn_request(conn, role="Developer", title="later", prompt="do it")
                db.queue_message(conn, "hello")
            (paths.tmp / "keep.tmp").write_text("next janitor owns this")
            (paths.prompts / "keep.md").write_text("next janitor owns this")
            result = scheduler.stop()
            with db.connect(paths.db) as conn:
                agent = conn.execute("SELECT * FROM agents WHERE name = 'developer-1'").fetchone()
                request = conn.execute("SELECT * FROM spawn_requests").fetchone()
                message = conn.execute("SELECT * FROM messages").fetchone()
                banner = db.get_meta(conn, "red_banner")
                stopped = db.get_meta(conn, "harness_stopped")
            self.assertEqual(agent["current_status"], "stopped")
            self.assertEqual(request["status"], "cancelled")
            self.assertEqual(message["status"], "cancelled")
            self.assertEqual(banner, "Harness stopped.")
            self.assertEqual(stopped, "1")
            self.assertIn(("session", "developer-1"), fake.killed_windows)
            self.assertIn(("session", "manhole"), fake.killed_windows)
            self.assertEqual(result["agents"], 1)
            self.assertEqual(result["tmux_sessions"], 0)
            self.assertNotIn("tmp_entries", result)
            self.assertNotIn("prompt_files", result)
            self.assertTrue((paths.tmp / "keep.tmp").exists())
            self.assertTrue((paths.prompts / "keep.md").exists())
            self.assertFalse((root / "STATUS.md").exists())

    def test_stop_kills_harness_created_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            fake = FakeTmux()
            scheduler = HarnessScheduler(tmp, tmux=fake)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                db.set_meta(conn, "tmux_session", "llm-harness-repo")
                conn.commit()
            result = scheduler.stop()
            self.assertEqual(fake.killed_sessions, ["llm-harness-repo"])
            self.assertEqual(result["tmux_sessions"], 1)

    def test_stop_discovers_current_tmux_session_without_metadata(self):
        class CurrentSessionTmux(FakeTmux):
            def current_session(self):
                return "session"

        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            fake = CurrentSessionTmux()
            fake.windows["session"] = {"developer-1": tmp, "status": tmp}
            scheduler = HarnessScheduler(tmp, tmux=fake)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                db.upsert_agent(conn, name="developer-1", role="Developer", current_status="running", tmux_window="developer-1", cwd=tmp)
            scheduler.stop()
            self.assertIn(("session", "developer-1"), fake.killed_windows)
            self.assertIn(("session", "status"), fake.killed_windows)

    def test_watchdog_does_not_restart_stopped_harness(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                db.set_meta(conn, "harness_stopped", "1")
                conn.commit()
            with mock.patch.object(HarnessScheduler, "run", side_effect=AssertionError("watchdog restarted stopped harness")):
                self.assertEqual(watchdog_loop(tmp, once=True), 0)

    def test_stopped_harness_flag_blocks_agent_status_updates_after_banner_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                db.upsert_agent(conn, name="developer-1", role="Developer", current_status="stopped", cwd=tmp)
                db.set_meta(conn, "harness_stopped", "1")
                db.set_meta(conn, "red_banner", "")
                conn.commit()
            server = HarnessMCP(tmp, paths.db)
            result = server.call_tool("memory_update_agent", {"name": "developer-1", "status": "running"})
            self.assertIn("harness_stopped", result["content"][0]["text"])

    def test_reset_counters_clears_upgrade_noise_without_stopping_active_agents(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            scheduler = HarnessScheduler(tmp, tmux=FakeTmux())
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                db.upsert_agent(conn, name="developer-live", role="Developer", current_status="running", cwd=tmp)
                db.upsert_agent(conn, name="developer-crashed", role="Developer", current_status="crash", cwd=tmp)
                db.upsert_agent(conn, name="developer-stopped", role="Developer", current_status="stopped", cwd=tmp)
                run_id = db.record_test_run(
                    conn,
                    command="python -m unittest",
                    status="failed",
                    full_log="failed",
                    results=[{"nodeid": "tests/test_x.py::test_a", "file": "tests/test_x.py", "status": "failed"}],
                )
                db.note_failing_tests(conn, run_id, "bad")
                lane_id = db.queue_worklane(conn, "Retry integration", status="integration_failed")
                db.queue_spawn_request(conn, role="Architect", title="old", prompt="old")
                db.queue_message(conn, "old")
                db.set_meta(conn, "red_banner", "PROGRESS STALLED")
                db.set_meta(conn, "harness_stopped", "1")
                conn.commit()
            result = scheduler.reset_counters()
            with db.connect(paths.db) as conn:
                agents = list(conn.execute("SELECT name, current_status FROM agents ORDER BY name"))
                lane = conn.execute("SELECT * FROM worklanes WHERE id = ?", (lane_id,)).fetchone()
                counts = {
                    "test_runs": conn.execute("SELECT COUNT(*) AS count FROM test_runs").fetchone()["count"],
                    "test_results": conn.execute("SELECT COUNT(*) AS count FROM test_results").fetchone()["count"],
                    "bug_reports": conn.execute("SELECT COUNT(*) AS count FROM bug_reports WHERE status = 'open'").fetchone()["count"],
                    "issues": conn.execute("SELECT COUNT(*) AS count FROM issues WHERE source = 'test-loop' AND status = 'open'").fetchone()["count"],
                    "spawn_requests": conn.execute("SELECT COUNT(*) AS count FROM spawn_requests WHERE status = 'queued'").fetchone()["count"],
                    "messages": conn.execute("SELECT COUNT(*) AS count FROM messages WHERE status = 'queued'").fetchone()["count"],
                }
                banner = db.get_meta(conn, "red_banner")
                stopped = db.get_meta(conn, "harness_stopped")
            self.assertEqual(result["terminal_agents"], 2)
            self.assertEqual([(row["name"], row["current_status"]) for row in agents], [("developer-live", "running")])
            self.assertEqual(lane["status"], "ready_for_integration")
            self.assertEqual(lane["integration_queue"], "ready_fast_path")
            self.assertEqual(counts, {"test_runs": 0, "test_results": 0, "bug_reports": 0, "issues": 0, "spawn_requests": 0, "messages": 0})
            self.assertEqual(banner, "")
            self.assertEqual(stopped, "1")

    def test_stopped_harness_ignores_agent_status_updates(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                db.upsert_agent(conn, name="developer-1", role="Developer", current_status="stopped", cwd=tmp)
                db.set_meta(conn, "red_banner", "Harness stopped.")
                conn.commit()
            server = HarnessMCP(tmp, paths.db)
            result = server.call_tool("memory_update_agent", {"name": "developer-1", "status": "running"})
            self.assertIn("harness_stopped", result["content"][0]["text"])
            with db.connect(paths.db) as conn:
                agent = conn.execute("SELECT * FROM agents WHERE name = 'developer-1'").fetchone()
            self.assertEqual(agent["current_status"], "stopped")

    def test_liveness_marks_missing_tmux_panes_crashed(self):
        class MissingTmux(FakeTmux):
            def target_exists(self, target):
                return False

        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            scheduler = HarnessScheduler(tmp, tmux=MissingTmux())
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                db.upsert_agent(conn, name="developer-1", role="Developer", current_status="running", tmux_pane="%missing", cwd=tmp)
                lane_id = db.queue_worklane(conn, "Unreported work")
                db.assign_card(conn, lane_id, "developer-1")
                scheduler.check_agent_liveness(conn)
                agent = db.list_agents(conn)[0]
                lane = conn.execute("SELECT * FROM worklanes WHERE id = ?", (lane_id,)).fetchone()
                self.assertEqual(agent["current_status"], "crash")
                self.assertIn("tmux pane no longer exists", agent["notes"])
                self.assertEqual((lane["stage"], lane["status"]), ("planned", "queued"))
                self.assertIn("ended without an accepted report", lane["notes"])

    def test_status_reconciliation_marks_missing_tmux_panes_crashed(self):
        class MissingTmux(FakeTmux):
            def target_exists(self, target):
                return False

        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            scheduler = HarnessScheduler(tmp, tmux=MissingTmux())
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                db.upsert_agent(conn, name="developer-1", role="Developer", current_status="running", tmux_pane="%missing", cwd=tmp)
                lane_id = db.queue_worklane(conn, "Unreported work")
                db.assign_card(conn, lane_id, "developer-1")
                self.assertEqual(scheduler.reconcile_missing_tmux_agents(conn), 1)
                agent = db.list_agents(conn)[0]
                lane = conn.execute("SELECT * FROM worklanes WHERE id = ?", (lane_id,)).fetchone()

            self.assertEqual(agent["current_status"], "crash")
            self.assertEqual((lane["stage"], lane["status"]), ("planned", "queued"))

    def test_repair_requeues_cards_from_agents_with_ended_at_even_if_status_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            scheduler = HarnessScheduler(tmp, tmux=FakeTmux())
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                db.upsert_agent(conn, name="developer-1", role="Developer", current_status="running", tmux_pane="%developer-1", cwd=tmp)
                db.update_agent_status(conn, "developer-1", "crash", "ended once", ended=True)
                db.update_agent_status(conn, "developer-1", "running", "stale heartbeat")
                lane_id = db.queue_worklane(conn, "Should be requeued")
                db.assign_card(conn, lane_id, "developer-1")

                scheduler.repair_control_plane_cards(conn)
                lane = conn.execute("SELECT stage, status, owner_agent_id FROM worklanes WHERE id = ?", (lane_id,)).fetchone()

            self.assertEqual((lane["stage"], lane["status"], lane["owner_agent_id"]), ("planned", "queued", None))

    def test_reconciliation_closes_terminal_agent_windows(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            fake = FakeTmux()
            scheduler = HarnessScheduler(tmp, tmux=fake)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                db.upsert_agent(
                    conn,
                    name="developer-1",
                    role="Developer",
                    current_status="success",
                    tmux_session="fake-session",
                    tmux_window="developer-1",
                    tmux_pane="%developer-1",
                    cwd=tmp,
                    ended_at=db.utc_now(),
                )
                self.assertEqual(scheduler.reconcile_missing_tmux_agents(conn), 1)

            self.assertIn(("fake-session", "developer-1"), fake.killed_windows)

    def test_reconciliation_stops_duplicate_coordinators(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            fake = FakeTmux()
            scheduler = HarnessScheduler(tmp, tmux=fake)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                for name in ("coordinator-1", "coordinator-2"):
                    db.upsert_agent(
                        conn,
                        name=name,
                        role="Coordinator",
                        current_status="running",
                        tmux_session="fake-session",
                        tmux_window=name,
                        tmux_pane=f"%{name}",
                        cwd=tmp,
                    )
                card_id = db.create_card(conn, "Duplicate coordinator alert", role_type="Coordinator", integration_required=False)
                db.assign_card(conn, card_id, "coordinator-2")

                self.assertEqual(scheduler.reconcile_missing_tmux_agents(conn), 1)
                agents = conn.execute("SELECT name, current_status FROM agents ORDER BY name").fetchall()
                card = conn.execute("SELECT stage, status, owner_agent_id FROM worklanes WHERE id = ?", (card_id,)).fetchone()

            self.assertEqual([(row["name"], row["current_status"]) for row in agents], [("coordinator-1", "running"), ("coordinator-2", "stopped")])
            self.assertEqual((card["stage"], card["status"], card["owner_agent_id"]), ("planned", "queued", None))
            self.assertIn(("fake-session", "coordinator-2"), fake.killed_windows)

    def test_idle_liveness_prompts_are_throttled(self):
        old = (datetime.now(timezone.utc) - timedelta(seconds=max(IDLE_SECONDS, IDLE_PROMPT_SECONDS) + 1)).isoformat(timespec="seconds")
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            fake = FakeTmux()
            scheduler = HarnessScheduler(tmp, tmux=fake)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                db.upsert_agent(conn, name="auditor-1", role="Auditor", current_status="running", tmux_pane="%auditor", cwd=tmp)
                db.upsert_agent(conn, name="developer-1", role="Developer", current_status="running", tmux_pane="%developer", cwd=tmp)
                conn.execute("UPDATE agents SET last_seen_at = ?, last_prompt_at = ? WHERE name = 'developer-1'", (old, old))
                conn.commit()
                scheduler.check_agent_liveness(conn)
                scheduler.check_agent_liveness(conn)
            self.assertEqual(len(fake.sent), 1)
            self.assertIn("30 minutes", fake.sent[0][1])

    def test_liveness_batches_many_idle_agents_into_one_auditor_spawn(self):
        old = (datetime.now(timezone.utc) - timedelta(seconds=max(IDLE_SECONDS, IDLE_PROMPT_SECONDS, AUDITOR_SPAWN_SECONDS) + 1)).isoformat(timespec="seconds")
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            fake = FakeTmux()
            scheduler = HarnessScheduler(tmp, tmux=fake)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                for index in range(1, 6):
                    db.upsert_agent(conn, name=f"developer-{index}", role="Developer", current_status="running", tmux_pane=f"%developer-{index}", cwd=tmp)
                conn.execute("UPDATE agents SET last_seen_at = ?, last_prompt_at = ?", (old, old))
                conn.commit()
                scheduler.check_agent_liveness(conn)
                auditors = list(conn.execute("SELECT * FROM agents WHERE role = 'Auditor' AND current_status = 'running'"))
            auditor_windows = [window for _, window, _ in fake.commands if window.startswith("auditor-")]
            self.assertEqual(auditor_windows, ["auditor-1"])
            self.assertEqual(len(auditors), 1)
            self.assertIn("5 agents appear idle", (Path(tmp) / ".harness" / "prompts" / "auditor-1.md").read_text())

    def test_prompt_auditor_skips_stale_auditor_before_spawning(self):
        class StaleAuditorTmux(FakeTmux):
            def target_exists(self, target):
                return target != "%stale"

        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            fake = StaleAuditorTmux()
            scheduler = HarnessScheduler(tmp, tmux=fake)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                db.upsert_agent(conn, name="auditor-1", role="Auditor", current_status="running", tmux_pane="%stale", cwd=tmp)
                db.upsert_agent(conn, name="auditor-2", role="Auditor", current_status="working", tmux_pane="%live", cwd=tmp)
                scheduler.prompt_auditor(conn, "check this")
                stale = conn.execute("SELECT * FROM agents WHERE name = 'auditor-1'").fetchone()
            self.assertEqual(stale["current_status"], "crash")
            self.assertEqual(len(fake.sent), 1)
            self.assertEqual(fake.sent[0][0], "%live")
            self.assertIn("Assigned card #", fake.sent[0][1])
            self.assertIn("check this", fake.sent[0][1])

    def test_prompt_coordinator_reuses_active_coordinator(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            fake = FakeTmux()
            scheduler = HarnessScheduler(tmp, tmux=fake)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                db.upsert_agent(conn, name="coordinator-1", role="Coordinator", current_status="working", tmux_pane="%coordinator", cwd=tmp)
                scheduler.prompt_coordinator(conn, "reorganize")
            coordinator_windows = [window for _, window, _ in fake.commands if window.startswith("coordinator-")]
            self.assertEqual(coordinator_windows, [])
            self.assertEqual(len(fake.sent), 1)
            self.assertEqual(fake.sent[0][0], "%coordinator")
            self.assertIn("Assigned card #", fake.sent[0][1])
            self.assertIn("reorganize", fake.sent[0][1])

    def test_architect_spawn_requests_reuse_one_active_architect(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            fake = FakeTmux()
            scheduler = HarnessScheduler(tmp, tmux=fake)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                db.upsert_agent(conn, name="architect-1", role="Architect", current_status="working", tmux_pane="%architect", cwd=tmp)
                for index in range(1, 4):
                    db.queue_spawn_request(
                        conn,
                        role="Architect",
                        title=f"Investigate systemic issue {index}",
                        prompt=f"Find root cause {index}",
                        requester="test",
                    )
                scheduler.handle_spawn_requests(conn)
                requests = list(conn.execute("SELECT * FROM spawn_requests ORDER BY id"))
            architect_windows = [window for _, window, _ in fake.commands if window.startswith("architect-")]
            self.assertEqual(architect_windows, [])
            self.assertEqual([request["agent_name"] for request in requests], ["architect-1", "architect-1", "architect-1"])
            self.assertEqual(len(fake.sent), 1)
            self.assertTrue(all(request["card_id"] for request in requests))
            self.assertEqual([request["status"] for request in requests], ["started", "deferred", "deferred"])


if __name__ == "__main__":
    unittest.main()
