from __future__ import annotations

import io
import json
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
from llm_harness.integration import integrate_once
from llm_harness.mcp_server import HarnessMCP, serve
from llm_harness.roles import developer_count_for_building, specs_for_team
from llm_harness.scheduler import AUDITOR_SPAWN_SECONDS, IDLE_PROMPT_SECONDS, IDLE_SECONDS, HarnessScheduler, watchdog_loop
from llm_harness.status import dashboard, refresh_reports
from llm_harness.testing_loop import parse_test_output, run_tests_once
from llm_harness.tmux import TmuxPane


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
            self.assertEqual(attempt["merge_result"], "merge_conflicts")
            self.assertEqual(json.loads(attempt["tests_json"]), [])
            self.assertEqual((conflict_card["stage"], conflict_card["status"]), ("planned", "queued"))
            self.assertIn("merge_conflicts", conflict_card["description"])

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

    def test_scheduler_once_starts_support_windows_and_minimal_team(self):
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
            code = scheduler.run(goal="Build something", team="minimal", once=True)
            self.assertEqual(code, 0)
            window_names = {window for _, window, _ in fake.commands}
            self.assertIn("manhole", window_names)
            self.assertIn("status", window_names)
            self.assertIn("integration", window_names)
            self.assertNotIn("switch:status", window_names)
            self.assertIn(("fake-session", "status", "watch -c -n 5 ./harness status"), fake.commands)
            self.assertIn(("fake-session", "integration", "while true; do ./harness integrate; sleep 30; done"), fake.commands)
            self.assertIn(("fake-session", "tests", "while true; do ./harness test-loop --once; sleep 5; done"), fake.commands)
            codex_commands = [command for _, window, command in fake.commands if window not in {"manhole", "status", "updater", "integration", "tests"} and not window.startswith("switch:")]
            self.assertTrue(codex_commands)
            self.assertTrue(all("--yolo" in command and f"--model {CODEX_MODEL}" in command and f'model_reasoning_effort="{CODEX_REASONING_EFFORT}"' in command for command in codex_commands))
            self.assertIn("Default to supervisor/read-only mode", (root / ".harness" / "prompts" / "manhole.md").read_text())
            with db.connect(root / ".harness" / "harness.sqlite3") as conn:
                agents = db.list_agents(conn)
                self.assertEqual([agent["role"] for agent in agents], ["Coordinator"])
                self.assertEqual(db.get_meta(conn, "red_banner"), "")
                self.assertEqual(db.get_meta(conn, "harness_stopped"), "0")

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
            self.assertEqual(spawned_developers, ["developer-2", "developer-3", "developer-4", "developer-5", "developer-6"])
            self.assertEqual(assigned, 5)

    def test_non_actionable_developer_report_retires_card_and_worker(self):
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
            self.assertEqual(agent["current_status"], "success")

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
            self.assertEqual(agent["current_status"], "success")

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
