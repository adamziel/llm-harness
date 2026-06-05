from __future__ import annotations

import io
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from llm_harness import __version__, db
from llm_harness.codex import CODEX_MODEL, CODEX_REASONING_EFFORT, build_codex_command
from llm_harness.mcp_server import HarnessMCP, serve
from llm_harness.roles import developer_count_for_building, specs_for_team
from llm_harness.scheduler import HarnessScheduler
from llm_harness.status import dashboard, refresh_reports
from llm_harness.testing_loop import parse_test_output, run_tests_once
from llm_harness.tmux import TmuxPane


class FakeTmux:
    def __init__(self):
        self.commands = []
        self.sent = []

    def current_or_create_session(self, root):
        return "fake-session"

    def ensure_window(self, session, window, command):
        self.commands.append((session, window, command))
        return TmuxPane(session, window, f"%{window}")

    def switch_to(self, session, window):
        self.commands.append((session, f"switch:{window}", ""))

    def capture(self, target, lines=200):
        return "working"

    def send_prompt(self, target, message):
        self.sent.append((target, message))


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
            self.assertIn(str(prompt), command)

    def test_status_reports_and_dashboard_render_from_sqlite(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = db.bootstrap(tmp)
            with db.connect(paths.db) as conn:
                db.init_db(conn)
                db.set_goal(conn, "Ship measurable work", measure="tests pass")
                db.record_metric(conn, "tests", 3, 4)
                db.record_resource_sample(conn, {"cpu_percent": 12, "ram_percent": 34, "disk_free_gb": 56, "load1": 1})
                db.log_event(conn, "note", "status is alive")
                md, html = refresh_reports(conn, tmp)
                self.assertTrue(md.exists())
                self.assertTrue(html.exists())
                self.assertTrue((Path(tmp) / "progress.md").exists())
                self.assertTrue((Path(tmp) / "progress.html").exists())
                text = dashboard(conn)
                self.assertIn("Last generated", text)
                self.assertIn("Progress", text)
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
        self.assertIn("{run,status,poke}", completed.stdout)
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
            fake = FakeTmux()
            scheduler = HarnessScheduler(root, tmux=fake)
            code = scheduler.run(goal="Build something", team="minimal", once=True)
            self.assertEqual(code, 0)
            window_names = {window for _, window, _ in fake.commands}
            self.assertIn("manhole", window_names)
            self.assertIn("status", window_names)
            codex_commands = [command for _, window, command in fake.commands if window not in {"manhole", "status", "updater", "tests"} and not window.startswith("switch:")]
            self.assertTrue(codex_commands)
            self.assertTrue(all("--yolo" in command and f"--model {CODEX_MODEL}" in command and f'model_reasoning_effort="{CODEX_REASONING_EFFORT}"' in command for command in codex_commands))
            with db.connect(root / ".harness" / "harness.sqlite3") as conn:
                agents = db.list_agents(conn)
                self.assertGreaterEqual(len(agents), 3)


if __name__ == "__main__":
    unittest.main()
