# Developer Role Instructions

Avoid creating a single huge file with the entire project code, and avoid
fragmenting every little thing into its own file or function.

Write relevant, intention-led docblocks for every function or most functions and
types you create. They may be one line long, or much longer when a function is
nuanced or plays a non-obvious role. Explain why the function exists. Only
explain what it does or how it works when that is not clear from reading the
function. Inline comments are appropriate for specific surprising sections.

Developers work in dedicated git worktrees, commit reasonably often, run focused
tests for their specific feature, and push their branch when a remote is
configured.
