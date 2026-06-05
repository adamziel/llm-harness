from __future__ import annotations

import io
import json
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from llm_harness import __version__, db
from llm_harness.codex import CODEX_MODEL, CODEX_REASONING_EFFORT, build_codex_command
from llm_harness.mcp_server import HarnessMCP, serve
from llm_harness.roles import developer_count_for_building, specs_for_team
from llm_harness.scheduler import AUDITOR_SPAWN_SECONDS, IDLE_PROMPT_SECONDS, IDLE_SECONDS, HarnessScheduler
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
                db.upsert_agent(conn, name="developer-1", role="Developer", current_status="working", cwd=tmp)
                db.upsert_agent(conn, name="developer-2", role="Developer", current_status="crash", cwd=tmp)
                db.upsert_agent(conn, name="developer-3", role="Developer", current_status="stopped", cwd=tmp)
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
                self.assertIn("status is alive", text)

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
        self.assertIn("{run,status,stop,poke}", completed.stdout)
        self.assertNotIn("test-loop", completed.stdout)
        self.assertNotIn("update-status", completed.stdout)
        self.assertNotIn("mcp-config", completed.stdout)

    def test_version_flag_prints_package_version(self):
        root = Path(__file__).resolve().parents[1]
        completed = subprocess.run([sys.executable, str(root / "harness"), "-v"], text=True, capture_output=True, check=True)
        self.assertEqual(completed.stdout.strip(), f"harness {__version__}")

    def test_package_version_matches_pyproject(self):
        root = Path(__file__).resolve().parents[1]
        match = re.search(r'^version = "([^"]+)"$', (root / "pyproject.toml").read_text(), re.MULTILINE)
        self.assertIsNotNone(match)
        self.assertEqual(__version__, match.group(1))

    def test_building_team_uses_seventy_five_percent_of_cpu_cores_for_developers(self):
        self.assertEqual(developer_count_for_building(8), 6)
        self.assertEqual(developer_count_for_building(6), 4)
        self.assertEqual(developer_count_for_building(1), 1)
        specs = {spec.name: spec.min_count for spec in specs_for_team("building")}
        self.assertEqual(specs["Manager"], 1)
        self.assertEqual(specs["Integrator"], 1)
        self.assertEqual(set(specs), {"Manager", "Developer", "Integrator"})

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
                self.assertEqual(conn.execute("SELECT COUNT(*) AS count FROM work_lanes").fetchone()["count"], 1)
                resolve_fixed_tests(conn, [{"nodeid": "tests/test_x.py::test_a", "status": "passed"}], "good")
                bug = conn.execute("SELECT * FROM bug_reports WHERE test_nodeid = 'tests/test_x.py::test_a'").fetchone()
                self.assertEqual(bug["status"], "fixed")

    def test_scheduler_once_starts_support_windows_and_minimal_team(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            paths = db.bootstrap(root)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                db.set_meta(conn, "red_banner", "Harness stopped.")
            fake = FakeTmux()
            scheduler = HarnessScheduler(root, tmux=fake)
            code = scheduler.run(goal="Build something", team="minimal", once=True)
            self.assertEqual(code, 0)
            window_names = {window for _, window, _ in fake.commands}
            self.assertIn("manhole", window_names)
            self.assertIn("status", window_names)
            self.assertNotIn("switch:status", window_names)
            self.assertIn(("fake-session", "status", "watch -c -n 5 ./harness status"), fake.commands)
            codex_commands = [command for _, window, command in fake.commands if window not in {"manhole", "status", "updater", "tests"} and not window.startswith("switch:")]
            self.assertTrue(codex_commands)
            self.assertTrue(all("--yolo" in command and f"--model {CODEX_MODEL}" in command and f'model_reasoning_effort="{CODEX_REASONING_EFFORT}"' in command for command in codex_commands))
            with db.connect(root / ".harness" / "harness.sqlite3") as conn:
                agents = db.list_agents(conn)
                self.assertGreaterEqual(len(agents), 3)
                self.assertEqual(db.get_meta(conn, "red_banner"), "")

    def test_team_capacity_counts_live_non_terminal_developers(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            fake = FakeTmux()
            scheduler = HarnessScheduler(tmp, tmux=fake)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                db.upsert_agent(conn, name="manager-1", role="Manager", current_status="working", tmux_pane="%manager", cwd=tmp)
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
                db.upsert_agent(conn, name="manager-1", role="Manager", current_status="running", tmux_pane="%manager", cwd=tmp)
                db.upsert_agent(conn, name="integrator-1", role="Integrator", current_status="running", tmux_pane="%integrator", cwd=tmp)
                db.upsert_agent(conn, name="developer-1", role="Developer", current_status="working", tmux_pane="%live", cwd=tmp)
                db.upsert_agent(conn, name="developer-2", role="Developer", current_status="success", tmux_pane="%done", cwd=tmp)
                db.upsert_agent(conn, name="developer-3", role="Developer", current_status="working", tmux_pane="%missing", cwd=tmp)
                with mock.patch("llm_harness.roles.os.cpu_count", return_value=4):
                    scheduler.ensure_team(conn, "building")
                missing = conn.execute("SELECT current_status FROM agents WHERE name = 'developer-3'").fetchone()["current_status"]
            spawned_developers = [window for _, window, _ in fake.commands if window.startswith("developer-")]
            self.assertEqual(spawned_developers, ["developer-4", "developer-5"])
            self.assertEqual(missing, "crash")

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
            self.assertEqual(agent["current_status"], "stopped")
            self.assertEqual(request["status"], "cancelled")
            self.assertEqual(message["status"], "cancelled")
            self.assertEqual(banner, "Harness stopped.")
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
                scheduler.check_agent_liveness(conn)
                agent = db.list_agents(conn)[0]
                self.assertEqual(agent["current_status"], "crash")
                self.assertIn("tmux pane no longer exists", agent["notes"])

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
            self.assertEqual(fake.sent, [("%live", "check this")])

    def test_prompt_manager_reuses_active_manager(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            fake = FakeTmux()
            scheduler = HarnessScheduler(tmp, tmux=fake)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                db.upsert_agent(conn, name="manager-1", role="Manager", current_status="working", tmux_pane="%manager", cwd=tmp)
                scheduler.prompt_manager(conn, "reorganize")
            manager_windows = [window for _, window, _ in fake.commands if window.startswith("manager-")]
            self.assertEqual(manager_windows, [])
            self.assertEqual(fake.sent, [("%manager", "reorganize")])

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
            self.assertEqual(len(fake.sent), 3)


if __name__ == "__main__":
    unittest.main()
