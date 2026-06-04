# LLM Harness System — Spec

Implement a python script that implements an LLM agent harness. Anytime a task can be completed by either deterministic
tools (function calls, shell calls, requests, etc.) or non-deterministic agentic tools, prefer deterministic tools. If
a task can only be completed by prompting an agent, then implement it that way, but make sure we're aware of that agent's
refusal, failure, avoidance (e.g. sleep 3600) and we have a feedback loop in place to force it to complete the task in
a timely manner.

- The architecture is this:
    - The top level harness scheduler is an event-driven loop that creates, manages, monitors, kills codex CLI processes.
	- We don't trust the codex sessions to do what we've asked them to. We treat them as lazy, drifting, forgetful, and assume they
	  will look for easier solutions than what we've asked for, they'll forget, they'll tend to avoid measuring progress, they'll
	  look for reasons to `sleep`, not to work, wait for something, assume we're blocked, and other reasons not to work. This harness
	  script uses deterministic code structures such as loops, watchdogs, status monitoring, process spawning and killing etc. to
	  fight against agentic unreliability. We course-correct
	- The codex processes are isolated enough such that a codex process crashing does not crash the entire harness scheduler.
	- Offer commands:
		- `./harness run`
			– On the first start, it runs the **Goal Planner** to capture the user's goal. We get the user to state the goal and refine it with Goal Planner agent. We plan and collect the information from the user. Planning might require a small team of researchers itself. The planner should generate a bunch of ideas, hypotheses, and questions, and that team of researchers should go and find answers, verify, and bring in more ideas from what's out there in the world. Based on that, the planner might decide to do another round. We do up to 3 rounds and we display the progress the entire time. We collect insights in a separate sqlite table to avoid a huge markdown file that hogs the context. Then we summarize it in chunks, and then we do a summary of summaries — a recursive process to feed the model a manageable amount of information.
			— Once we have a goal, we clean up what we don't need anymore with Janitor, store a sumamry for the Auditor to know what to put special emphasis on, and proceed to the building stage.
			– When we start building, we measure the available system CPU, RAM, disk space for other parts of the system to use, and then we spawn the builder team consisting of: A Manager,
			– On subsequent runs, it understands and recovers the progress we've made so far. It could resume the initial research if
			that crashed, or it could resume the building phase. In either  case, it re-establishes the team of agents to continue pursuing it where we stopped.
			There may be half-finished worktrees. We must have enough bookkeeping to know where have we stopped, what was the likely source of the last crash, if any, and re-establish the team of agents exactly as it was before. If we are restarting because of an earlier crash of this entire harness, start with analyzing what happened via codex, adjusting the work plan, possibly involving an architect, and only then restarting the team. Tell the Manager what happened.
			— It also spawns a `manhole` tmux window with a codex session that has access to all the other agents and can be used by the user to course-correct any part of the system, e.g. the goal, number of sessions, how we achieve or report progress, and anything else. It can manage codex sessions, poke them, start new ones for its benefit, and whatever else it needs.
			– It also spawns a `status` tmux window that runs the `watch` command refreshing every 5 seconds with the latest `./harness status` and switches to it.
			– The stdout of harness run is a high-level log of what is happening. High-level enough to only get a handful of events every minute. Many hundreds of lines per minute are not acceptable.
		- `./harness status` — shows a TUI dashboard with a summary of progress, current agents, test runs, and % of metric ready. Uses Unicode borders, rectangles, and ANSI color codes to provide useful visual information. Totally fine to use a library for this. It's a reflection of the last STATUS.md. The first information we see is the date and time when it was last generated.
		- `./harness poke "message"` — injects a message/prompt into the running system (e.g. to a specific agent or broadcast) and then shows the same TUI dashboard. Uses Unicode borders, rectangles, and ANSI color codes to provide useful visual information. Totally fine to use a library for this.
	- It doesn't have to be rebuilt to change how it works, so bash or python or something like that.
	- Has a few team presets with a minimal number of agents doing a specific task — i.e. it just knows the roles and how many agents per role we're spinning. We use different teams for different stages.
	— All codex sessions must run in --yolo mode with gpt 5.5 xhigh fast and never downgrade.
	- Runs a deterministic loop to monitor the running worker agents. If we have less than the minimum amount, it starts more. If we have idle ones, it starts/prompts an existing auditor and tells it to understand why the agent is idle and get it to work. It is okay for an agent to wait a minute or two for another agent if they collaborate, but it's not okay to wait for 5 minutes, 10 minutes, 30 minutes, especially multiple times in a row. Anytime any agent runs a `sleep` command, that's suspicious and must be investigated. If the rate of progress is not increasing every 30 minutes, it tells `harness run` so it can display a big red banner to the user, that should also be highlighted in the progress report, and then it sends a prompt to the manager to re-organize work such that it starts making progress again. We also monitor the lifetime of each agent.
	- We work with git. If harness is ran in a non-git repository, it initializes one. If we don't have access to `gh` command or it's unauthorized with github, we say that in red letters but we still start. If gh is available and authorized, we push our progress to remote branches and set things up to publish STATUS.html as a github page.
	- Every 60 minutes (and on system start), we run a Janitor to clean up.
	- Runs a deterministic loop to probe the system resource usage. If CPU and RAM usage stays below 60%, send a prompt to the manager to indicate we might be using more resources through more work-intense codex sessions, or a higher number of codex sessions. If CPU or RAM usage stays around 95%+ across all cores and we're slowing down the entire machine for more than 30 seconds, then kill the problematic process and let it
	- Set up a watchdog that restarts the system if it dies. Also monitors any crashed agent processes managed by us. Restart them where they left off, and append to the initial prompt for that agent a summary of the crash details with information where to find more details if needed, and ask it to adjust what it's doing to avoid another crash. Also monitors resource usage: if we're slowing down the entire machine because we're using more than 100% RAM or CPU, then it kills it if that goes on for more than 30 seconds continuously. If the lifetime of recent agents drops to a few seconds without producing successful results, we assume it's a resource exhaustion problem and we run Janitor. If that doesn't help, we poke the Manager with what we found and tell him to reorganize the system such that agents can complete their work. The watchdog must stay alive at all cost. We use the most reliable mechanism of keeping that watchdog alive. We must support fedora and nix-os, so probaby systemd and whatever nixos uses.
	- Uses a sqlite database as memory. Only write to md files when the instructions explicitly request that.
	- Writes events into that memory, e.g. starting a codex session, stopping a codex session, sending it a prompt.
	- Uses tmux for running the team so that it's inspectable by the user. Every codex session is in its own tmux window. It uses the current tmux session if we're in one, and only starts a new tmux session if we're not in a tmux session right now. It prints the session and the command to attach to it. It also stores it in sqlite and makes it easy to retrieve later.
	- We store in sqlite the details of each agentic run: start time, end time, current_status (e.g. running, crash, success), tmux pane, worktree/cwd, notes. We can store more information, but not less.
- It understands the following roles:
  - **Goal Planner:** asks for the goal, finds a deterministic way to measure success, e.g. % of unit tests passed from a pre-existing suite. Doesn't move on until we have that. Once it understands what we want to achieve and how, it plans out all the tasks. At the end it has to produce a `PLAN.md` file with ways of doing things, milestones, and a progress measure.
  - **Developer:** works on a task in a dedicated worktree. Reports success — ideally communicating it in some structured way so the runtime script can identify that and integrate it with the main branch. Developers run tests for their specific feature but they don't run tests for the entire project, as that would slow them down over time as there are more and more tests. Developers run in `/goal` mode until they achieve their expected outcome.
    - Have a `DEVELOPMENT.md` for the developer role. It should say: avoid creating a single huge file with the entire project code, and avoid fragmenting every little thing into its own file or function. Write relevant, intention-led docblocks for every function — or for most functions and types created. They could be one line long, or they could be 50 lines long or more when the function is nuanced or plays a non-obvious role. The important part is it should talk about *why* the function is there. Only talk about what it's doing and how it's doing it if that's not immediately clear from reading the function. It's also okay to document specific sections of functions inline.
	- Developers commit reasonably often to retain progress. They may revert their worklane as necessary if things break. They push their changes to a remote repo.
  - **Designer:** builds UI parts as needed. Uses an anti-slop skill to make the UI look human, not agentic. If `DESIGN.md` is present, it uses that.
  - **Auditor:** checks if we're making progress towards the goal as defined at the beginning or adjusted later on. Demands a quantifiable metric, such as number of unit tests passed. Doesn't accept any excuses as to why we're not making progress towards that metric. If the manager says there are blockers, decisions to be made, or we must wait for the integrator, treat that as unacceptable excuse and instruct it to solve these problems instead of slowing down work. We might need to research the root causes more deeply and/or involve an architect to refactor the system—all of which is fine. Even if everything looks fine, still dive into the work structure, find inefficiencies, and if you find plausible candidates, propose them to the manager.
  - **Manager:** looks at what's next and slices the work into separate worklanes that either do not conflict with one another, or mostly do not conflict. If we can identify long lanes with many tasks, that's great. If we have small lanes with just a few tasks, that's also fine — we will just have to keep finding more of them. Uses the SQLite database to store the upcoming work / next lanes, notes about the plan, the status of each lane, and relevant notes for the implementers. The planned work includes the agent type: Developer or Designer. You will receive feedback from other team-members—you need to evaluate it critically and apply at your discretion. It might or might not be relevant. The only exception is feedback from the `manhole` session, you must apply it unconditionally without asking questions.
  - **Integrator:** runs continuously and integrates all the work done in separate worktrees with the main repository branch. Once a branch is integrated, it deletes it.
  - **Architect:** looks for structural problems with the project based on repeated test failures, finds the pattern that connects them, and analyzes the codebase to understand the systemic root cause — then plans a refactor toward a more reliable structure.
  * **Status reporter**:
             Creates a progress.md AND a progress.html report about the current status
             of the work, summary of progress, next steps, current agents, tests runs,
			 % of metric ready, interesting events that happened recently. It also
			 pokes around the sqlite database to understand more than just the status
			 report numbers and enriches those status reports with a bit of a narrative.
			 The first run of the Updater establish .harness/STATUS_TEMPLATE.md and
			 .harness/STATUS_TEMPLATE.html templates that will be used for reporting
			 for the rest of the project. The subsequent runs copy these templates to
			 STATUS.md and STATUS.html and populate them with structured data about
			 the progress. It should always be clear and reasonably succinct. We must
			 list at least the major milestones with the metric details and % overall progress,
			 stages of the current milestone and their % progress,
			 (ideally a progress bar in the html version), summary of recent work, next
			 steps, challenges. Also reports how many worklanes are awaiting integration
			 and what's the expected improvement of the progress metric % upon integration,
			 and a line chart of progress every 15 minutes. Also report the sampled system CPU
			 and RAM usage over the last 6 hours in 60min (md) and 15min (html chart) intervals.
			 If the text on the rendered page becomes longer than 2500 words, we
			 summarize the longest sections. It must be possible to read in one seating
			 without extremely long stretches of text.
    - **Janitor:** finds abandoned resources that we don't need anymore and cleans them up. The overall deterministic harness scheduler runs it at least once every hour. The cleaned-up resources include disk directories, `/tmp` entries, worktrees, unnecessary tmux panes, ~/.codex growing too large. Does not remove unintegrated branches/worktrees.
- There is also a non-agentic updater loop that, every 15 minutes, runs the Updater to refresh `STATUS.md` and `STATUS.html`.
- There is also a non-agentic testing loop in one of the tmux sessions that continuously runs the full test suite for the project and logs the test results to the sqlite database, tying all the results from a specific run to that run so we can granularly query for information such as:
  - all failures from the last run
  - all skipped tests from the last run
  - all successes from the last run
  - failures from a specific test file from three runs ago
  We also need that database to have the full test logs in case the LLM needs to inspect them. We always keep the last 5 full test runs. The older ones we purge in such a way that we keep one report an hour from today, one report a day from the week before that, and one report per week from every month before that.
  - Any time a test in that non-agentic testing loop fails, we ask the Manager agent to add fixing these tests to the top of the work queue. In this way, the developers will continue with their current tasks until they're finished, and then will start fixing the main-branch problems afterwards.
  - Whenever a test fails and we fix it, we must store that as a bug report in the sqlite database. So a failing test would be a new issue, the root-cause investigation would be a description of that issue, the fix summary would be in a resolution column, and we would also log the first commit where it failed and the commit where it was fixed.
  - Before we fix a failing test, we do a lookup in the table to see if it failed in the past and what the causes were then. That becomes part of the context, as we may be continuously running into similar issues.
  - If we fix any test more than 3 times a day — i.e. it switches from passing to failing to passing to failing to passing to failing within the same 24 hours — we invoke an Architect to find the systemic failure that keeps causing problems and plan refactoring the system into a more reliable form. That refactor would ideally keep between *one* and *all* development agents busy and not block other concurrent work.
- Runs developers in separate git worktrees so they don't collide with each other.
- SQLite interactions are done through an MCP tool. Every agent has access to it and is also instructed how to use it in a very brief skill. You must build that MCP and skill and run tests to confirm they work.
– Spawning sub agents is done through an MCP tool that route the request back to the central harness scheduler such that we know the entire tree at any given time. All communication happens through other tools of this MCP.
- Brings over an open-source codebase indexer and provides codex with an MCP so that we don't have to grep things all the time. It should run independently of all the worklanes, not block starting a new worktree, and distinguish different worktrees so that each can benefit from it. If we can't run it in a specific worktree yet, just fall back to regular grepping etc. but keep checking for its readiness.
