# Plan: Align Harness With Card-Based Deterministic Control Plane

## Goal

Update the harness implementation to match the revised `harness-refined.md` criteria: Python owns orchestration, every non-support Codex task is backed by a card, and cards move through explicit stages: `planned`, `development`, `review`, `integration`, and `done`.

## Spec deltas to implement

The refined spec now requires these behavior changes:

1. **Card-backed work is mandatory**
   - Implementation work remains a worklane.
   - Out-of-band work such as Architect reconsideration, Coordinator reorganization, Integrator triage, Conflict Resolver work, and Prompt Maintainer work must also be represented as cards.
   - No non-support Codex session should do concrete work without an assigned card.

2. **Python owns stage movement**
   - Codex can report evidence but cannot authoritatively move work globally.
   - Python moves cards through `planned -> development -> review -> integration -> done`.
   - Non-code cards can skip `integration` only when `integration_required = false`.

3. **Agents pick up staged cards**
   - Developers receive `planned` implementation cards and move them to `development`.
   - Verifier/Auditor roles inspect `review` cards.
   - The deterministic integration loop handles `integration` cards.
   - Architect/Coordinator/Integrator advisory work must be assigned as explicit cards.

4. **Continuous tests stay outside the board execution path**
   - The test loop keeps running independently.
   - Test failures create or update stabilization cards instead of freezing the board.
   - Duplicate failure cards must be deduplicated by unresolved test/command/commit context.

5. **Status must show the board**
   - Status should expose cards grouped by stage.
   - Active agents should correlate to cards, not just legacy worklanes.
   - Uncarded active non-support work should be visible as a red control-plane error.

6. **Integration ends at the remote, not the local worktree**
   - Successful integration must push the promoted commit to the configured remote target branch.
   - Record the remote ref, commit SHA, and push result before moving the card to `done`.
   - A failed push leaves the card in `integration` or moves it to `integration_failed`; it is not complete.

## Implementation plan

### 1. Schema and migration

- Add card fields to the durable work table or introduce a `cards` table with compatibility views for existing `worklanes`.
- Required fields:
  - `card_type`
  - `stage`
  - `review_required`
  - `integration_required`
  - `planned_at`
  - `review_ready_at`
  - `reviewed_at`
  - `done_at`
- Add `card_stage_transitions` for audit history.
- Migrate legacy worklane statuses:
  - `queued` -> `planned`
  - `assigned` / `active` -> `development`
  - `needs_verification` -> `review`
  - `ready_for_integration` / `integrating` / `integration_failed` -> `integration`
  - `integrated` -> `done`
  - `stale` / `abandoned` remain exceptional statuses attached to their last meaningful stage.

### 2. Deterministic card transition API

- Add Python functions for:
  - `create_card`
  - `assign_card`
  - `move_card_stage`
  - `record_card_report`
  - `requeue_card`
  - `complete_card`
- Enforce legal transitions in Python.
- Record every transition in SQLite events and `card_stage_transitions`.

### 3. Scheduler assignment rules

- Developers may only start when a claimable `planned` implementation card exists.
- Assign exactly one card per Developer.
- Stop or reuse any Developer without a card.
- Treat Coordinator, Architect, Integrator, Auditor, Verifier, Conflict Resolver, and Prompt Maintainer concrete work as card-backed specialist assignments.
- Keep Manhole read-only unless explicitly authorized by the user.

### 4. Spawn request gate

- Keep `spawn_agent` request-only.
- Coalesce duplicate singleton role requests by role, title, unresolved card, and active agent.
- Reject or convert uncarded spawn requests into planned specialist cards before spawning.
- Prevent duplicate Integrators; routine integration belongs to the deterministic loop.

### 5. Agent report enforcement

- Require `card_id`, `stage`, and structured evidence in `agent_report`.
- Move `development -> review` only after an accepted report.
- If a Codex pane ends without an accepted report, mark the worker terminal and requeue/preserve its card.
- Do not accept freeform “goal achieved” as completion.

### 6. Review and integration loops

- Add deterministic review handling for cards in `review`.
- Move code-producing cards to `integration` only after review passes.
- Move non-code advisory cards directly from `review` to `done` after acceptance.
- Keep deterministic `./harness integrate` processing `integration` cards continuously.
- After merge and smoke-test success, push the promoted commit to the configured remote target branch with `git push`.
- Do not rely on `gh` for integration completion; missing GitHub CLI support must not silently turn a local merge into done work.
- Record the remote ref, commit SHA, and push result before moving a card to `done`.
- Keep cards in `integration` or move them to `integration_failed` when the push fails or no authenticated remote exists.
- Convert merge, test, or push integration failures into conflict-resolution cards when needed.

### 7. Test-loop integration

- Keep the full test loop independent of card execution.
- On failing tests, create/update stabilization cards in `planned`.
- Deduplicate repeated failures.
- Do not create a new card every time the same unresolved test fails.

### 8. Status and diagnostics

- Update `./harness status` to show:
  - card counts by stage,
  - active agent -> card correlation,
  - unassigned cards by stage,
  - uncarded active non-support agents as errors,
  - integration and review queues separately.
- Update `lanes`/`agents` outputs to include card stage and card id.

### 9. Tests

Add focused tests for:

- planned card moves to development when assigned,
- development card moves to review only after accepted `agent_report`,
- reviewed code card moves to integration,
- successful integration pushes to the configured remote target branch before moving to done,
- failed integration push does not move the card to done,
- reviewed non-code card moves to done,
- active non-support Codex work cannot exist without a card,
- Developer without a card is stopped, reused, or assigned by Python,
- Architect reconsideration is represented as a card,
- Integrator triage is represented as a card or handled by deterministic integration,
- duplicate singleton requests are coalesced,
- repeated test failures update one unresolved stabilization card,
- status renders the card board by stage.

## Rollout order

1. Ship schema/card transition compatibility while preserving existing worklane behavior.
2. Enforce Developer card assignment.
3. Add status board display and uncarded-agent warnings.
4. Convert specialist/Integrator/Coordinator concrete work to card-backed assignments.
5. Tighten MCP `agent_report` and `spawn_agent` contracts.
6. Deduplicate test-loop-generated stabilization cards.
7. Remove or deprecate legacy status-only worklane paths once tests cover the board model.
