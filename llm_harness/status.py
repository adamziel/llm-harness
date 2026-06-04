"""Status dashboard and report generation for the harness."""

from __future__ import annotations

import html
import json
import sqlite3
from pathlib import Path
from textwrap import shorten

from .db import latest_metric, recent_events, utc_now

ANSI = {
    "reset": "\033[0m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "bold": "\033[1m",
}

MD_TEMPLATE = """# Harness Status

Last generated: {{generated_at}}

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


def refresh_reports(conn: sqlite3.Connection, root: str | Path) -> tuple[Path, Path]:
    """Render STATUS.md and STATUS.html from SQLite so status is restart-safe."""

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
    return status_md, status_html


def collect_status(conn: sqlite3.Connection) -> dict[str, object]:
    """Gather the bounded data set shared by TUI, markdown, and HTML reports."""

    goal = conn.execute("SELECT * FROM goals WHERE id = 1").fetchone()
    agents = conn.execute("SELECT * FROM agents ORDER BY role, name").fetchall()
    work_lanes = conn.execute("SELECT * FROM work_lanes ORDER BY id DESC LIMIT 10").fetchall()
    test_run = conn.execute("SELECT * FROM test_runs ORDER BY id DESC LIMIT 1").fetchone()
    resources = conn.execute("SELECT * FROM resource_samples ORDER BY id DESC LIMIT 24").fetchall()
    metric = latest_metric(conn)
    metric_history = conn.execute("SELECT * FROM metric_samples ORDER BY id DESC LIMIT 24").fetchall()
    events = recent_events(conn, 12)
    queued_integration = conn.execute(
        "SELECT COUNT(*) AS count, COALESCE(SUM(expected_metric_delta), 0) AS delta FROM work_lanes WHERE status = 'awaiting_integration'"
    ).fetchone()
    metadata = {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM metadata").fetchall()}
    return {
        "goal": dict(goal) if goal else None,
        "agents": [dict(row) for row in agents],
        "work_lanes": [dict(row) for row in work_lanes],
        "test_run": dict(test_run) if test_run else None,
        "resources": [dict(row) for row in resources],
        "metric": dict(metric) if metric else None,
        "metric_history": [dict(row) for row in metric_history],
        "events": [dict(row) for row in events],
        "queued_integration": dict(queued_integration) if queued_integration else {"count": 0, "delta": 0},
        "metadata": metadata,
    }


def dashboard(conn: sqlite3.Connection) -> str:
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
    red_banner = metadata.get("red_banner", "")
    if red_banner:
        lines.append(_box_line(width, red_banner, ANSI["red"]))
    lines.append(_box_line(width, f"Goal: {shorten(str(goal_text), width=width-10, placeholder='…')}"))
    lines.append(_box_line(width, f"Progress: {_bar(percent, 24)} {percent:5.1f}%"))

    agents = data["agents"]
    running = sum(1 for a in agents if a.get("current_status") == "running")
    crashed = sum(1 for a in agents if a.get("current_status") == "crash")
    agent_color = ANSI["green"] if crashed == 0 else ANSI["red"]
    lines.append(_box_line(width, f"Agents: {running} running, {crashed} crashed, {len(agents)} tracked", agent_color))

    test_run = data["test_run"]
    if isinstance(test_run, dict) and test_run:
        test_color = ANSI["green"] if test_run.get("status") == "passed" else ANSI["red"]
        lines.append(_box_line(width, f"Latest tests: {test_run.get('status')} via {test_run.get('command')}", test_color))
    else:
        lines.append(_box_line(width, "Latest tests: no recorded test runs", ANSI["yellow"]))

    resources = data["resources"]
    if resources:
        latest = resources[0]
        color = ANSI["red"] if latest.get("cpu_percent", 0) >= 95 or latest.get("ram_percent", 0) >= 95 else ANSI["blue"]
        lines.append(_box_line(width, f"CPU {latest.get('cpu_percent', 0):.1f}% | RAM {latest.get('ram_percent', 0):.1f}% | disk free {latest.get('disk_free_gb', 0):.1f} GB", color))

    queued = data["queued_integration"]
    if isinstance(queued, dict):
        lines.append(_box_line(width, f"Awaiting integration: {queued.get('count', 0)} lanes, expected metric delta {queued.get('delta', 0)}"))

    lines.append(_box_sep(width))
    lines.append(_box_line(width, "Recent events", ANSI["bold"]))
    for event in data["events"][-8:]:
        lines.append(_box_line(width, f"{event['ts']} {event['type']}: {shorten(event['message'], width=width-32, placeholder='…')}"))
    lines.append(_box_bottom(width))
    return "\n".join(lines)


def _markdown_context(data: dict[str, object]) -> dict[str, str]:
    """Format status data for the markdown template."""

    goal = data.get("goal") or {}
    metric = data.get("metric") or {}
    return {
        "generated_at": str(data["generated_at"]),
        "goal": str(goal.get("text", "No goal recorded yet") if isinstance(goal, dict) else "No goal recorded yet"),
        "metric": _metric_text(metric),
        "agents": _markdown_table(data.get("agents", []), ["name", "role", "current_status", "tmux_window", "worktree"]),
        "work_lanes": _markdown_table(data.get("work_lanes", []), ["id", "title", "role", "status", "expected_metric_delta"]),
        "tests": _test_text(data.get("test_run")),
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
        "metric_percent": f"{percent:.1f}",
        "goal_html": html.escape(str(goal.get("text", "No goal recorded yet") if isinstance(goal, dict) else "No goal recorded yet")),
        "metric_html": html.escape(_metric_text(metric)),
        "metric_chart_html": _sparkline(data.get("metric_history", []), "percent_ready"),
        "resources_html": _html_resource(data.get("resources", [])),
        "tests_html": html.escape(_test_text(data.get("test_run"))).replace("\n", "<br>"),
        "agents_html": _html_table(data.get("agents", []), ["name", "role", "current_status", "tmux_window", "worktree"]),
        "work_lanes_html": _html_table(data.get("work_lanes", []), ["id", "title", "role", "status", "expected_metric_delta"]),
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


def _test_text(test_run: object) -> str:
    if isinstance(test_run, dict) and test_run:
        summary = json.loads(test_run.get("summary_json") or "{}")
        bits = ", ".join(f"{k}={v}" for k, v in sorted(summary.items()))
        return f"{test_run.get('status')} — {test_run.get('command')}" + (f" ({bits})" if bits else "")
    return "No test runs recorded yet."


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
        queued = [lane for lane in lanes if lane.get("status") in {"queued", "ready"}]
        if queued:
            return "Start or finish queued lane: " + str(queued[0].get("title"))
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
    plain = shorten(text, width=width - 4, placeholder="…")
    padding = " " * max(0, width - 4 - len(plain))
    if color:
        plain = f"{color}{plain}{ANSI['reset']}"
    return f"│ {plain}{padding} │"
