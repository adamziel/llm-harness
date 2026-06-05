Yes — here’s a revised version that keeps your original intent but bakes in what we concluded: **no global state machine, no 15-agent standing org chart, roles as capabilities, small resident team, concurrent worklanes, async integration, deterministic loops, and integration-aware backpressure**. It also tightens some ambiguity in the original proposal around Manager/Integrator congestion.

# LLM Harness System — Revised Spec

Implement a Python script that implements an LLM agent harness for driving concurrent software development with Codex CLI sessions.

The purpose of the harness is to compensate for agentic unreliability using deterministic supervision. Anytime a task can be completed by deterministic tools — function calls, shell calls, requests, SQLite queries, Git commands, process checks, test runners, etc. — prefer deterministic tools. If a task can only be completed by prompting an agent, implement it that way, but ensure the harness detects refusal, failure, avoidance, idle drift, repeated waiting, excessive sleeping, and lack of measurable progress. The harness must have feedback loops that keep work moving in a timely manner.

This system must **not** be designed as a single global state machine where the whole harness moves through blocking phases. The harness should instead be a **concurrent control plane**: independent deterministic loops and a small number of resident Codex sessions coordinate through SQLite, Git worktrees, tmux, structured reports, and measurable progress signals.

The core principle is:

> Roles are capabilities, not necessarily always-running Codex sessions.

Most roles should be deterministic loops, short-lived agent jobs, or temporary modes of an existing resident session. We should not need 15 standing agents just to keep the system afloat.

An equally important principle is:

> Python is the control plane. Codex is an unreliable bounded worker.

Any decision that affects global harness state must be made by deterministic Python logic backed by SQLite, Git, tmux/process inspection, and tests. Codex sessions may propose, explain, investigate, edit code, and report evidence, but they must not be trusted as the authority for scheduling, spawning, assignment, integration, stop/reset behavior, or global progress decisions.

Think of the system as:

* Python scheduler = local operating-system kernel.
* SQLite = source of truth.
* tmux/process table = worker process registry.
* Codex = untrusted userspace worker.

Codex can request state changes through MCP tools, but Python validates, coalesces, rejects, records, and enforces those requests.

## Deterministic authority boundaries

The following must be owned by deterministic Python logic, not by Codex judgment:

* lane assignment,
* agent spawning,
* agent capacity limits,
* keeping required support windows alive,
* killing or replacing dead/stale panes,
* preventing duplicate singleton roles,
* test loop execution,
* integration queue processing,
* fast-path merges,
* integration backpressure,
* status rendering,
* stop/reset/janitor behavior,
* deciding whether a lane is claimable,
* recording terminal states,
* rejecting or coalescing noisy spawn requests,
* requeuing abandoned lanes,
* preserving or deleting worktrees/branches.

Codex should only receive bounded worker assignments:

> You are `developer-N`. Your only job is `lane#X`. Work in this worktree. Commit changes. Run these focused checks. Report through `agent_report`.

Codex should not decide whether to spawn more agents, whether another Integrator is needed, whether work should be globally reassigned, whether `stop` means stopped, whether tests are globally good enough, what the next lane should be, or whether to merge to trunk.

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

The system is organized around **cards/worklanes**, not global phases.

Every non-support Codex task must be represented by a durable card before work starts. This includes ordinary implementation work and out-of-band-looking work such as Architect reconsideration, Coordinator reorganization, Integrator triage, conflict analysis, prompt/protocol repair, or investigation of repeated failures. If a Codex session is doing useful work and it is not the Manhole in read-only mode, a deterministic scheduler should be able to point to the card that authorizes that work.

The harness uses a card board with explicit stages:

* `planned`
* `development`
* `review`
* `integration`
* `done`

Optional exceptional states such as `blocked`, `cancelled`, `stale`, or `abandoned` may exist, but the happy path is planned → development → review → integration → done. Cards that do not produce source changes, such as Architect reports or Coordinator reorganization proposals, still move through planned → development → review → done; they can skip integration only because their `integration_required` flag is false.

A worklane is an independently schedulable unit of work with:

* a stage,
* a card type,
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

Development, verification, testing, integration, auditing, reporting, cleanup, and planning should happen concurrently whenever possible. No subsystem should wait for another subsystem except where a specific card declares an explicit dependency.

The continuous test loop is intentionally outside the card board execution path. It runs regardless of card stage, records test history, and creates or updates defect/stabilization cards when it finds failures. It must not block card movement globally; it supplies evidence and new cards for the scheduler to prioritize.

## Resident Codex sessions

The default resident team should be small.

Recommended default:

* **Coordinator**: 1 session.
* **Developers**: scalable pool, initially capped conservatively.
* **Integrator**: 0 or 1 advisory session; routine integration is deterministic.
* **Manhole**: 1 session for user intervention.
* **Optional Auditor/Verifier**: 0 or 1 session, enabled when quality or progress confidence is low.

For example:

* Small mode: 1 Coordinator, 2 Developers, 0–1 Integrator, 1 Manhole.
* Medium mode: 1 Coordinator, 4 Developers, 0–1 Integrator, 1 Manhole, optional shared Auditor/Verifier.
* Large mode: 1 Coordinator, 6–8 Developers, 1 Integrator, 1 Manhole, optional shared Auditor/Verifier.

The system must not spawn one standing Codex session for every conceptual role. Roles such as Architect, Reproducer, Conflict Resolver, Dependency Mapper, Lane Scout, Narrative Summarizer, Prompt Maintainer, and Flow Auditor should normally be short-lived jobs or temporary assignments to an existing idle worker.

Singleton roles must be enforced by Python. Coordinator, Manhole, Architect, Auditor, Verifier, and Integrator requests should be coalesced into an existing active session unless the user explicitly configures multiple sessions. Repeated requests for the same role/title should become one queued or delivered assignment, not many tmux panes or repeated prompt spam.

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
* Routine integration/merge loop.
* Lane assignment loop.
* Spawn request gate.
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
Assigns `planned` cards to eligible workers deterministically.
Moves cards between `planned`, `development`, `review`, `integration`, and `done`.
Rejects or coalesces duplicate spawn requests.
Ensures singleton roles do not multiply.
Starts deterministic loops:
watchdog,
resource monitor,
test loop,
integration loop,
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
* `./harness reset-counters`
* `./harness doctor`
* `./harness logs`
* `./harness lanes`
* `./harness agents`

`./harness stop` must be deterministic and fast. It should mark the harness stopped in SQLite, stop scheduler/watchdog resurrection, terminate harness-owned tmux windows/processes, cancel queued prompts/spawn requests, and leave Janitor-style cleanup for the next run unless explicitly requested.

`./harness reset-counters` is for upgrades after harness control-plane bugs. It should clear stale telemetry that would mislead scheduling decisions, such as historical crashed-agent counts, old test-loop failure records, stale queued spawn requests, progress-stall banners, and retryable integration-failed lanes. It must not delete source work, unintegrated branches, unintegrated worktrees, or evidence needed to recover active work.

## Cards and worklanes

All Codex-executed work is represented as cards. Implementation cards are worklanes; advisory/investigation/control-plane cards are also first-class cards.

A card/worklane should include at least:

* `id`
* `title`
* `description`
* `goal`
* `acceptance_criteria`
* `owner_agent_id`
* `role_type`
* `card_type`
* `priority`
* `stage`
* `status`
* `base_branch`
* `branch_name`
* `worktree_path`
* `dependencies`
* `conflict_risk`
* `review_required`
* `integration_required`
* `expected_metric_impact`
* `created_at`
* `planned_at`
* `assigned_at`
* `review_ready_at`
* `reviewed_at`
* `last_activity_at`
* `ready_for_integration_at`
* `integrated_at`
* `done_at`
* `abandoned_at`
* `notes`

Canonical card stages:

* `planned`: card exists, has enough context to be considered, and is not yet owned by a worker.
* `development`: card is assigned to a worker for implementation, investigation, conflict resolution, or advisory output.
* `review`: worker output exists and must be checked against acceptance criteria.
* `integration`: reviewed code-producing work is ready for deterministic merge/integration.
* `done`: work has been accepted, integrated if required, and no further action is expected.

Exceptional statuses may refine the stage:

* `blocked`
* `stale`
* `abandoned`
* `cancelled`
* `integration_failed`
* `review_failed`

Stages and statuses are per-card only. They must not become a single global harness state machine.

Valid stage movement is controlled by Python:

* planned → development when Python assigns a card to a worker.
* development → review when a worker submits an accepted structured report.
* review → integration when review passes and `integration_required = true`.
* review → done when review passes and `integration_required = false`.
* integration → done when deterministic integration succeeds.
* review/integration → planned or development when repair is needed.
* any active stage → blocked/stale/abandoned/cancelled only through deterministic scheduler rules.

A Developer who finishes a worklane should:

1. Commit changes.
2. Run lane-specific tests.
3. Produce a structured report.
4. Submit an `agent_report`.
5. Let Python move the card to `review`, `integration`, or back to `planned/development`.

Developers must not wait for integration unless explicitly reassigned to integration repair.

Lane assignment itself is not a Developer or Coordinator decision. The Python scheduler owns claimability and assignment:

* A Developer must only be spawned or reused when a claimable lane exists.
* A Developer receives exactly one lane and one worktree assignment at a time.
* If no claimable lane exists, do not spawn a Developer.
* If a Developer has no assigned lane, it should be stopped, reused, or given a lane by the scheduler; it should not sit idle doing global planning.
* If a Developer exits or reaches a goal-complete screen without an `agent_report`, mark the agent terminal, requeue its assigned lane, and record the failure.
* If a Developer reports correctly, Python updates the lane state and may close/reuse the worker.

Stage-specific pickup rules:

* Developers pick up `planned` implementation cards and move them to `development`.
* Verifiers/Auditors pick up `review` cards and produce deterministic or structured acceptance evidence.
* The deterministic integration loop picks up `integration` cards.
* Conflict Resolver cards are created from failed `integration` cards and start in `planned`.
* Architect, Lane Scout, Prompt Maintainer, and Coordinator advisory work must be explicit cards, usually with `integration_required = false`.
* Coordinator may propose new cards or stage changes, but Python applies them.

Out-of-band work is handled by creating cards, not by allowing untracked agent activity. Examples:

* `Architect`: “Reconsider system design around repeated integration failures” starts as a planned advisory card, moves to development while the Architect investigates, moves to review when the report/refactor proposal is submitted, then either creates follow-up implementation cards or moves to done.
* `Coordinator`: “Reprioritize stabilization work after backpressure” starts as a planned control-plane card and ends with concrete card/stage/priority changes accepted by Python.
* `Integrator`: “Triage current integration_failed lanes” starts as a planned integration-support card and ends with integration cards requeued, conflict-resolution cards created, or evidence recorded.

No agent should perform these tasks just because a prompt asked them to; the scheduler must first create or assign the corresponding card.

## Resident roles

### Coordinator

The Coordinator combines:

* backlog curation,
* dispatch,
* flow control,
* lightweight auditing,
* queue health monitoring,
* intervention planning.

The Coordinator should not be a blocking manager and should not be the authority for scheduling. It may curate backlog, propose priorities, and recommend interventions, but deterministic Python assigns workers, enforces capacity, and applies backpressure.

Responsibilities:

* Maintain a pool of `planned` cards.
* Split large cards.
* Merge duplicate cards.
* Prioritize work.
* Propose assignments for idle Developers; Python performs the actual assignment.
* Stop creating work in overloaded areas.
* React to integration backlog.
* React to failing tests.
* React to stale lanes.
* Invoke short-lived specialist jobs when needed.
* Apply user instructions from the Manhole unconditionally unless unsafe.

Coordinator output is advisory unless it is submitted through a harness MCP tool and accepted by deterministic scheduler rules. If the Coordinator repeatedly creates noisy or duplicate requests, the scheduler should coalesce or reject them and record why.

Concrete Coordinator work, such as “reorganize integration backpressure” or “triage current integration-failed lanes”, must itself be represented as an advisory/control-plane card. A running Coordinator may have no card while idle or supervising, but any concrete task it performs must be card-backed.

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
* Report completion and become available for scheduler-directed reuse.

Developers should not run the entire project test suite routinely. Full test runs are handled by the non-agentic test loop.

Developers run in `/goal` mode until they achieve their assigned card outcome. They must not switch to unrelated planning, routing, integration, or general maintenance work unless the scheduler assigns that as a concrete card.

### Integrator

Routine integration is primarily a deterministic Python loop. The LLM Integrator is an optional singleton advisory or exception-handling role for cases where deterministic integration cannot classify or resolve the situation alone.

It combines:

* integration triage,
* fast-path integration,
* basic verification,
* conflict classification.

The Integrator must not become a congestion point or multiply into several independent sessions. Unless explicitly configured otherwise, there should be at most one active LLM Integrator; additional Integrator requests should be coalesced into the existing session or handled by the deterministic integration loop.

Responsibilities:

* Inspect lanes the deterministic integration loop could not safely process.
* Classify conflicts or ambiguous failures.
* Propose targeted fixes or conflict-resolution worklanes.
* Use bounded integration attempts when explicitly delegated.
* Run targeted smoke checks only when needed.
* Record integration failures with actionable detail.
* Avoid spending too long on one problematic lane.

Concrete LLM Integrator work must be card-backed. If the Integrator is asked to triage failed integrations, Python should create or assign an `Integrator`/`Conflict Resolver` card in `planned` or `review`; the Integrator must not perform open-ended uncarded queue scanning.

Important rule:

> If a lane cannot be integrated within a bounded time or bounded number of attempts, Python stops trying, records the reason, creates or updates a conflict-resolution worklane, and moves on.

The Integrator should not block Developers from continuing work.

### Manhole

The Manhole is a tmux window with a Codex session that has access to the state of the harness and can be used by the user to course-correct any part of the system.

By default, Manhole is read-only/supervisory. It can inspect and explain state freely, but it must not mutate state, assign work, push, merge, spawn agents, or edit source files unless the user explicitly authorizes that action in the Manhole session.

When authorized, it can:

* inspect agents,
* inspect worklanes,
* poke agents,
* adjust priorities,
* change the goal,
* change reporting behavior,
* request more or fewer sessions,
* invoke specialist roles,
* instruct the Coordinator.

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

The deterministic integration loop should favor fast-path work to keep throughput high.

Suggested branch model:

* configured remote target branch, such as `origin/main` or `origin/trunk`
* local tracking branch for that target
* `harness/integration`
* `worklane/<id>-<slug>`
* `integration-attempt/<timestamp>-<lane>`

Integration is not complete while the result exists only in a local worktree. A successful integration must push the promoted commit to the configured remote target branch and record the remote ref and commit SHA. If the push fails, the card remains in `integration` or moves to `integration_failed`; it must not be marked `done`.

Basic flow:

1. Developer completes worklane.
2. Developer commits.
3. Developer reports structured completion.
4. Verifier or deterministic integration logic checks local acceptance evidence.
5. Lane enters an integration queue.
6. Deterministic integration loop attempts merge into a temporary integration branch.
7. Targeted smoke tests run.
8. Successful lane is promoted onto the configured local target branch.
9. The promoted commit is pushed to the configured remote target branch.
10. Python records the pushed remote ref, commit SHA, and push result.
11. Only after the push succeeds does the card move to `done`.
12. Failed merge, test, or push requeues the lane or converts it into conflict-resolution work.
13. Developer pool continues working throughout.

The integration loop must never allow one bad branch to stall the whole system. The LLM Integrator may be asked to inspect ambiguous failures, but Python owns the queue, bounded attempts, status transitions, and promotion/requeue decisions.

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
* cards,
* worklanes,
* card stage transitions,
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
* `cards` or a worklane table with card fields
* `worklanes`
* `card_stage_transitions`
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
* moving a card between stages,
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
  "card_id": "...",
  "stage": "development | review",
  "status": "in_progress | blocked | ready_for_review | failed",
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
  "card_id": "...",
  "stage": "integration | done",
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

Integration completion uses `git push`, not GitHub-specific API calls. If the configured remote is missing or unauthenticated, integration cannot complete and the card must stay out of `done`.

If `gh` is unavailable or unauthorized, print a red warning but continue.

If `gh` is available and authorized:

* push progress to remote branches,
* push successful integration results to the configured remote target branch before marking cards done,
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

The test loop is not a card worker and should not own a board stage. It runs continuously alongside the board, records evidence, and creates or updates cards when failures appear. A failing full-suite run should not freeze all cards in `development`; it should create or reprioritize stabilization cards in `planned` and provide evidence for review/integration decisions.

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

Repeated test failures must not create an infinite stream of duplicate cards. Python should deduplicate by failing test, command, commit range, and unresolved card status.

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
* current card board by stage,
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

Every time the status is updated, it must be committed and pushed to the main branch of the remote repo.

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
* requeue or preserve its assigned worklane as appropriate,
* reduce or stop unstable capacity if crashes repeat,
* notify Coordinator as advisory context.

If `./harness stop` has marked the harness stopped, watchdog must not resurrect scheduler loops, support windows, or agent panes. Only an explicit `./harness run` should clear the stopped flag and resume work.

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

If progress metric is not improving over a configurable window, the status dashboard should show a red warning. Python should apply deterministic backpressure and may ask the Coordinator for advisory reorganization proposals.

If an agent appears to be done but has not produced an accepted structured report, deterministic logic should not trust the freeform claim. Mark the worker terminal, preserve evidence, and repair the lane state.

## MCP tools

SQLite interactions are done through an MCP tool.

Every agent has access to the SQLite MCP and brief instructions for using it.

Build and test this MCP.

Spawning subagents must also go through an MCP tool routed back to the central harness scheduler. Agents must not spawn invisible unmanaged agent trees.

The `spawn_agent` MCP tool is request-only. It queues an intent; it does not directly create a tmux window. Python decides whether to accept, reject, coalesce, or delay the request based on role singleton rules, capacity, duplicate request detection, lane availability, resource pressure, and integration backpressure.

The `agent_report` MCP tool is the only authoritative completion path for Codex work. If a worker exits, reaches a goal-complete screen, or stops producing output without an accepted `agent_report`, Python should treat that as an incomplete/failed worker outcome and repair state deterministically.

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
* planned card moves to development when assigned,
* development card moves to review only after accepted structured report,
* reviewed code-producing card moves to integration,
* non-code Architect/Coordinator card moves from review to done without integration,
* active non-support Codex work cannot exist without a card,
* ready worklane enters integration queue,
* deterministic integration loop handles fast-path lane,
* deterministic integration loop pushes successful integration to the configured remote target branch before marking the card done,
* deterministic integration loop treats a failed push as an integration failure instead of local completion,
* deterministic integration loop requeues conflicted lane without blocking others,
* failed full test creates issue record,
* fixed test updates issue record,
* repeated failing test invokes Architect,
* Janitor does not delete unintegrated worktree,
* restart resumes known worklanes,
* stale worklane is detected,
* integration backlog triggers backpressure,
* low CPU does not spawn more Developers when integration is backed up,
* `./harness poke` records event and routes message,
* `./harness stop` prevents watchdog resurrection until explicit `./harness run`.
* `./harness reset-counters` clears stale telemetry without deleting source work.
* duplicate singleton spawn requests are coalesced instead of opening many panes.
* multiple Integrator requests reuse one Integrator or the deterministic integration loop.
* Developer is not spawned without a claimable worklane.
* Developer without a lane is stopped, reused, or assigned by Python.
* Codex goal-complete without `agent_report` requeues the lane.
* MCP `spawn_agent` queues requests but does not directly create unmanaged agents.

## Core objective

The harness should feel less like a pipeline and more like a local operating system for coding agents.

It should keep useful work flowing concurrently while deterministic loops continuously measure, supervise, integrate, test, clean, report, and recover.

The system succeeds when:

* Developers rarely wait,
* every non-support Codex task is visible as a card,
* cards move predictably through planned, development, review, integration, and done,
* Integrator does not become a bottleneck,
* broken work is requeued quickly,
* progress is measured objectively,
* status is always inspectable,
* crashes are recoverable,
* integration debt is controlled,
* and the user can intervene at any time through the Manhole.
