from __future__ import annotations

import argparse
import io
import json
import os
import shlex
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from llm_harness import db
from llm_harness import status as status_mod
from llm_harness.codex import CODEX_MODEL, CODEX_REASONING_EFFORT, UnsafeCodexCommand, assert_codex_command_safe, build_codex_command
from llm_harness.indexer import code_search, refresh_index
from llm_harness.mcp_server import HarnessMCP
from llm_harness.roles import ROLE_ORDER, developer_count_for_building, prompt_for_role, slug_role, specs_for_team
from llm_harness.scheduler import HarnessScheduler
from llm_harness.testing_loop import (
    discover_test_command,
    maybe_invoke_architect,
    parse_test_output,
    queue_test_fix_lane,
    resolve_fixed_tests,
    summarize_results,
)
from llm_harness.tmux import Tmux, _session_name, shell_command


@contextmanager
def memory_conn():
    """Create a fast SQLite connection with the same row behavior as db.connect."""

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    db.init_db(conn)
    try:
        yield conn
    finally:
        conn.close()


def add_cases(cls: type[unittest.TestCase], prefix: str, cases, func) -> None:
    """Attach one real unittest method per case so the runner counts every case."""

    for index, case in enumerate(cases, start=1):
        name = f"test_{prefix}_{index:03d}"

        def test(self, case=case):
            return func(self, case)

        test.__name__ = name
        setattr(cls, name, test)


class RoleSizingTests(unittest.TestCase):
    pass


def _role_sizing_case(self: unittest.TestCase, case: tuple[int, int]) -> None:
    cores, expected = case
    self.assertEqual(developer_count_for_building(cores), expected)
    self.assertGreaterEqual(developer_count_for_building(cores), 1)


add_cases(
    RoleSizingTests,
    "developer_count",
    [(cores, max(1, int(cores * 0.75))) for cores in range(1, 21)],
    _role_sizing_case,
)


class RolePromptAndSlugTests(unittest.TestCase):
    pass


def _role_prompt_case(self: unittest.TestCase, role: str) -> None:
    prompt = prompt_for_role(role, f"agent-{slug_role(role)}", "ship it", "/tmp/harness.sqlite3", "/repo")
    self.assertIn("ship it", prompt)
    self.assertIn("/tmp/harness.sqlite3", prompt)
    self.assertIn("/repo", prompt)
    self.assertIn("--yolo", prompt)
    self.assertIn("gpt-5.5 xhigh fast", prompt)
    self.assertIn("SQLite MCP", prompt)


def _slug_case(self: unittest.TestCase, case: tuple[str, str]) -> None:
    role, expected = case
    self.assertEqual(slug_role(role), expected)


for idx, role in enumerate([*ROLE_ORDER, "Research Assistant"], start=1):
    add_cases(RolePromptAndSlugTests, f"prompt_{idx:02d}", [role], _role_prompt_case)

add_cases(
    RolePromptAndSlugTests,
    "slug",
    [
        ("Goal Planner", "goal-planner"),
        ("Status reporter", "status-reporter"),
        ("QA Lead", "qa-lead"),
        ("Developer", "developer"),
        ("Architect", "architect"),
        ("Janitor", "janitor"),
        ("Integrator", "integrator"),
        ("Designer", "designer"),
        ("Auditor", "auditor"),
        ("Manager", "manager"),
    ],
    _slug_case,
)


class CodexCommandTests(unittest.TestCase):
    pass


def _codex_command_case(self: unittest.TestCase, dirname: str) -> None:
    with tempfile.TemporaryDirectory(prefix=dirname.replace(" ", "_")) as tmp:
        root = Path(tmp) / dirname
        root.mkdir()
        prompt = root / "prompt file.md"
        prompt.write_text("Do the work")
        command = build_codex_command(prompt, root)
        self.assertIn("codex --yolo", command)
        self.assertIn(f"--model {CODEX_MODEL}", command)
        self.assertIn(f'model_reasoning_effort="{CODEX_REASONING_EFFORT}"', command)
        self.assertIn("prompt file.md", command)
        assert_codex_command_safe(command)


def _unsafe_command_case(self: unittest.TestCase, command: str) -> None:
    with self.assertRaises(UnsafeCodexCommand):
        assert_codex_command_safe(command)


add_cases(
    CodexCommandTests,
    "build",
    ["plain", "with space", "dash-name", "under_score", "nested.a", "caps", "num123", "quote-safe", "repo", "worktree"],
    _codex_command_case,
)
add_cases(
    CodexCommandTests,
    "unsafe",
    [
        "codex --model gpt-5.5",
        "codex --yolo",
        "codex --model other --yolo",
        "codex --dangerously-auto-approve --model gpt-5.5 -c model_reasoning_effort=\"xhigh\"",
        "codex --yolo --model gpt-4",
        "python -m codex --model gpt-5.5 -c model_reasoning_effort=\"xhigh\"",
        "codex --yolo --model",
        "codex --model gpt-5.5 -c model_reasoning_effort=\"xhigh\" --safe",
        "",
        "codex run",
    ],
    _unsafe_command_case,
)


class DBMetadataGoalEventTests(unittest.TestCase):
    pass


def _metadata_case(self: unittest.TestCase, case: tuple[str, str, str]) -> None:
    key, first, second = case
    with memory_conn() as conn:
        db.set_meta(conn, key, first)
        self.assertEqual(db.get_meta(conn, key), first)
        db.set_meta(conn, key, second)
        self.assertEqual(db.get_meta(conn, key), second)
        self.assertEqual(db.get_meta(conn, "missing", "fallback"), "fallback")


def _goal_case(self: unittest.TestCase, case: tuple[str, str]) -> None:
    goal_text, status = case
    with memory_conn() as conn:
        db.set_goal(conn, goal_text, measure="tests", status=status, auditor_summary="watch metric")
        goal = db.get_goal(conn)
        self.assertEqual(goal["text"], goal_text)
        self.assertEqual(goal["status"], status)
        self.assertEqual(goal["measure"], "tests")
        self.assertTrue(db.recent_events(conn, 1)[0]["message"].startswith("Goal"))


def _event_case(self: unittest.TestCase, case: tuple[str, dict[str, object]]) -> None:
    event_type, payload = case
    with memory_conn() as conn:
        event_id = db.log_event(conn, event_type, f"event {event_type}", agent_name="agent-1", payload=payload)
        row = db.recent_events(conn, 1)[0]
        self.assertEqual(row["id"], event_id)
        self.assertEqual(row["type"], event_type)
        self.assertEqual(row["agent_name"], "agent-1")
        self.assertEqual(json.loads(row["payload_json"]), payload)


add_cases(
    DBMetadataGoalEventTests,
    "metadata",
    [(f"key_{i}", f"value_{i}", f"next_{i}") for i in range(1, 6)],
    _metadata_case,
)
add_cases(
    DBMetadataGoalEventTests,
    "goal",
    [("planning goal", "planning"), ("building goal", "building"), ("paused goal", "paused"), ("done goal", "complete"), ("audit goal", "audit")],
    _goal_case,
)
add_cases(
    DBMetadataGoalEventTests,
    "event",
    [(name, {"index": idx, "ok": True}) for idx, name in enumerate(["spawn", "poke", "tests", "janitor", "metric", "tmux", "warning", "status", "index", "goal"], start=1)],
    _event_case,
)


class DBAgentMessageMetricTests(unittest.TestCase):
    pass


def _agent_case(self: unittest.TestCase, case: tuple[str, str]) -> None:
    role, status = case
    with memory_conn() as conn:
        db.upsert_agent(conn, name=f"{slug_role(role)}-case", role=role, current_status="running", tmux_pane="%1", cwd="/repo")
        db.update_agent_status(conn, f"{slug_role(role)}-case", status, notes=f"now {status}", ended=status in {"success", "crash"})
        agent = db.list_agents(conn)[0]
        self.assertEqual(agent["role"], role)
        self.assertEqual(agent["current_status"], status)
        self.assertIn(status, agent["notes"])


def _message_case(self: unittest.TestCase, target: str) -> None:
    with memory_conn() as conn:
        message_id = db.queue_message(conn, f"hello {target}", target=target)
        db.mark_message(conn, message_id, "delivered")
        row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        self.assertEqual(row["target"], target)
        self.assertEqual(row["status"], "delivered")


def _metric_case(self: unittest.TestCase, case: tuple[float, float, float]) -> None:
    value, target, expected_percent = case
    with memory_conn() as conn:
        db.record_metric(conn, "coverage", value, target)
        metric = db.latest_metric(conn)
        self.assertEqual(metric["metric_name"], "coverage")
        self.assertAlmostEqual(metric["percent_ready"], expected_percent)


def _resource_case(self: unittest.TestCase, case: tuple[float, float, float]) -> None:
    cpu, ram, disk = case
    with memory_conn() as conn:
        db.record_resource_sample(conn, {"cpu_percent": cpu, "ram_percent": ram, "disk_free_gb": disk, "load1": 1.5, "processes": [{"pid": 1}]})
        row = conn.execute("SELECT * FROM resource_samples").fetchone()
        self.assertEqual(row["cpu_percent"], cpu)
        self.assertEqual(json.loads(row["process_json"]), [{"pid": 1}])


add_cases(DBAgentMessageMetricTests, "agent", [(role, status) for role, status in zip(ROLE_ORDER[:10], ["running", "idle", "success", "crash", "waiting", "running", "success", "idle", "running", "waiting"])], _agent_case)
add_cases(DBAgentMessageMetricTests, "message", ["broadcast", "Manager", "developer-1", "Auditor", "Integrator"], _message_case)
add_cases(DBAgentMessageMetricTests, "metric", [(0, 10, 0), (5, 10, 50), (10, 10, 100), (20, 10, 100), (5, 0, 0)], _metric_case)
add_cases(DBAgentMessageMetricTests, "resource", [(1, 2, 3), (59.9, 40, 100), (95, 10, 20), (10, 96, 30), (60, 60, 40)], _resource_case)


class DBTestRunBugRetentionTests(unittest.TestCase):
    pass


def _record_test_run_case(self: unittest.TestCase, case: tuple[str, str]) -> None:
    nodeid, status = case
    with memory_conn() as conn:
        run_id = db.record_test_run(conn, "cmd", "failed" if status in {"failed", "error"} else "passed", "log", results=[{"nodeid": nodeid, "file": nodeid.split("::")[0], "status": status}])
        row = conn.execute("SELECT * FROM test_results WHERE run_id = ?", (run_id,)).fetchone()
        self.assertEqual(row["nodeid"], nodeid)
        self.assertEqual(row["status"], status)


def _note_failure_case(self: unittest.TestCase, status: str) -> None:
    with memory_conn() as conn:
        run_id = db.record_test_run(conn, "cmd", "failed", "log", commit_sha="bad", results=[{"nodeid": f"tests/test_{status}.py::test_x", "status": status}])
        db.note_failing_tests(conn, run_id, "bad")
        count = conn.execute("SELECT COUNT(*) AS count FROM bug_reports").fetchone()["count"]
        self.assertEqual(count, 1 if status in {"failed", "error"} else 0)


def _repeated_failure_case(self: unittest.TestCase, nodeid: str) -> None:
    with memory_conn() as conn:
        for _ in range(2):
            run_id = db.record_test_run(conn, "cmd", "failed", "log", results=[{"nodeid": nodeid, "status": "failed"}])
            db.note_failing_tests(conn, run_id, "bad")
        bug = conn.execute("SELECT * FROM bug_reports WHERE test_nodeid = ?", (nodeid,)).fetchone()
        self.assertEqual(bug["occurrences"], 2)


def _purge_logs_case(self: unittest.TestCase, run_count: int) -> None:
    with memory_conn() as conn:
        for idx in range(run_count):
            db.record_test_run(conn, "cmd", "passed", f"full-log-{idx}")
        db.purge_old_test_logs(conn)
        rows = conn.execute("SELECT id, full_log FROM test_runs ORDER BY id").fetchall()
        retained = [row for row in rows if row["full_log"]]
        self.assertTrue(all(row["full_log"] for row in rows[-5:]))
        self.assertGreaterEqual(len(retained), 5)
        self.assertLessEqual(len(retained), 6)


add_cases(DBTestRunBugRetentionTests, "record", [(f"tests/test_{i}.py::test_case", status) for i, status in enumerate(["passed", "failed", "skipped", "error", "passed"], start=1)], _record_test_run_case)
add_cases(DBTestRunBugRetentionTests, "note_failure", ["failed", "error", "passed", "skipped", "failed"], _note_failure_case)
add_cases(DBTestRunBugRetentionTests, "repeat", [f"tests/test_repeat.py::test_{i}" for i in range(1, 6)], _repeated_failure_case)
add_cases(DBTestRunBugRetentionTests, "purge", [6, 7, 8, 9, 10], _purge_logs_case)


class TestingLoopTests(unittest.TestCase):
    pass


def _pytest_parse_case(self: unittest.TestCase, case: tuple[str, str]) -> None:
    nodeid, status = case
    parsed = parse_test_output(f"{nodeid} {status.upper()}\n")
    expected = "failed" if status == "failed" else status
    self.assertEqual(parsed[0]["nodeid"], nodeid)
    self.assertEqual(parsed[0]["status"], expected)


def _unittest_parse_case(self: unittest.TestCase, case: tuple[str, str, str]) -> None:
    test_name, raw_status, expected = case
    parsed = parse_test_output(f"{test_name} (tests.test_case.Case.{test_name}) ... {raw_status}\n")
    self.assertEqual(parsed[0]["status"], expected)
    self.assertEqual(parsed[0]["nodeid"], f"tests.test_case.Case.{test_name}")


def _summary_case(self: unittest.TestCase, statuses: list[str]) -> None:
    summary = summarize_results([{"status": status} for status in statuses], returncode=1 if "failed" in statuses else 0)
    for status in {"passed", "failed", "skipped", "error"}:
        self.assertEqual(summary.get(status, 0), statuses.count(status))


def _discover_case(self: unittest.TestCase, case: tuple[bool, bool, bool]) -> None:
    has_pyproject, has_tests, has_pytest = case
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        if has_pyproject:
            (root / "pyproject.toml").write_text("[project]\nname='x'\n")
        if has_tests:
            (root / "tests").mkdir()
        with mock.patch("llm_harness.testing_loop.importlib.util.find_spec", return_value=object() if has_pytest else None):
            command = discover_test_command(root)
        if (has_pyproject or has_tests) and has_pytest:
            self.assertEqual(command[-1], "-vv")
        elif has_tests:
            self.assertEqual(command[-2:], ["tests", "-v"])
        else:
            self.assertEqual(command[-1], "discover")


def _bug_workflow_case(self: unittest.TestCase, nodeid: str) -> None:
    with memory_conn() as conn:
        queue_test_fix_lane(conn, 7, [{"nodeid": nodeid, "status": "failed"}], "bad")
        lane = conn.execute("SELECT * FROM work_lanes").fetchone()
        self.assertIn(nodeid, lane["notes"])
        fail_id = db.record_test_run(conn, "cmd", "failed", "log", results=[{"nodeid": nodeid, "status": "failed"}])
        db.note_failing_tests(conn, fail_id, "bad")
        resolve_fixed_tests(conn, [{"nodeid": nodeid, "status": "passed"}], "good")
        self.assertEqual(conn.execute("SELECT status FROM bug_reports WHERE test_nodeid = ?", (nodeid,)).fetchone()["status"], "fixed")


add_cases(TestingLoopTests, "pytest_parse", [(f"tests/test_file.py::test_{i}", status) for i, status in enumerate(["passed", "failed", "skipped", "error", "passed"], start=1)], _pytest_parse_case)
add_cases(TestingLoopTests, "unittest_parse", [(f"test_{i}", raw, expected) for i, (raw, expected) in enumerate([("ok", "passed"), ("FAIL", "failed"), ("ERROR", "error"), ("skipped 'why'", "skipped"), ("ok", "passed")], start=1)], _unittest_parse_case)
add_cases(TestingLoopTests, "summary", [["passed"], ["failed"], ["skipped"], ["error"], ["passed", "failed", "skipped", "error"]], _summary_case)
add_cases(TestingLoopTests, "discover", [(True, False, True), (False, True, False), (False, False, False), (True, True, False), (False, True, True)], _discover_case)
add_cases(TestingLoopTests, "bug_workflow", [f"tests/test_bug.py::test_{i}" for i in range(1, 6)], _bug_workflow_case)


class StatusRenderingTests(unittest.TestCase):
    pass


def _status_report_case(self: unittest.TestCase, case: tuple[float, float]) -> None:
    value, target = case
    with tempfile.TemporaryDirectory() as tmp, db.connect(Path(tmp) / "harness.sqlite3") as conn:
        db.init_db(conn)
        db.set_goal(conn, f"Goal {value}", measure="metric")
        db.record_metric(conn, "metric", value, target)
        db.record_resource_sample(conn, {"cpu_percent": value, "ram_percent": target, "disk_free_gb": 99, "load1": 0})
        md, html = status_mod.refresh_reports(conn, tmp)
        self.assertIn("Goal", md.read_text())
        self.assertIn("Harness Status", html.read_text())
        self.assertIn("Progress", status_mod.dashboard(conn))


def _red_banner_case(self: unittest.TestCase, banner: str) -> None:
    with memory_conn() as conn:
        db.set_meta(conn, "red_banner", banner)
        self.assertIn(banner, status_mod.dashboard(conn))


def _html_escape_case(self: unittest.TestCase, text: str) -> None:
    with tempfile.TemporaryDirectory() as tmp, db.connect(Path(tmp) / "harness.sqlite3") as conn:
        db.init_db(conn)
        db.set_goal(conn, text, measure="metric")
        _, html_file = status_mod.refresh_reports(conn, tmp)
        rendered = html_file.read_text()
        self.assertNotIn(text, rendered)
        self.assertIn("&lt;", rendered)


def _sparkline_case(self: unittest.TestCase, values: list[float]) -> None:
    chart = status_mod._sparkline([{"percent_ready": value} for value in values], "percent_ready")
    self.assertIn("svg", chart if len(values) >= 2 else "No metric history chart yet.<svg")


def _template_case(self: unittest.TestCase, filename: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        status_mod.ensure_templates(tmp)
        path = Path(tmp) / ".harness" / filename
        path.write_text("custom {{generated_at}}")
        status_mod.ensure_templates(tmp)
        self.assertEqual(path.read_text(), "custom {{generated_at}}")


add_cases(StatusRenderingTests, "report", [(0, 10), (1, 2), (5, 10), (10, 10), (20, 10)], _status_report_case)
add_cases(StatusRenderingTests, "red_banner", ["PROGRESS STALLED", "RESOURCE HOT", "MANAGER NEEDED"], _red_banner_case)
add_cases(StatusRenderingTests, "html_escape", ["<script>alert(1)</script>", "<b>bold</b>", "5 < 6"], _html_escape_case)
add_cases(StatusRenderingTests, "sparkline", [[0, 50], [0, 50, 100]], _sparkline_case)
add_cases(StatusRenderingTests, "template", ["STATUS_TEMPLATE.md", "STATUS_TEMPLATE.html"], _template_case)


class MCPIndexerTests(unittest.TestCase):
    pass


def _mcp_handle_case(self: unittest.TestCase, method: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        paths = db.bootstrap(tmp)
        server = HarnessMCP(tmp, paths.db)
        response = server.handle({"jsonrpc": "2.0", "id": 1, "method": method})
        self.assertEqual(response["id"], 1)
        self.assertIn("result", response)


def _readonly_reject_case(self: unittest.TestCase, sql: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        paths = db.bootstrap(tmp)
        server = HarnessMCP(tmp, paths.db)
        result = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "memory_query", "arguments": {"sql": sql}}})
        self.assertIn("error", result)


def _code_index_case(self: unittest.TestCase, case: tuple[str, str]) -> None:
    filename, content = case
    with tempfile.TemporaryDirectory() as tmp, db.connect(Path(tmp) / ".harness.sqlite3") as conn:
        root = Path(tmp)
        (root / filename).write_text(content)
        db.init_db(conn)
        self.assertGreaterEqual(refresh_index(conn, root), 1)
        results = code_search(conn, root, "needle")
        self.assertEqual(results[0]["path"], filename)


def _mcp_tool_case(self: unittest.TestCase, case: tuple[str, dict[str, object], str]) -> None:
    tool_name, args, expected = case
    with tempfile.TemporaryDirectory() as tmp:
        paths = db.bootstrap(tmp)
        server = HarnessMCP(tmp, paths.db)
        result = server.call_tool(tool_name, args)
        self.assertIn(expected, result["content"][0]["text"])


def _mcp_error_case(self: unittest.TestCase, message: dict[str, object]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        paths = db.bootstrap(tmp)
        server = HarnessMCP(tmp, paths.db)
        result = server.handle(message)
        self.assertIn("error", result)


add_cases(MCPIndexerTests, "handle", ["initialize", "tools/list", "initialize"], _mcp_handle_case)
add_cases(MCPIndexerTests, "readonly", ["DELETE FROM events", "UPDATE goals SET text = 'x'"], _readonly_reject_case)
add_cases(MCPIndexerTests, "index", [(f"file_{i}.py", f"def func_{i}():\n    return 'needle {i}'\n") for i in range(1, 6)], _code_index_case)
add_cases(
    MCPIndexerTests,
    "tool",
    [
        ("memory_record_event", {"type": "note", "message": "hello"}, "event_id"),
        ("memory_query", {"sql": "SELECT COUNT(*) AS count FROM events"}, "count"),
        ("spawn_agent", {"role": "Developer", "title": "lane", "prompt": "work"}, "spawn_request_id"),
    ],
    _mcp_tool_case,
)
add_cases(MCPIndexerTests, "error", [{"id": 3, "method": "unknown"}, {"id": 4, "method": "tools/call", "params": {"name": "missing", "arguments": {}}}], _mcp_error_case)


class SchedulerAndTmuxTests(unittest.TestCase):
    pass


def _effective_team_case(self: unittest.TestCase, case: tuple[str, str]) -> None:
    goal_status, expected = case
    with tempfile.TemporaryDirectory() as tmp:
        paths = db.bootstrap(tmp)
        scheduler = HarnessScheduler(tmp)
        with db.connect(paths.db) as conn:
            db.init_db(conn)
            db.set_goal(conn, "goal", status=goal_status)
            self.assertEqual(scheduler.effective_team(conn, "auto"), expected)


def _session_name_case(self: unittest.TestCase, case: tuple[str, str]) -> None:
    dirname, expected_part = case
    path = Path(tempfile.gettempdir()) / dirname
    self.assertIn(expected_part, _session_name(path))


def _shell_quote_case(self: unittest.TestCase, parts: tuple[str, ...]) -> None:
    command = shell_command(*parts)
    for part in parts:
        if " " not in part:
            self.assertIn(part, command)
    self.assertNotIn("\n", command)


def _tmux_new_window_case(self: unittest.TestCase, command: str) -> None:
    calls = []

    def runner(args, check=True, text=True, capture_output=True):
        calls.append(args)
        if args[1] == "list-windows":
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[1] == "display-message":
            return subprocess.CompletedProcess(args, 0, stdout="%1\n", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    Tmux(runner=runner).ensure_window("0", "manhole", command)
    new_window = [args for args in calls if args[1] == "new-window"][0]
    self.assertEqual(new_window[:7], ["tmux", "new-window", "-d", "-t", "0:", "-n", "manhole"])
    self.assertEqual(len(new_window), 8)
    self.assertEqual(shlex.split(new_window[7]), ["bash", "-lc", command])


def _tmux_target_exists_case(self: unittest.TestCase, case: tuple[str, bool]) -> None:
    pane_dead, expected = case

    def runner(args, check=True, text=True, capture_output=True):
        return subprocess.CompletedProcess(args, 0, stdout=f"%1\t{pane_dead}\n", stderr="")

    self.assertEqual(Tmux(runner=runner).target_exists("%1"), expected)


def _spawn_spec_case(self: unittest.TestCase, team: str) -> None:
    specs = specs_for_team(team)
    self.assertTrue(specs)
    self.assertTrue(all(spec.min_count >= 1 for spec in specs))


add_cases(SchedulerAndTmuxTests, "effective", [("planning", "planning"), ("building", "building"), ("complete", "building")], _effective_team_case)
add_cases(SchedulerAndTmuxTests, "session_name", [("repo name", "repo-name"), ("repo.name", "repo.name"), ("repo_name", "repo_name")], _session_name_case)
add_cases(SchedulerAndTmuxTests, "shell_quote", [("watch", "-n", "5"), ("./harness", "status"), ("echo", "hello world")], _shell_quote_case)
add_cases(
    SchedulerAndTmuxTests,
    "new_window",
    ["echo hello", "cd /repo && codex --yolo", 'codex -c \'model_reasoning_effort="xhigh"\' "$(cat prompt.md)"'],
    _tmux_new_window_case,
)
add_cases(SchedulerAndTmuxTests, "target_exists", [("0", True), ("1", False)], _tmux_target_exists_case)
add_cases(SchedulerAndTmuxTests, "spec", ["planning", "unknown"], _spawn_spec_case)
