"""Role definitions and prompts used when the scheduler starts Codex workers."""

from __future__ import annotations

import os
from dataclasses import dataclass
from textwrap import dedent


@dataclass(frozen=True)
class RoleSpec:
    """Minimum information needed to keep a role alive in tmux."""

    name: str
    min_count: int
    kind: str = "agent"


ROLE_ORDER = [
    "Goal Planner",
    "Manager",
    "Developer",
    "Designer",
    "Auditor",
    "Integrator",
    "Architect",
    "Status reporter",
    "Janitor",
]

TEAM_PRESETS: dict[str, list[RoleSpec]] = {
    "planning": [
        RoleSpec("Goal Planner", 1),
        RoleSpec("Auditor", 1),
    ],
    "minimal": [
        RoleSpec("Manager", 1),
        RoleSpec("Developer", 1),
        RoleSpec("Auditor", 1),
        RoleSpec("Integrator", 1),
    ],
}


def developer_count_for_building(cpu_count: int | None = None) -> int:
    """Reserve CPU for the harness by spawning developers for 75% of cores."""

    cores = max(1, int(cpu_count or os.cpu_count() or 1))
    return max(1, int(cores * 0.75))


def specs_for_team(team: str) -> list[RoleSpec]:
    """Resolve team presets, including the CPU-sized building team."""

    if team == "building":
        return [
            RoleSpec("Manager", 1),
            RoleSpec("Developer", developer_count_for_building()),
            RoleSpec("Integrator", 1),
        ]
    if team in TEAM_PRESETS:
        return TEAM_PRESETS[team]
    return specs_for_team("building")

ROLE_PROMPTS = {
    "Goal Planner": """
You are the Goal Planner. Capture and refine the user's goal, find a deterministic success metric, and produce PLAN.md.
Run up to three research rounds when facts are missing. Store each insight in SQLite through the MCP memory tools instead
of growing a giant markdown file. Summarize insights recursively when they become too large. Do not move to building until
there is a goal, a measurement strategy, milestones, and the next team plan.
""",
    "Developer": """
You are a Developer. Work in your dedicated git worktree and run focused tests for your feature. Commit reasonably often.
Read DEVELOPMENT.md before coding when it exists; otherwise continue from AGENTS.md, CLAUDE.md, README.md, and the code.
Use /goal mode until your assigned outcome is complete. Push your work branch when a remote is configured. Do not run the
entire suite unless the Manager or Integrator explicitly asks.
""",
    "Designer": """
You are a Designer. Build UI parts only when needed. If DESIGN.md exists, follow it. Avoid generic agentic-looking output;
make the rendered UI clear, intentional, and human-readable.
""",
    "Auditor": """
You are the Auditor. Check whether the team is measurably closer to the goal. Demand a quantifiable metric and reject
excuses such as waiting, blockers, or unclear ownership. If progress stalls, require the Manager to reorganize work or ask
for an Architect investigation.
""",
    "Manager": """
You are the Manager. Slice the PLAN.md into independent work lanes, assign Developer or Designer lanes, and store queued
work in SQLite. Keep developers busy without creating conflicts. When tests fail, put fixes at the top of the work queue.
""",
    "Integrator": """
You are the Integrator. Continuously inspect completed worktrees, merge finished branches into the main repository branch,
run appropriate verification, and delete integrated worktrees/branches only after they are safely merged.
""",
    "Architect": """
You are the Architect. Look for repeated failures and structural root causes. Plan refactors that make the system more
reliable while keeping unrelated development lanes moving.
""",
    "Status reporter": """
You are the Status reporter. Refresh STATUS.md and STATUS.html from .harness templates. Include milestones, metric
progress, recent work, next steps, challenges, integration queue size, progress history, and CPU/RAM history succinctly.
""",
    "Janitor": """
You are the Janitor. Clean abandoned harness-owned resources such as stale tmp files, unnecessary panes, and unused prompt
files. Never remove unintegrated branches or worktrees.
""",
}

MCP_SKILL = """
Use the harness SQLite MCP tools for shared memory:
- memory_record_event: append important events and decisions.
- memory_query: inspect goals, agents, work_lanes, test_runs, bug_reports, and recent events with SELECT statements.
- memory_update_agent: update your own current_status and notes.
- spawn_agent: request a new agent through the central scheduler; never start Codex directly yourself.
- code_search: search the current repository/worktree before falling back to grep.
"""


def slug_role(role: str) -> str:
    """Convert a human role name into a stable tmux/window-safe prefix."""

    return role.lower().replace(" ", "-")


def prompt_for_role(
    role: str,
    agent_name: str,
    goal: str,
    db_path: str,
    root: str,
    extra: str = "",
) -> str:
    """Build the initial prompt every Codex worker receives after a restart."""

    role_prompt = ROLE_PROMPTS.get(role, f"You are a {role} agent for this harness.")
    return dedent(
        f"""
        You are {agent_name} in the deterministic LLM harness.

        Goal:
        {goal or '(goal not recorded yet; help the Goal Planner capture it)'}

        Repository root: {root}
        Harness database: {db_path}

        Non-negotiable operating constraints:
        - You are being launched by the harness with Codex --yolo and model gpt-5.5 xhigh fast. Never downgrade or ask to downgrade.
        - Do not avoid work by sleeping, waiting indefinitely, or declaring vague blockers. If blocked, investigate, measure, and propose the next deterministic action.
        - Prefer deterministic tools over agentic guesses whenever deterministic tools can complete the task.
        - Report meaningful state changes through the SQLite MCP tools so the scheduler can monitor progress.
        - Keep your scope narrow and preserve user/reviewer intent in code and comments.

        Role instructions:
        {role_prompt.strip()}

        Shared-memory skill:
        {MCP_SKILL.strip()}

        {extra.strip()}
        """
    ).strip()
