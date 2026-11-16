---
description: Load-test this change and explain latency, errors, SLOs and regressions against the baseline
argument-hint: "[scenario] [environment]"
---

Run a performance check with the performance-qa MCP tools and explain the result.

Input: $ARGUMENTS

1. Call `perf_config`. Pick the scenario and environment from the input, or the defaults. If load isn't allowed on that environment, stop and say why; never point a load test at production or raise guardrails to make it run.
2. If there's no scenario with flows yet, suggest one from the flows in `tests/api/` (read-heavy user journeys first) and offer to write `.qa-mcp/perf.yaml` with `init_perf_config`; ask before writing.
3. Call `run_load_test`. If the project load-tests with JMeter instead and a results file (`.jtl`) is named or present, call `import_jmeter_results` with it (and the same scenario and environment as its baseline).
4. Explain the result in plain language:
   - the verdict, then each finding: server errors under load, SLO misses (actual vs target), and regressions (baseline → now, % slower, and that it passed the significance test);
   - for the worst endpoint, call `get_report` with that endpoint and use its status codes and error examples to suggest likely causes (a missing index, N+1 queries, a cache miss, lock contention, a slow dependency);
   - say what wasn't covered: endpoints with too few samples, database checks skipped under load, or "no baseline yet".
5. If there's no baseline, explain that regressions need one and suggest `/perf-baseline` on main.

Never change SLOs, tolerances or the baseline to make a run pass. Finish with the verdict and the report path.
