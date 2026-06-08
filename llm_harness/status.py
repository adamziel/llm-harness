"""Status dashboard and report generation for the harness."""

from __future__ import annotations

import html
import json
import os
import re
import subprocess
import time
from pathlib import Path
from textwrap import shorten
from typing import Any

from .db import is_active_agent_status, log_event, metric_history, recent_events, selected_metric, set_meta, utc_now

ANSI = {
    "reset": "\033[0m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "bold": "\033[1m",
}
ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

STATUS_REFRESH_SECONDS = 15 * 60
STATUS_PUBLISH_PATHS = (
    "STATUS.md",
    "STATUS.html",
    "progress.md",
    "progress.html",
    ".harness/STATUS_TEMPLATE.md",
    ".harness/STATUS_TEMPLATE.html",
)
MAINLINE_BRANCH_FALLBACKS = ("main", "master", "trunk")

MD_TEMPLATE = """# Harness Status

Last generated: {{generated_at}}

## Runtime alerts
{{runtime_alerts}}

## Goal
{{goal}}

## Metric
{{metric}}

## Agents
{{agents}}

## Work lanes
{{work_lanes}}

## Tests
{{tests}}

## Resource samples
{{resources}}

## Recent events
{{events}}

## Next steps
{{next_steps}}
"""

HTML_TEMPLATE = """<!doctype html>
<html lang=\"en\">
<meta charset=\"utf-8\">
<title>Harness Status</title>
<style>
:root { color-scheme: light dark; font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, sans-serif; }
body { max-width: 1100px; margin: 2rem auto; padding: 0 1rem; line-height: 1.45; }
.card { border: 1px solid #8885; border-radius: 14px; padding: 1rem; margin: 1rem 0; box-shadow: 0 1px 8px #0001; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 1rem; }
.bar { height: 1rem; background: #8883; border-radius: 999px; overflow: hidden; }
.fill { height: 100%; background: linear-gradient(90deg, #2dd4bf, #2563eb); width: {{metric_percent}}%; }
table { width: 100%; border-collapse: collapse; }
th, td { border-bottom: 1px solid #8884; padding: .35rem; text-align: left; vertical-align: top; }
.bad { color: #dc2626; font-weight: 700; }
.warn { color: #d97706; font-weight: 700; }
.good { color: #059669; font-weight: 700; }
pre { white-space: pre-wrap; }
</style>
<body>
<h1>Harness Status</h1>
<p><strong>Last generated:</strong> {{generated_at}}</p>
{{runtime_alerts_html}}
<div class=\"card\"><h2>Goal</h2><p>{{goal_html}}</p></div>
<div class=\"grid\">
  <div class=\"card\"><h2>Metric</h2><div class=\"bar\"><div class=\"fill\"></div></div><p>{{metric_html}}</p>{{metric_chart_html}}</div>
  <div class=\"card\"><h2>Resources</h2>{{resources_html}}</div>
  <div class=\"card\"><h2>Tests</h2>{{tests_html}}</div>
</div>
<div class=\"card\"><h2>Agents</h2>{{agents_html}}</div>
<div class=\"card\"><h2>Work lanes</h2>{{work_lanes_html}}</div>
<div class=\"card\"><h2>Recent events</h2>{{events_html}}</div>
<div class=\"card\"><h2>Next steps</h2><p>{{next_steps_html}}</p></div>
</body></html>
"""


def ensure_templates(root: str | Path) -> None:
    """Create status templates on first updater run and leave later edits intact."""

    harness_dir = Path(root) / ".harness"
    harness_dir.mkdir(parents=True, exist_ok=True)
    md = harness_dir / "STATUS_TEMPLATE.md"
    html_template = harness_dir / "STATUS_TEMPLATE.html"
    if not md.exists():
        md.write_text(MD_TEMPLATE)
    if not html_template.exists():
        html_template.write_text(HTML_TEMPLATE)


def refresh_reports(conn: Any, root: str | Path, publish: bool = True) -> tuple[Path, Path]:
    """Render STATUS.md and STATUS.html from Turso so status is restart-safe."""

    root_path = Path(root)
    ensure_templates(root_path)
    data = collect_status(conn)
    generated = utc_now()
    data["generated_at"] = generated
    md_template = (root_path / ".harness" / "STATUS_TEMPLATE.md").read_text()
    html_template = (root_path / ".harness" / "STATUS_TEMPLATE.html").read_text()

    md = _render(md_template, _markdown_context(data))
    html_text = _render(html_template, _html_context(data))
    status_md = root_path / "STATUS.md"
    status_html = root_path / "STATUS.html"
    status_md.write_text(md)
    status_html.write_text(html_text)
    (root_path / "progress.md").write_text(md)
    (root_path / "progress.html").write_text(html_text)
    conn.execute("INSERT INTO status_snapshots(created_at, summary_json) VALUES (?, ?)", (generated, json.dumps(data, sort_keys=True)))
    set_meta(conn, "last_status_refresh_epoch", str(time.time()))
    conn.commit()
    if publish:
        commit_and_push_status(conn, root_path)
    return status_md, status_html


def commit_and_push_status(conn: Any, root: str | Path) -> bool:
    """Commit and push status artifacts without staging unrelated user work."""

    root_path = Path(root)
    if not _git_ok(root_path, ["rev-parse", "--is-inside-work-tree"]):
        return False
    branch = _git_stdout(root_path, ["branch", "--show-current"])
    mainline = _mainline_branch(root_path)
    if not branch or branch != mainline:
        log_event(
            conn,
            "status_publish_skipped",
            "Status files were updated but not pushed because this is not the mainline worktree branch",
            payload={"branch": branch, "mainline_branch": mainline},
        )
        return False
    if not _git_ok(root_path, ["remote", "get-url", "origin"]):
        log_event(conn, "status_publish_skipped", "Status files were updated but no origin remote is configured")
        return False
    staged_before = _git_stdout(root_path, ["diff", "--cached", "--name-only", "--"])
    if staged_before.strip():
        log_event(conn, "status_publish_skipped", "Status files were updated but existing staged changes would make an automatic commit unsafe")
        return False

    existing = [path for path in STATUS_PUBLISH_PATHS if (root_path / path).exists()]
    if not existing:
        return False
    add = subprocess.run(["git", "add", "--", *existing], cwd=root_path, text=True, capture_output=True, check=False, timeout=20)
    if add.returncode != 0:
        log_event(conn, "status_publish_failed", f"Failed to stage status files: {(add.stderr or add.stdout).strip()}")
        return False
    changed = _git_stdout(root_path, ["diff", "--cached", "--name-only", "--", *existing])
    if not changed.strip():
        return False
    commit = subprocess.run(["git", "commit", "-m", "Update harness status"], cwd=root_path, text=True, capture_output=True, check=False, timeout=20)
    if commit.returncode != 0:
        subprocess.run(["git", "restore", "--staged", "--", *existing], cwd=root_path, text=True, capture_output=True, check=False, timeout=20)
        log_event(conn, "status_publish_failed", f"Failed to commit status files: {(commit.stderr or commit.stdout).strip()}")
        return False
    push = subprocess.run(["git", "push", "origin", f"HEAD:{mainline}"], cwd=root_path, text=True, capture_output=True, check=False, timeout=30)
    if push.returncode != 0:
        log_event(conn, "status_publish_failed", f"Committed status files but failed to push: {(push.stderr or push.stdout).strip()}", payload={"branch": branch, "mainline_branch": mainline})
        return False
    log_event(conn, "status_published", f"Committed and pushed status update to origin/{mainline}", payload={"branch": branch, "mainline_branch": mainline})
    return True


def _git_stdout(root: Path, args: list[str]) -> str:
    result = subprocess.run(["git", *args], cwd=root, text=True, capture_output=True, check=False, timeout=20)
    return result.stdout.strip() if result.returncode == 0 else ""


def _git_ok(root: Path, args: list[str]) -> bool:
    return subprocess.run(["git", *args], cwd=root, text=True, capture_output=True, check=False, timeout=20).returncode == 0


def _mainline_branch(root: Path) -> str:
    origin_head = _git_stdout(root, ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
    if origin_head.startswith("origin/"):
        return origin_head.removeprefix("origin/")
    branch = _git_stdout(root, ["branch", "--show-current"])
    if branch in MAINLINE_BRANCH_FALLBACKS:
        return branch
    for candidate in MAINLINE_BRANCH_FALLBACKS:
        if _git_ok(root, ["show-ref", "--verify", f"refs/heads/{candidate}"]):
            return candidate
    return ""


def collect_status(conn: Any) -> dict[str, object]:
    """Gather the bounded data set shared by TUI, markdown, and HTML reports."""

    goal = conn.execute("SELECT * FROM goals WHERE id = 1").fetchone()
    agents = conn.execute("SELECT * FROM agents ORDER BY role, name").fetchall()
    work_lanes = conn.execute("SELECT * FROM worklanes ORDER BY id DESC LIMIT 10").fetchall()
    test_run = conn.execute("SELECT * FROM test_runs ORDER BY id DESC LIMIT 1").fetchone()
    resources = conn.execute("SELECT * FROM resource_samples ORDER BY id DESC LIMIT 24").fetchall()
    metric = selected_metric(conn)
    history = metric_history(conn, str(metric["metric_name"])) if metric else []
    events = recent_events(conn, 12)
    active_work = conn.execute(
        """
        SELECT
            a.name AS agent_name,
            a.role AS agent_role,
            a.current_status AS agent_status,
            a.branch AS agent_branch,
            a.worktree AS agent_worktree,
            a.notes AS agent_notes,
            w.id AS lane_id,
            w.title AS lane_title,
            w.status AS lane_status,
            w.stage AS lane_stage,
            w.card_type AS card_type,
            w.branch_name AS lane_branch,
            w.worktree_path AS lane_worktree,
            w.expected_metric_impact AS lane_delta
        FROM agents a
        LEFT JOIN worklanes w
            ON w.stage = 'development'
            AND w.status = 'assigned'
            AND (
                w.owner_agent_id = a.id
                OR (a.branch != '' AND w.branch_name = a.branch)
                OR (a.worktree != '' AND w.worktree_path = a.worktree)
            )
        WHERE a.current_status NOT IN ('crash', 'success', 'stopped')
        ORDER BY
            CASE a.role
                WHEN 'Coordinator' THEN 0
                WHEN 'Integrator' THEN 1
                WHEN 'Developer' THEN 2
                ELSE 3
            END,
            a.name,
            w.id
        LIMIT 12
        """
    ).fetchall()
    pending_lanes = conn.execute(
        """
        SELECT
            w.id,
            w.title,
            w.status,
            w.stage,
            w.card_type,
            w.role_type,
            w.branch_name,
            w.expected_metric_impact
        FROM worklanes w
        WHERE w.stage != 'done'
          AND NOT EXISTS (
              SELECT 1
              FROM agents a
              WHERE a.current_status NOT IN ('crash', 'success', 'stopped')
                AND w.stage = 'development'
                AND w.status = 'assigned'
                AND (
                    w.owner_agent_id = a.id
                    OR (a.branch != '' AND w.branch_name = a.branch)
                    OR (a.worktree != '' AND w.worktree_path = a.worktree)
                )
          )
        ORDER BY
            CASE w.status
                WHEN 'integration_failed' THEN 0
                ELSE 1
            END,
            CASE w.stage
                WHEN 'integration' THEN 0
                WHEN 'review' THEN 1
                WHEN 'development' THEN 2
                WHEN 'planned' THEN 3
                ELSE 4
            END,
            w.priority ASC,
            w.id ASC
        LIMIT 8
        """
    ).fetchall()
    card_counts = conn.execute(
        """
        SELECT stage, COUNT(*) AS count
        FROM worklanes
        GROUP BY stage
        ORDER BY CASE stage
            WHEN 'planned' THEN 0
            WHEN 'development' THEN 1
            WHEN 'review' THEN 2
            WHEN 'integration' THEN 3
            WHEN 'done' THEN 4
            ELSE 5
        END
        """
    ).fetchall()
    uncarded_agents = [
        dict(row)
        for row in active_work
        if row["lane_id"] is None
        and row["agent_role"] not in {"Manhole", "Status reporter", "Janitor"}
        and str(row["agent_notes"] or "")
        and not str(row["agent_notes"]).startswith("Maintain ")
    ]
    queued_integration = conn.execute(
        "SELECT COUNT(*) AS count, COALESCE(SUM(expected_metric_impact), 0) AS delta FROM worklanes WHERE stage = 'integration' AND status = 'ready_for_integration'"
    ).fetchone()
    failed_integration = conn.execute(
        "SELECT COUNT(*) AS count FROM worklanes WHERE status = 'integration_failed'"
    ).fetchone()
    integration_health = conn.execute(
        """
        SELECT
            SUM(CASE WHEN stage = 'integration' AND status = 'ready_for_integration' AND branch_name != '' THEN 1 ELSE 0 END) AS ready_with_branch,
            SUM(CASE WHEN stage = 'integration' AND status = 'ready_for_integration' AND branch_name = '' THEN 1 ELSE 0 END) AS ready_missing_branch,
            SUM(CASE WHEN status = 'integration_failed'
                      AND source_key NOT LIKE 'integration-failure:%'
                      AND title NOT LIKE 'Resolve integration failure for card #%' THEN 1 ELSE 0 END) AS failed_originals,
            SUM(CASE WHEN stage != 'done'
                      AND (source_key LIKE 'integration-failure:%'
                           OR title LIKE 'Resolve integration failure for card #%') THEN 1 ELSE 0 END) AS recovery_cards
        FROM worklanes
        """
    ).fetchone()
    metadata = {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM metadata").fetchall()}
    test_gate = {
        "mode": metadata.get("test_gate_mode", ""),
        "reason": metadata.get("test_gate_reason", ""),
        "failure_count": metadata.get("test_gate_failure_count", ""),
        "run_id": metadata.get("test_gate_run_id", ""),
    }
    gate_station = _gate_station_status(conn, metadata)
    runtime_alerts = _runtime_alerts(metadata, agents, active_work, pending_lanes)
    return {
        "goal": dict(goal) if goal else None,
        "agents": [dict(row) for row in agents],
        "work_lanes": [dict(row) for row in work_lanes],
        "test_run": dict(test_run) if test_run else None,
        "resources": [dict(row) for row in resources],
        "metric": dict(metric) if metric else None,
        "metric_history": [dict(row) for row in history],
        "events": [dict(row) for row in events],
        "active_work": [dict(row) for row in active_work],
        "pending_lanes": [dict(row) for row in pending_lanes],
        "card_counts": [dict(row) for row in card_counts],
        "uncarded_agents": uncarded_agents,
        "queued_integration": dict(queued_integration) if queued_integration else {"count": 0, "delta": 0},
        "failed_integration": dict(failed_integration) if failed_integration else {"count": 0},
        "integration_health": dict(integration_health) if integration_health else {},
        "test_gate": test_gate,
        "gate_station": gate_station,
        "metadata": metadata,
        "runtime_alerts": runtime_alerts,
    }


def dashboard(conn: Any) -> str:
    """Return the Unicode/ANSI dashboard printed by ./harness status and poke."""

    data = collect_status(conn)
    generated = utc_now()
    width = 96
    lines = [_box_top(width), _box_line(width, f"Last generated: {generated}", ANSI["bold"])]
    goal = data["goal"] or {}
    metric = data["metric"] or {}
    goal_text = goal.get("text", "No goal recorded yet") if isinstance(goal, dict) else "No goal recorded yet"
    percent = float(metric.get("percent_ready", 0) if isinstance(metric, dict) else 0)
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    runtime_alerts = data.get("runtime_alerts") if isinstance(data.get("runtime_alerts"), list) else []
    for alert in runtime_alerts:
        lines.append(_box_line(width, f"!!! {alert} !!!", ANSI["red"] + ANSI["bold"]))
    red_banner = metadata.get("red_banner", "")
    if red_banner:
        lines.append(_box_line(width, red_banner, ANSI["red"]))
    lines.append(_box_line(width, f"Goal: {shorten(str(goal_text), width=width-10, placeholder='…')}"))
    progress_count = _progress_count_text(metric)
    progress_suffix = f" ({progress_count})" if progress_count else ""
    lines.append(_box_line(width, f"Progress: {_bar(percent, 24)} {percent:5.1f}%{progress_suffix}"))

    agents = data["agents"]
    active = sum(1 for a in agents if is_active_agent_status(str(a.get("current_status", ""))))
    crashed = sum(1 for a in agents if a.get("current_status") == "crash")
    agent_color = ANSI["green"] if crashed == 0 else ANSI["red"]
    lines.append(_box_line(width, f"Agents: {active} active, {crashed} crashed, {len(agents)} tracked", agent_color))

    test_run = data["test_run"]
    if isinstance(test_run, dict) and test_run:
        test_color = ANSI["green"] if test_run.get("status") == "passed" else ANSI["red"]
        lines.append(_box_line(width, f"Latest tests: {test_run.get('status')} via {test_run.get('command')}", test_color))
    else:
        lines.append(_box_line(width, "Latest tests: no recorded test runs", ANSI["yellow"]))
    test_gate = data.get("test_gate")
    if isinstance(test_gate, dict) and test_gate.get("mode") == "hard_blocker":
        lines.append(_box_line(width, f"Gate: HARD BLOCKER — {test_gate.get('reason')}", ANSI["red"] + ANSI["bold"]))
    elif isinstance(test_gate, dict) and test_gate.get("mode") == "soft_known_red":
        lines.append(_box_line(width, f"Gate: SOFT KNOWN-RED — {test_gate.get('failure_count')} known failures, progress allowed", ANSI["yellow"]))
    elif isinstance(test_gate, dict) and test_gate.get("mode") == "quarantined_known_red":
        lines.append(_box_line(width, f"Gate: KNOWN-RED QUARANTINE — {test_gate.get('failure_count')} known failures, progress allowed", ANSI["yellow"]))
    gate_station = data.get("gate_station")
    if isinstance(gate_station, dict) and gate_station.get("mode") == "1":
        first_target = str(gate_station.get("first_failing_target") or "unknown")
        lines.append(_box_line(width, f"First failing target: {first_target}", ANSI["yellow"]))
        lines.append(
            _box_line(
                width,
                f"No-fail-fast failing targets: {gate_station.get('inventory_failure_count', 0)} | "
                f"Focused tests cleared this session: {gate_station.get('focused_tests_cleared_session', 0)}",
                ANSI["yellow"],
            )
        )
        lines.append(_box_line(width, f"Full gate: passed={gate_station.get('full_gate_passed', 0)}, failed={gate_station.get('full_gate_failed', 0)}"))
        owners = gate_station.get("cluster_owners")
        if isinstance(owners, list) and owners:
            owner_text = ", ".join(
                f"{owner.get('owner') or owner.get('source_key')}→{owner.get('agent_name') or 'unassigned'}"
                for owner in owners[:3]
            )
            lines.append(_box_line(width, f"Gate clusters: {owner_text}", ANSI["yellow"]))

    resources = data["resources"]
    if resources:
        latest = resources[0]
        color = ANSI["red"] if latest.get("cpu_percent", 0) >= 95 or latest.get("ram_percent", 0) >= 95 else ANSI["blue"]
        lines.append(_box_line(width, f"CPU {latest.get('cpu_percent', 0):.1f}% | RAM {latest.get('ram_percent', 0):.1f}% | disk free {latest.get('disk_free_gb', 0):.1f} GB", color))

    queued = data["queued_integration"]
    if isinstance(queued, dict):
        lines.append(_box_line(width, f"Awaiting integration: {queued.get('count', 0)} lanes, expected metric delta {queued.get('delta', 0)}"))
    failed = data.get("failed_integration")
    if isinstance(failed, dict) and failed.get("count", 0):
        lines.append(_box_line(width, f"Integration failed queue: {failed.get('count', 0)} lanes", ANSI["red"]))
    integration_health = data.get("integration_health")
    if isinstance(integration_health, dict) and integration_health:
        lines.append(
            _box_line(
                width,
                "Integration: "
                f"ready with branch={integration_health.get('ready_with_branch') or 0}, "
                f"missing branch={integration_health.get('ready_missing_branch') or 0}, "
                f"failed originals={integration_health.get('failed_originals') or 0}, "
                f"recovery cards={integration_health.get('recovery_cards') or 0}",
                ANSI["yellow"] if (integration_health.get("failed_originals") or integration_health.get("recovery_cards")) else "",
            )
        )
    card_counts = _card_count_text(data.get("card_counts", []))
    if card_counts:
        lines.append(_box_line(width, f"Cards: {card_counts}"))

    lines.append(_box_sep(width))
    uncarded = data.get("uncarded_agents", [])
    if isinstance(uncarded, list) and uncarded:
        lines.append(_box_line(width, "Uncarded active work", ANSI["red"]))
        for row in uncarded[:4]:
            lines.append(_box_line(width, f"{row.get('agent_name')} [{row.get('agent_role')}] has no card: {row.get('agent_notes')}", ANSI["red"]))
        lines.append(_box_sep(width))
    lines.append(_box_line(width, "Active work (agents ↔ cards)", ANSI["bold"]))
    work_lines = _active_work_lines(data.get("active_work", []))
    for line in work_lines[:8]:
        lines.append(_box_line(width, line))
    extra = max(0, len(work_lines) - 8)
    if extra:
        lines.append(_box_line(width, f"… {extra} more active work rows"))
    lane_lines = _pending_lane_lines(data.get("pending_lanes", []))
    if lane_lines:
        lines.append(_box_line(width, "Unassigned cards", ANSI["bold"]))
        for line in lane_lines:
            lines.append(_box_line(width, line))

    lines.append(_box_sep(width))
    lines.append(_box_line(width, "Recent events", ANSI["bold"]))
    for event in data["events"][-8:]:
        lines.append(_box_line(width, f"{event['ts']} {event['type']}: {shorten(event['message'], width=width-32, placeholder='…')}"))
    lines.append(_box_bottom(width))
    return "\n".join(lines)


def _active_work_lines(rows: object) -> list[str]:
    """Render active agent/card correlations for the compact TUI."""

    if not isinstance(rows, list) or not rows:
        return ["No active agents or assigned lanes."]
    lines: list[str] = []
    for row in rows:
        agent = str(row.get("agent_name") or "unknown-agent")
        role = str(row.get("agent_role") or "agent")
        status = str(row.get("agent_status") or "unknown")
        lane_id = row.get("lane_id")
        if lane_id is None:
            note = str(row.get("agent_notes") or "no assigned lane")
            if note.startswith("Maintain "):
                continue
            lane = f"no lane · {note}" if note else "no lane"
        else:
            title = str(row.get("lane_title") or "untitled")
            lane_status = str(row.get("lane_status") or "unknown")
            lane_stage = str(row.get("lane_stage") or "unknown")
            lane = f"card#{lane_id} {lane_stage}/{lane_status}: {title}"
        branch = str(row.get("lane_branch") or row.get("agent_branch") or "")
        if branch:
            lane = f"{lane} ({branch})"
        lines.append(f"{agent} [{role}/{status}] → {lane}")
    return lines or ["No active agents or assigned lanes."]


def _pending_lane_lines(rows: object) -> list[str]:
    """Render non-done cards that have no active agent."""

    if not isinstance(rows, list) or not rows:
        return []
    lines: list[str] = []
    for row in rows:
        lane_id = row.get("id")
        status = str(row.get("status") or "unknown")
        stage = str(row.get("stage") or "unknown")
        role = str(row.get("role_type") or "Developer")
        title = str(row.get("title") or "untitled")
        branch = str(row.get("branch_name") or "")
        branch_text = f" ({branch})" if branch else ""
        lines.append(f"card#{lane_id} {stage}/{status}/{role}: {title}{branch_text}")
    return lines


def _card_count_text(rows: object) -> str:
    """Render card counts in board order."""

    if not isinstance(rows, list) or not rows:
        return ""
    return ", ".join(f"{row.get('stage')}={row.get('count')}" for row in rows)


def _markdown_context(data: dict[str, object]) -> dict[str, str]:
    """Format status data for the markdown template."""

    goal = data.get("goal") or {}
    metric = data.get("metric") or {}
    return {
        "generated_at": str(data["generated_at"]),
        "runtime_alerts": "\n".join(f"- **{alert}**" for alert in data.get("runtime_alerts", [])) or "None.",
        "goal": str(goal.get("text", "No goal recorded yet") if isinstance(goal, dict) else "No goal recorded yet"),
        "metric": _metric_text(metric),
        "agents": _markdown_table(data.get("agents", []), ["name", "role", "current_status", "tmux_window", "worktree"]),
        "work_lanes": _markdown_table(data.get("work_lanes", []), ["id", "title", "role_type", "card_type", "stage", "status", "integration_queue", "expected_metric_impact"]),
        "tests": _test_text(data.get("test_run")) + _gate_station_text(data.get("gate_station")),
        "resources": _resource_text(data.get("resources", [])),
        "events": "\n".join(f"- {e['ts']} **{e['type']}**: {e['message']}" for e in data.get("events", [])) or "No events yet.",
        "next_steps": _next_steps(data),
    }


def _html_context(data: dict[str, object]) -> dict[str, str]:
    """Format status data for the HTML template without inserting raw event text."""

    metric = data.get("metric") or {}
    percent = float(metric.get("percent_ready", 0) if isinstance(metric, dict) else 0)
    goal = data.get("goal") or {}
    return {
        "generated_at": html.escape(str(data["generated_at"])),
        "runtime_alerts_html": _html_runtime_alerts(data.get("runtime_alerts", [])),
        "metric_percent": f"{percent:.1f}",
        "goal_html": html.escape(str(goal.get("text", "No goal recorded yet") if isinstance(goal, dict) else "No goal recorded yet")),
        "metric_html": html.escape(_metric_text(metric)),
        "metric_chart_html": _sparkline(data.get("metric_history", []), "percent_ready"),
        "resources_html": _html_resource(data.get("resources", [])),
        "tests_html": html.escape(_test_text(data.get("test_run")) + _gate_station_text(data.get("gate_station"))).replace("\n", "<br>"),
        "agents_html": _html_table(data.get("agents", []), ["name", "role", "current_status", "tmux_window", "worktree"]),
        "work_lanes_html": _html_table(data.get("work_lanes", []), ["id", "title", "role_type", "card_type", "stage", "status", "integration_queue", "expected_metric_impact"]),
        "events_html": "<ul>" + "".join(f"<li>{html.escape(e['ts'])} <strong>{html.escape(e['type'])}</strong>: {html.escape(e['message'])}</li>" for e in data.get("events", [])) + "</ul>",
        "next_steps_html": html.escape(_next_steps(data)),
    }


def _render(template: str, context: dict[str, str]) -> str:
    """Tiny placeholder renderer keeps reports dependency-free and editable."""

    rendered = template
    for key, value in context.items():
        rendered = rendered.replace("{{" + key + "}}", value)
    return rendered


def _metric_text(metric: object) -> str:
    if isinstance(metric, dict) and metric:
        return f"{metric.get('metric_name')}: {metric.get('value')} / {metric.get('target')} ({float(metric.get('percent_ready', 0)):.1f}%)"
    return "No progress metric samples recorded yet."


def _progress_count_text(metric: object) -> str:
    """Render the progress numerator/target for the compact TUI."""

    if not isinstance(metric, dict) or not metric:
        return ""
    return f"{_display_number(metric.get('value'))} / {_display_number(metric.get('target'))} {metric.get('metric_name')}"


def _display_number(value: object) -> str:
    """Format database numeric values without distracting .0 suffixes."""

    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return str(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:g}"


def _test_text(test_run: object) -> str:
    if isinstance(test_run, dict) and test_run:
        summary = json.loads(test_run.get("summary_json") or "{}")
        bits = ", ".join(f"{k}={v}" for k, v in sorted(summary.items()))
        return f"{test_run.get('status')} — {test_run.get('command')}" + (f" ({bits})" if bits else "")
    return "No test runs recorded yet."


def _gate_station_status(conn: Any, metadata: dict[str, str]) -> dict[str, object]:
    """Collect red-gate stabilization counters without mixing them with PHPT progress."""

    full_gate = conn.execute(
        """
        SELECT
            SUM(CASE WHEN status = 'passed' THEN 1 ELSE 0 END) AS passed,
            SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed
        FROM test_runs
        WHERE command LIKE '%tools/run-tests.sh%'
        """
    ).fetchone()
    owners = conn.execute(
        """
        SELECT
            w.id AS card_id,
            w.source_key,
            w.title,
            a.name AS agent_name
        FROM worklanes w
        LEFT JOIN agents a
            ON a.id = w.owner_agent_id
            AND a.current_status NOT IN ('crash', 'success', 'stopped')
            AND a.ended_at IS NULL
        WHERE w.stage != 'done'
          AND w.source_key LIKE 'gate-cluster:%'
        ORDER BY w.id ASC
        LIMIT 8
        """
    ).fetchall()
    return {
        "mode": metadata.get("gate_station_mode", "0"),
        "inventory_failure_count": int(float(metadata.get("gate_station_inventory_failure_count", "0") or 0)),
        "first_failing_target": metadata.get("gate_station_first_failing_target", ""),
        "focused_tests_cleared_session": int(float(metadata.get("focused_tests_cleared_session", "0") or 0)),
        "full_gate_passed": int(full_gate["passed"] or 0) if full_gate else 0,
        "full_gate_failed": int(full_gate["failed"] or 0) if full_gate else 0,
        "cluster_owners": [
            {
                "card_id": row["card_id"],
                "source_key": row["source_key"],
                "owner": str(row["source_key"] or "").removeprefix("gate-cluster:"),
                "title": row["title"],
                "agent_name": row["agent_name"] or "",
            }
            for row in owners
        ],
    }


def _gate_station_text(gate_station: object) -> str:
    """Render gate-station counters for STATUS.md/HTML test sections."""

    if not isinstance(gate_station, dict) or gate_station.get("mode") != "1":
        return ""
    lines = [
        "",
        f"Gate station: first failing target={gate_station.get('first_failing_target') or 'unknown'}",
        f"No-fail-fast failing targets: {gate_station.get('inventory_failure_count', 0)}",
        f"Focused tests cleared this session: {gate_station.get('focused_tests_cleared_session', 0)}",
        f"Full gate: passed={gate_station.get('full_gate_passed', 0)}, failed={gate_station.get('full_gate_failed', 0)}",
    ]
    owners = gate_station.get("cluster_owners")
    if isinstance(owners, list) and owners:
        rendered = ", ".join(f"{row.get('owner')}→{row.get('agent_name') or 'unassigned'}" for row in owners[:8])
        lines.append(f"Failure cluster owners: {rendered}")
    return "\n" + "\n".join(lines)


def _resource_text(resources: object) -> str:
    if not isinstance(resources, list) or not resources:
        return "No resource samples recorded yet."
    latest = resources[0]
    return f"Latest: CPU {latest.get('cpu_percent')}%, RAM {latest.get('ram_percent')}%, disk free {latest.get('disk_free_gb')} GB."


def _html_resource(resources: object) -> str:
    if not isinstance(resources, list) or not resources:
        return "<p>No resource samples recorded yet.</p>"
    rows = ["<table><tr><th>Time</th><th>CPU</th><th>RAM</th><th>Disk free</th></tr>"]
    for sample in resources[:24]:
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(sample.get('ts')))}</td>"
            f"<td>{sample.get('cpu_percent')}%</td>"
            f"<td>{sample.get('ram_percent')}%</td>"
            f"<td>{sample.get('disk_free_gb')} GB</td>"
            "</tr>"
        )
    rows.append("</table>")
    return "".join(rows)


def _html_runtime_alerts(alerts: object) -> str:
    """Render runtime alerts prominently in the HTML status page."""

    if not isinstance(alerts, list) or not alerts:
        return ""
    items = "".join(f"<li>{html.escape(str(alert))}</li>" for alert in alerts)
    return f"<div class=\"card bad\"><h2>Runtime alerts</h2><ul>{items}</ul></div>"


def _runtime_alerts(metadata: dict[str, str], agents: list[Any], active_work: list[Any], pending_lanes: list[Any]) -> list[str]:
    """Return visible status alerts for dead harness control-plane processes."""

    alerts: list[str] = []
    if metadata.get("harness_stopped") == "1" or metadata.get("red_banner") == "Harness stopped.":
        return alerts
    active_agents = sum(1 for agent in agents if is_active_agent_status(str(agent["current_status"])))
    scheduler_pid = str(metadata.get("scheduler_pid", "")).strip()
    if scheduler_pid and scheduler_pid != "0":
        try:
            pid = int(scheduler_pid)
        except ValueError:
            alerts.append(f"HARNESS SCHEDULER STATUS UNKNOWN: invalid scheduler pid {scheduler_pid!r}")
        else:
            command = _process_command(pid)
            if command is None:
                alerts.append(f"HARNESS SCHEDULER DEAD: recorded ./harness run pid {pid} is not running")
            elif command and not _looks_like_scheduler_command(command):
                alerts.append(f"HARNESS SCHEDULER PID STALE: pid {pid} now runs {shorten(command, width=48, placeholder='…')}")
    elif active_agents:
        alerts.append(f"HARNESS SCHEDULER NOT RECORDED: {active_agents} active agents exist but no ./harness run pid is recorded")

    active_lane_ids = {int(row["lane_id"]) for row in active_work if row["lane_id"] is not None}
    pending_lane_ids = {int(row["id"]) for row in pending_lanes if row["id"] is not None}
    overlap = sorted(active_lane_ids & pending_lane_ids)
    if overlap:
        shown = ", ".join(f"card#{card_id}" for card_id in overlap[:5])
        suffix = f", and {len(overlap) - 5} more" if len(overlap) > 5 else ""
        alerts.append(f"CARD BOARD INCONSISTENT: {shown}{suffix} appear both active and unassigned")
    return alerts


def _process_command(pid: int) -> str | None:
    """Return a process command, an empty string if hidden, or None if absent."""

    try:
        os.kill(pid, 0)
    except OSError:
        return None
    try:
        result = subprocess.run(["ps", "-p", str(pid), "-o", "command="], text=True, capture_output=True, check=False, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _looks_like_scheduler_command(command: str) -> bool:
    """Return whether a process command still looks like harness run/watchdog."""

    normalized = " ".join(command.lower().split())
    return "harness" in normalized and (" run" in normalized or " watchdog" in normalized)


def _sparkline(rows: object, value_key: str) -> str:
    """Render a tiny inline SVG trend without pulling in a charting library."""

    if not isinstance(rows, list) or len(rows) < 2:
        return "<p>No metric history chart yet.</p>"
    ordered = list(reversed(rows))
    values = [float(row.get(value_key, 0) or 0) for row in ordered]
    high = max(values) or 1.0
    low = min(values)
    span = high - low or 1.0
    points = []
    width = 240
    height = 64
    for index, value in enumerate(values):
        x = 0 if len(values) == 1 else (index / (len(values) - 1)) * width
        y = height - ((value - low) / span) * height
        points.append(f"{x:.1f},{y:.1f}")
    return (
        "<svg role=\"img\" aria-label=\"Progress trend\" viewBox=\"0 0 240 64\" width=\"100%\" height=\"64\">"
        "<polyline fill=\"none\" stroke=\"#2563eb\" stroke-width=\"3\" points=\""
        + " ".join(points)
        + "\"/></svg>"
    )


def _markdown_table(rows: object, columns: list[str]) -> str:
    if not isinstance(rows, list) or not rows:
        return "None."
    out = ["| " + " | ".join(columns) + " |", "| " + " | ".join("---" for _ in columns) + " |"]
    for row in rows:
        out.append("| " + " | ".join(str(row.get(col, "")).replace("\n", " ") for col in columns) + " |")
    return "\n".join(out)


def _html_table(rows: object, columns: list[str]) -> str:
    if not isinstance(rows, list) or not rows:
        return "<p>None.</p>"
    out = ["<table><tr>" + "".join(f"<th>{html.escape(col)}</th>" for col in columns) + "</tr>"]
    for row in rows:
        out.append("<tr>" + "".join(f"<td>{html.escape(str(row.get(col, '')))}</td>" for col in columns) + "</tr>")
    out.append("</table>")
    return "".join(out)


def _next_steps(data: dict[str, object]) -> str:
    lanes = data.get("work_lanes", [])
    if isinstance(lanes, list) and lanes:
        queued = [lane for lane in lanes if lane.get("status") in {"queued", "assigned", "needs_verification", "ready_for_integration"}]
        if queued:
            return "Move next worklane forward: " + str(queued[0].get("title"))
    if not data.get("goal"):
        return "Capture the user goal and deterministic success metric."
    return "Keep scheduler loops running, refresh status, and audit measurable progress."


def _bar(percent: float, width: int) -> str:
    filled = int(round((max(0, min(percent, 100)) / 100) * width))
    return "█" * filled + "░" * (width - filled)


def _box_top(width: int) -> str:
    return "┌" + "─" * (width - 2) + "┐"


def _box_sep(width: int) -> str:
    return "├" + "─" * (width - 2) + "┤"


def _box_bottom(width: int) -> str:
    return "└" + "─" * (width - 2) + "┘"


def _box_line(width: int, text: str, color: str = "") -> str:
    plain = shorten(_clean_tui_text(text), width=width - 4, placeholder="…")
    padding = " " * max(0, width - 4 - len(plain))
    if color:
        plain = f"{color}{plain}{ANSI['reset']}"
    return f"│ {plain}{padding} │"


def _clean_tui_text(text: str) -> str:
    """Prevent stored terminal control sequences from corrupting the TUI."""

    cleaned = ANSI_ESCAPE_RE.sub("", str(text))
    cleaned = CONTROL_RE.sub("", cleaned)
    return cleaned.replace("\r", " ").replace("\n", " ").replace("\t", " ")
