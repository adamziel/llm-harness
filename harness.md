# LLM Harness System — Revised Spec

Implement a Python script that implements an LLM agent harness for driving concurrent software development with Codex CLI sessions.

The purpose of the harness is to compensate for agentic unreliability using deterministic supervision. Anytime a task can be completed by deterministic tools — function calls, shell calls, requests, SQLite queries, Git commands, process checks, test runners, etc. — prefer deterministic tools. If a task can only be completed by prompting an agent, implement it that way, but ensure the harness detects refusal, failure, avoidance, idle drift, repeated waiting, excessive sleeping, and lack of measurable progress. The harness must have feedback loops that keep work moving in a timely manner.

This system must **not** be designed as a single global state machine where the whole harness moves through blocking phases. The harness should instead be a **concurrent control plane**: independent deterministic loops and a small number of resident Codex sessions coordinate through SQLite, Git worktrees, tmux, structured reports, and measurable progress signals.

The core principle is:

> Roles are capabilities, not necessarily always-running Codex sessions.

Most roles should be deterministic loops, short-lived agent jobs, or temporary modes of an existing resident session. We should not need 15 standing agents just to keep the system afloat.

## High-level architecture

The top-level harness scheduler is an event-driven concurrent supervisor that creates, manages, monitors, prompts, kills, and restarts Codex CLI processes.

We do not trust Codex sessions to reliably do what we asked. We treat them as lazy, drifting, forgetful, and likely to look for easier solutions than requested. They may forget the goal, avoid measuring progress, claim blockers too early, wait unnecessarily, run `sleep`, or assume the system is blocked. The harness fights this with deterministic structures:

* watchdog loops,
* process supervision,
* SQLite-backed durable state,
* structured agent reports,
* test loops,
* worklane queues,
* integration queues,
* resource monitors,
* idle detection,
* crash recovery,
* bounded integration attempts,
* status rendering,
* cleanup routines.

Codex processes are isolated enough that one crashing session does not crash the entire harness scheduler.

The system is organized around **worklanes**, not global phases.

A worklane is an independently schedulable unit of work with:

* a goal,
* an owner,
* a worktree,
* a base branch,
* acceptance criteria,
* current status,
* recent activity,
* expected metric impact,
* conflict risk,
* integration status,
* test evidence.

Development, verification, testing, integration, auditing, reporting, cleanup, and planning should happen concurrently whenever possible. No subsystem should wait for another subsystem except where a specific worklane declares an explicit dependency.

## Resident Codex sessions

The default resident team should be small.

Recommended default:

* **Coordinator**: 1 session.
* **Developers**: scalable pool, initially capped conservatively.
* **Integrator**: 1 session.
* **Manhole**: 1 session for user intervention.
* **Optional Auditor/Verifier**: 0 or 1 session, enabled when quality or progress confidence is low.

For example:

* Small mode: 1 Coordinator, 2 Developers, 1 Integrator, 1 Manhole.
* Medium mode: 1 Coordinator, 4 Developers, 1 Integrator, 1 Manhole, optional shared Auditor/Verifier.
* Large mode: 1 Coordinator, 6–8 Developers, 1–2 Integrators, 1 Manhole, optional shared Auditor/Verifier.

The system must not spawn one standing Codex session for every conceptual role. Roles such as Architect, Reproducer, Conflict Resolver, Dependency Mapper, Lane Scout, Narrative Summarizer, Prompt Maintainer, and Flow Auditor should normally be short-lived jobs or temporary assignments to an existing idle worker.

The main thing that scales horizontally is the Developer pool. Developer scaling must be capped by integration health, not just CPU/RAM availability.

## Deterministic loops

These should normally be non-agentic deterministic loops, not Codex sessions:

* Watchdog.
* Resource monitor.
* Test loop.
* Status renderer.
* SQLite event logger.
* Queue monitor.
* Basic staleness detector.
* Basic integration triage.
* Safe Janitor cleanup.
* Metric monitor.

An LLM may be invoked to interpret anomalies, summarize long event streams, or propose reorganization, but the primary monitoring and bookkeeping must be deterministic.

## Commands

./harness init

Initializes a new harness project. This command is required before ./harness run.

Responsibilities:

Creates the .harness/ directory.
Creates the SQLite database and schema.
Initializes Git if running in a non-Git repository.
Checks whether gh is available and authorized.
Checks whether tmux is available.
Checks whether Codex CLI is available.
Checks whether the requested Codex model/profile is available.
Creates initial config files.
Creates role prompt files.
Creates DEVELOPMENT.md.
Creates .harness/STATUS_TEMPLATE.md.
Creates .harness/STATUS_TEMPLATE.html.
Creates or validates MCP configuration.
Builds and tests the SQLite MCP.
Builds and tests the scheduler/subagent MCP.
Initializes the codebase indexer if available.
Runs the Goal Planner to capture:
goal,
constraints,
success metric,
acceptance criteria,
initial backlog seed.
Writes PLAN.md.
Records the initialized run/project state in SQLite.

./harness init should be safe to rerun. If initialization has already happened, it should validate and repair missing setup where possible rather than duplicating state.

It should not start the resident agent team except for short-lived setup/planning agents needed during initialization.

./harness run

Starts or resumes the harness after ./harness init has completed.

Responsibilities:

Loads existing SQLite state.
Validates that initialization has completed.
Discovers existing tmux sessions and panes.
Discovers existing worktrees.
Determines which agents are alive, dead, idle, crashed, or stale.
Starts or resumes the resident sessions:
Coordinator,
Developer pool,
Integrator,
Manhole,
optional Auditor/Verifier.
Starts deterministic loops:
watchdog,
resource monitor,
test loop,
status renderer,
queue monitor,
metric monitor,
safe Janitor loop.
Opens the status tmux window.
Continues useful work from the current SQLite/worktree state.

If the harness itself previously crashed, ./harness run records that event, summarizes likely causes, informs the Coordinator, and resumes conservatively.

./harness run must not redo initialization or goal planning unless required setup is missing or the user explicitly requests reinitialization.

It must not require a global “restart phase” that blocks all other work.

### `./harness status`

Shows a TUI dashboard with:

* current date/time,
* last status generation time,
* overall progress metric,
* worklane queues,
* active agents,
* idle/stale/crashed agents,
* ready-for-integration queue,
* integration-failed queue,
* current test results,
* recent test trend,
* system CPU/RAM/disk usage,
* recent meaningful events,
* warnings and backpressure state.

The TUI should use Unicode borders, rectangles, and ANSI colors.

It is a reflection of the latest SQLite state and `STATUS.md`, not a freeform agent claim.

### `./harness poke "message"`

Injects a message into the running system and then displays the status dashboard.

The message may be routed to:

* Coordinator,
* specific agent,
* all agents,
* Integrator,
* Manhole,
* a specific worklane.

The harness must record the poke in SQLite.

### Additional useful commands

Implement if practical:

* `./harness stop`
* `./harness doctor`
* `./harness logs`
* `./harness lanes`
* `./harness agents`

## Worklanes

All development work is represented as worklanes.

A worklane should include at least:

* `id`
* `title`
* `description`
* `goal`
* `acceptance_criteria`
* `owner_agent_id`
* `role_type`
* `priority`
* `status`
* `base_branch`
* `branch_name`
* `worktree_path`
* `dependencies`
* `conflict_risk`
* `expected_metric_impact`
* `created_at`
* `assigned_at`
* `last_activity_at`
* `ready_for_integration_at`
* `integrated_at`
* `abandoned_at`
* `notes`

Possible worklane statuses:

* `queued`
* `assigned`
* `active`
* `blocked`
* `needs_verification`
* `ready_for_integration`
* `integrating`
* `integration_failed`
* `integrated`
* `stale`
* `abandoned`

These are per-worklane statuses only. They must not become a single global harness state machine.

A Developer who finishes a worklane should:

1. Commit changes.
2. Run lane-specific tests.
3. Produce a structured report.
4. Mark the lane `needs_verification` or `ready_for_integration`.
5. Immediately request another lane.

Developers must not wait for integration unless explicitly reassigned to integration repair.

## Resident roles

### Coordinator

The Coordinator combines:

* backlog curation,
* dispatch,
* flow control,
* lightweight auditing,
* queue health monitoring,
* intervention planning.

The Coordinator should not be a blocking manager. It should keep enough work available, assign idle workers, avoid obvious conflicts, and respond to backpressure signals.

Responsibilities:

* Maintain a pool of ready worklanes.
* Split large worklanes.
* Merge duplicate worklanes.
* Prioritize work.
* Assign idle Developers.
* Stop creating work in overloaded areas.
* React to integration backlog.
* React to failing tests.
* React to stale lanes.
* Invoke short-lived specialist jobs when needed.
* Apply user instructions from the Manhole unconditionally unless unsafe.

### Developer

A Developer works on one worklane at a time in a dedicated Git worktree.

Responsibilities:

* Understand the lane goal and acceptance criteria.
* Implement the change.
* Add or update tests where appropriate.
* Run lane-specific tests.
* Commit reasonably often.
* Avoid huge monolithic files.
* Avoid excessive fragmentation.
* Write intention-led docblocks for functions and types created.
* Report status using a structured schema.
* Mark completed work as ready for verification or integration.
* Immediately request another lane after completion.

Developers should not run the entire project test suite routinely. Full test runs are handled by the non-agentic test loop.

Developers run in `/goal` mode until they achieve their expected outcome.

### Integrator

The Integrator continuously integrates completed worklanes without blocking development.

It combines:

* integration triage,
* fast-path integration,
* basic verification,
* conflict classification.

The Integrator must not become a congestion point.

Responsibilities:

* Continuously scan lanes marked `ready_for_integration`.
* Prioritize low-conflict, high-confidence lanes.
* Use bounded integration attempts.
* Create temporary integration branches.
* Merge candidate lanes.
* Run targeted smoke checks.
* Promote successful work.
* Quickly requeue conflicted or failing work.
* Record integration failures with actionable detail.
* Avoid spending too long on one problematic lane.

Important rule:

> If a lane cannot be integrated within a bounded time or bounded number of attempts, stop trying, record the reason, create or update a conflict-resolution worklane, and move on.

The Integrator should not block Developers from continuing work.

### Manhole

The Manhole is a tmux window with a Codex session that has access to the state of the harness and can be used by the user to course-correct any part of the system.

The Manhole defaults to supervisor/read-only mode. It should inspect and explain state unless the user explicitly authorizes a concrete action in that manhole session.

It can:

* inspect agents,
* inspect worklanes,
* poke agents,
* adjust priorities,
* change the goal,
* change reporting behavior,
* request more or fewer sessions,
* invoke specialist roles,
* instruct the Coordinator.

It must not independently act as the Coordinator, create lanes, spawn agents, push branches, merge branches, or edit source files just because it sees work to do.

Instructions from Manhole to Coordinator must be applied unconditionally unless unsafe or impossible.

### Optional Auditor/Verifier

This may be a resident session or a short-lived job.

Responsibilities:

* Check whether completed lanes actually satisfy their acceptance criteria.
* Challenge unsupported claims of progress.
* Look for stale or fake progress.
* Inspect whether the metric is improving.
* Suggest reorganization when progress stalls.
* Escalate repeated integration failures or repeated test failures.

The Auditor should rely first on deterministic evidence:

* commits,
* tests,
* DB records,
* changed files,
* structured reports,
* integration results,
* metric trends.

Freeform agent claims are secondary.

## Non-resident specialist roles

These roles are normally invoked on demand.

### Goal Planner

Captures:

* goal,
* constraints,
* success metric,
* acceptance criteria,
* initial backlog seed.

It produces `PLAN.md`.

It should avoid delaying development unnecessarily. Once the initial metric and backlog seed exist, more planning can continue as ordinary research or planning worklanes.

### Lane Scout

Finds independently executable work:

* isolated modules,
* failing tests with clear scope,
* TODOs,
* type errors,
* documentation gaps,
* small refactors,
* test gaps,
* low-conflict improvements.

Feeds candidates to the Coordinator.

### Dependency Mapper

Analyzes likely conflict and dependency structure:

* file ownership,
* import graph,
* test ownership,
* hot files,
* overlapping worklanes,
* branch divergence.

Helps the Coordinator avoid assigning conflicting lanes.

### Conflict Resolver

Handles failed integrations.

A failed integration should become ordinary work, not a system-wide blocker.

Responsibilities:

* inspect integration failure,
* rebase or adapt the branch,
* resolve conflicts,
* update tests,
* return the lane to `ready_for_integration`.

### Reproducer

When tests fail, creates or identifies a minimal reproduction and stores it in SQLite.

### Architect

Invoked by triggers, not standing by default.

Triggers include:

* same test fails repeatedly,
* a test flips passing/failing more than 3 times in 24 hours,
* integration failures cluster around a subsystem,
* many lanes conflict on the same files,
* progress stalls despite high activity.

Architect produces refactor worklanes. It must not block unrelated work.

### Prompt/Protocol Maintainer

Invoked when agents repeatedly fail to follow role instructions, output schemas, or MCP usage rules.

Updates:

* role prompts,
* `DEVELOPMENT.md`,
* MCP instructions,
* structured report schemas,
* failure-specific reminders.

### Narrative Summarizer

Optional LLM role used by the deterministic Status Renderer when long event streams need concise human-readable explanation.

### Janitor

Mostly deterministic.

Cleans safe abandoned resources:

* temporary directories,
* obsolete logs,
* unnecessary tmux panes,
* old safe worktrees,
* excessive cache growth,
* old test logs according to retention policy.

Must not delete:

* unintegrated branches,
* unintegrated worktrees,
* recent useful logs,
* evidence needed for crash/debug recovery,
* anything not proven safe by SQLite records.

## Integration model

The integration system should use asynchronous queues:

* `ready_fast_path`
* `ready_needs_review`
* `ready_high_conflict`
* `ready_metric_sensitive`
* `integration_failed`

The Integrator should favor fast-path work to keep throughput high.

Suggested branch model:

* `main`
* `harness/integration`
* `worklane/<id>-<slug>`
* `integration-attempt/<timestamp>-<lane>`

Basic flow:

1. Developer completes worklane.
2. Developer commits.
3. Developer reports structured completion.
4. Verifier or Integrator checks local acceptance evidence.
5. Lane enters an integration queue.
6. Integrator attempts merge into a temporary integration branch.
7. Targeted smoke tests run.
8. Successful lane is promoted.
9. Failed lane is requeued or converted into conflict-resolution work.
10. Developer pool continues working throughout.

The Integrator must never allow one bad branch to stall the whole system.

## Backpressure without global blocking

The system should use backpressure, not global waiting.

Examples:

* If `ready_for_integration` queue grows beyond Developer count for more than 20 minutes, stop spawning feature work and assign idle workers to verification or conflict resolution.
* If `integration_failed` grows, spawn or reassign one Conflict Resolver.
* If too many lanes touch the same subsystem, stop creating new lanes in that subsystem.
* If a worklane is too far behind `main`, create a refresh/rebase task.
* If full-test failures grow, prioritize stabilization lanes.
* If the integration queue is near zero and tests are healthy, add Developer sessions if resources allow.
* If CPU/RAM is underused but integration is backed up, do not add feature Developers.
* If CPU/RAM is underused and integration is healthy, consider adding Developers.
* If CPU/RAM is overused for more than 30 seconds, identify and kill or pause problematic harness-owned processes.

The core scaling question is:

> Can the system integrate completed work as fast as Developers produce it?

If not, add integration support, not more feature Developers.

## SQLite memory

SQLite is the source of truth.

Use SQLite for:

* runs,
* agents,
* events,
* prompts,
* worklanes,
* worktrees,
* branches,
* commits,
* agent reports,
* test runs,
* test results,
* issues,
* bug history,
* integration attempts,
* resource samples,
* status snapshots,
* settings.

Only write markdown files when explicitly required.

Minimum tables should include:

* `runs`
* `agents`
* `events`
* `worklanes`
* `agent_messages`
* `agent_reports`
* `worktrees`
* `commits`
* `integration_attempts`
* `test_runs`
* `test_results`
* `issues`
* `resource_samples`
* `status_snapshots`
* `settings`

Every significant action should be recorded as an event:

* starting a session,
* stopping a session,
* sending a prompt,
* receiving a report,
* assigning a worklane,
* changing worklane status,
* creating a worktree,
* committing,
* attempting integration,
* failing integration,
* passing or failing tests,
* killing a process,
* cleaning resources.

## Structured agent reports

Agents must produce structured reports so the harness can parse them.

A Developer report should include:

```json
{
  "agent_id": "...",
  "worklane_id": "...",
  "status": "in_progress | blocked | needs_verification | ready_for_integration | failed",
  "summary": "...",
  "files_changed": [],
  "commits": [],
  "tests_run": [],
  "test_result": "pass | fail | not_run",
  "blockers": [],
  "next_action": "..."
}
```

An Integrator report should include:

```json
{
  "integrator_id": "...",
  "worklane_id": "...",
  "attempt_branch": "...",
  "status": "integrated | integration_failed | requeued | needs_conflict_resolution",
  "summary": "...",
  "merge_result": "...",
  "tests_run": [],
  "test_result": "pass | fail | not_run",
  "failure_reason": null,
  "next_action": "..."
}
```

Freeform logs are allowed, but structured reports are authoritative.

## Git and worktrees

The harness works with Git.

If run in a non-Git repository, initialize one.

If `gh` is unavailable or unauthorized, print a red warning but continue.

If `gh` is available and authorized:

* push progress to remote branches,
* optionally publish `STATUS.html` as a GitHub Page.

Developers work in separate Git worktrees to avoid collisions.

The harness stores worktree details in SQLite:

* path,
* branch,
* owner agent,
* worklane,
* base commit,
* status,
* last activity.

Unintegrated worktrees must not be deleted by Janitor.

## Testing loop

A non-agentic testing loop continuously runs the full test suite and logs results to SQLite.

It records:

* run id,
* commit,
* start time,
* end time,
* command,
* status,
* full logs,
* individual test successes,
* individual test failures,
* skipped tests.

The database must allow queries such as:

* all failures from the last run,
* all skipped tests from the last run,
* all successes from the last run,
* failures from a specific test file from three runs ago,
* history of a specific failing test.

Keep the last 5 full test logs.

For older logs, retain:

* one report per hour from today,
* one report per day from the previous week,
* one report per week from earlier months.

Any time a test fails, the harness should create or update an issue record and notify the Coordinator. Developers should not necessarily drop current work immediately, but the Coordinator should prioritize stabilization lanes appropriately.

When a test is fixed, store:

* failing test,
* suspected root cause,
* fix summary,
* first failing commit,
* fixing commit,
* related worklane.

Before fixing a failing test, look up whether it failed in the past and include previous causes/resolutions in the context.

If any test flips from passing to failing to passing repeatedly more than 3 times in 24 hours, invoke Architect to identify the systemic cause and create refactor worklanes.

## Status reporting

A deterministic Status Renderer updates `STATUS.md` and `STATUS.html` every 15 minutes and on startup.
Every time the status is updated, it must be committed and pushed to the main branch of the remote repo.

The first run creates:

* `.harness/STATUS_TEMPLATE.md`
* `.harness/STATUS_TEMPLATE.html`

Subsequent runs populate those templates from SQLite.

Status must include:

* overall progress metric,
* major milestones,
* current milestone stages,
* percentage progress,
* progress bar in HTML,
* current agents,
* current worklanes,
* integration queue size,
* integration failed queue size,
* expected metric improvement from pending integrations,
* test results,
* recent work,
* next steps,
* challenges,
* recent interesting events,
* CPU/RAM samples,
* progress chart every 15 minutes,
* resource chart every 15 minutes in HTML,
* resource summary every 60 minutes in Markdown.

If rendered text becomes longer than 2500 words, summarize the longest sections.

The status page should be readable in one sitting.

An optional Narrative Summarizer may enrich the report, but deterministic data is authoritative.

## Resource monitoring

A deterministic resource loop samples:

* CPU,
* RAM,
* disk,
* process tree usage,
* harness-owned process usage.

If CPU and RAM usage stay below 60% and integration/test health is good, the Coordinator may be prompted to consider increasing useful concurrency.

If CPU or RAM usage stays around 95%+ and the machine is slowed for more than 30 seconds, identify the problematic harness-owned process and kill or pause it.

If recent agents die after only a few seconds without producing useful results, assume possible resource exhaustion. Run Janitor. If that does not help, notify Coordinator and reduce concurrency.

Do not scale Developer count based only on unused CPU/RAM. Integration health and test health are stronger signals.

Do not spawn replacement Developers when no queued Developer or Designer worklane exists. An idle CPU is better than a no-op worker that opens a branch, finds no lane, and exits.

## Watchdog

The watchdog must be deterministic and as reliable as possible.

It monitors:

* harness process,
* Codex sessions,
* tmux panes,
* worktree health,
* crashed agents,
* repeated short-lived agents,
* idle agents,
* runaway resource usage.

On crash:

* record crash details,
* restart where possible,
* append crash context to the restarted agent’s prompt,
* ask it to adjust behavior to avoid repeated crash,
* notify Coordinator.

The watchdog must support Fedora and NixOS as well as practical local fallbacks. Prefer systemd where available; support a NixOS-compatible mechanism where possible.

The watchdog should not kill processes outside the harness process group.

## Idle and avoidance detection

The harness should monitor for:

* no output,
* no DB report,
* no file changes,
* no commits,
* repeated claims of being blocked,
* repeated waiting,
* `sleep` commands,
* long-running commands with no evidence of progress,
* agents that avoid tests,
* agents that claim success without evidence.

It is acceptable for an agent to wait briefly for collaboration, but not repeatedly for 5, 10, or 30 minutes without progress.

Any use of `sleep` is suspicious and should be investigated.

If progress metric is not improving over a configurable window, the status dashboard should show a red warning and the Coordinator should reorganize work.

## MCP tools

SQLite interactions are done through an MCP tool.

Every agent has access to the SQLite MCP and brief instructions for using it.

Build and test this MCP.

Spawning subagents must also go through an MCP tool routed back to the central harness scheduler. Agents must not spawn invisible unmanaged agent trees.

All inter-agent communication should happen through the harness-visible tools, prompts, SQLite records, or controlled tmux/session mechanisms.

The harness should know the entire agent tree at any given time.

## Codebase indexer

Bring over an open-source codebase indexer and expose it through an MCP so agents do not have to grep constantly.

It should:

* run independently of worklanes,
* not block new worktrees,
* distinguish different worktrees,
* fall back to grep if indexing is unavailable,
* keep checking for readiness.

## Codex configuration

All Codex sessions should default to:

* `--yolo`
* `gpt-5.5 xhigh fast`

Do not silently downgrade.

If the requested model is unavailable, fail clearly unless the user explicitly allows fallback.

Permission/model behavior should be configurable, but the default must match the above.

## Safety and cleanup boundaries

Destructive operations need guardrails.

Never delete:

* unintegrated branches,
* unintegrated worktrees,
* current useful logs,
* crash evidence,
* data not proven safe by SQLite records.

Never kill:

* processes outside the harness process group,
* user processes not created by the harness.

Janitor should support dry-run mode.

Cleanup decisions should be recorded in SQLite.

## Development instructions

Create `DEVELOPMENT.md` for Developer agents.

It should instruct Developers to:

* avoid creating a single huge file with the entire project,
* avoid fragmenting every tiny thing into its own file/function,
* write relevant intention-led docblocks for most functions/types created,
* document why a function/type exists,
* document what/how only when not obvious from code,
* add inline comments for non-obvious sections,
* commit reasonably often,
* run relevant lane-specific tests,
* report structured status,
* avoid unsupported claims of completion.

## Acceptance tests

The harness should include tests for at least:

* starting in a non-Git directory initializes Git,
* first `./harness run` creates SQLite DB,
* first `./harness run` creates/uses tmux session,
* status command renders without agents,
* crashed Developer is detected and restarted,
* sleeping Developer is detected and escalated,
* Developer worktree is created correctly,
* Developer completion creates structured report,
* ready worklane enters integration queue,
* Integrator handles fast-path lane,
* Integrator requeues conflicted lane without blocking others,
* failed full test creates issue record,
* fixed test updates issue record,
* repeated failing test invokes Architect,
* Janitor does not delete unintegrated worktree,
* restart resumes known worklanes,
* stale worklane is detected,
* integration backlog triggers backpressure,
* low CPU does not spawn more Developers when integration is backed up,
* `./harness poke` records event and routes message.

## Core objective

The harness should feel less like a pipeline and more like a local operating system for coding agents.

It should keep useful work flowing concurrently while deterministic loops continuously measure, supervise, integrate, test, clean, report, and recover.

The system succeeds when:

* Developers rarely wait,
* Integrator does not become a bottleneck,
* broken work is requeued quickly,
* progress is measured objectively,
* status is always inspectable,
* crashes are recoverable,
* integration debt is controlled,
* and the user can intervene at any time through the Manhole.
