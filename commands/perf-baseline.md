---
description: Save a performance baseline from the current branch (use on main or a release you trust)
argument-hint: [scenario] [environment]
---

Save the performance baseline that later runs are compared with, using the performance-qa MCP tools.

Input: $ARGUMENTS

1. Check the current git branch. If it isn't main (or the default branch), warn that a baseline should come from code you trust, and ask before continuing.
2. Call `perf_config` and confirm the scenario and environment. Load must be allowed there.
3. If the last run (`get_report`) completed on this same code, offer to save it with `save_baseline` (from_last_run). Otherwise run and save in one step with `save_baseline` and from_last_run=false.
4. Report what was saved: the file under `.qa-mcp/perf-baseline/`, the number of requests per endpoint, and p95 per endpoint. Suggest committing the baseline file so CI can compare pull requests with it.

Don't save a run that had errors, was aborted, or was blocked.
