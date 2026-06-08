# Harness Turso MCP

Use this skill in every Codex agent launched by the harness.

The harness exposes a local MCP server backed by `.harness/harness.turso`.
Use it as the shared memory and coordination surface instead of relying on chat
history alone.

Tools:

- `memory_record_event(type, message, agent_name?, payload?)` — record decisions,
  crashes, prompts sent, completed work, and anything the scheduler or Status
  reporter should know.
- `memory_query(sql, params?)` — read harness tables with `SELECT`, `WITH`, or
  `PRAGMA`. Useful tables include `goals`, `agents`, `worklanes`,
  `integration_attempts`, `agent_reports`, `test_runs`, `test_results`,
  `issues`, `events`, `resource_samples`, and `metric_samples`. Use
  `PRAGMA table_xinfo(table)` before assuming column names.
- `memory_update_agent(name, status, notes?, ended?)` — update your own status
  frequently enough for the watchdog to tell progress from idleness.
- `agent_report(...)` — submit structured Developer or Integrator reports.
  Structured reports are authoritative for worklane status.
- `spawn_agent(role, title, prompt, requester?, notes?)` — request another agent.
  This deliberately routes through the central scheduler so the harness knows the
  whole process tree. Do not start Codex directly.
- `code_search(query, worktree?, limit?, refresh?)` — search code with ripgrep or
  the Turso fallback index before doing broad manual greps.

Keep writes concise and structured. Prefer several small events over one giant
note that will waste future context.
