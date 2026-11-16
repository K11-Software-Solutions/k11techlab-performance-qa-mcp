# Changelog

## 0.1.0 (15 November 2026)

- First release: load tests from API Functional QA flows (weighted scenarios, fixtures, chained values, auth).
- Per-endpoint p50/p90/p95/p99/max, error rate, 5xx and throughput; SLOs per endpoint or pattern.
- Baselines and regressions that need size, a minimum delta and statistical significance (Mann-Whitney U).
- Guardrails: no production load, write rules, caps on users, duration and rate, abort on high error rate.
- Engines: k6 (when installed) and builtin; MCP tools, CLI for CI, `/perf-check` and `/perf-baseline` commands.
- **JMeter import:** analyse existing JMeter results (`.jtl`, CSV or XML) with the same SLOs, baselines, regressions and report (`import` command, `import_jmeter_results` tool).
- `perf-report.json` is read by k11 Release Gate 0.3.0.
