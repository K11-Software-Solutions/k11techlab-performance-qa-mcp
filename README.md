# k11 Performance QA: MCP server and CI tool

<!-- mcp-name: io.github.K11-Software-Solutions/performance-qa -->

[![PyPI](https://img.shields.io/pypi/v/k11-performance-qa-mcp?label=PyPI)](https://pypi.org/project/k11-performance-qa-mcp/)
[![CI](https://github.com/K11-Software-Solutions/k11techlab-performance-qa-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/K11-Software-Solutions/k11techlab-performance-qa-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

**Load testing that answers a release question: did this change make the API slower, or break it under load?**

Most load-testing tools give you numbers. This one gives you **evidence**: per-endpoint latency against your SLOs, and regressions against a baseline that are **statistically significant**, so a noisy run doesn't block a release and a real slowdown doesn't slip through. The report feeds straight into the [k11 Release Gate](https://github.com/K11-Software-Solutions/k11techlab-release-gate-mcp).

- **Your API tests become load scenarios.** Scenarios reuse the user-story flows from [k11 API Functional QA](https://github.com/K11-Software-Solutions/k11techlab-api-functional-qa-mcp) (`tests/api/*.yaml`): chained calls, saved IDs, fixtures and auth. No second set of scripts to maintain.
- **Per-endpoint metrics:** p50, p90, p95, p99, max, error rate, 5xx count and throughput for every `METHOD /path/{param}`.
- **SLOs** per endpoint or pattern (`p95_ms`, `p99_ms`, `error_rate`, `min_rps`, …).
- **Regressions that mean something:** slower than the baseline by more than a tolerance **and** a minimum number of milliseconds **and** significant (one-sided Mann-Whitney U test).
- **Guardrails:** no load on production-like environments, data-changing flows only where writes are allowed, caps on virtual users, duration and request rate, and an automatic stop when too many requests fail.
- **Two engines:** [Grafana k6](https://k6.io) when it's installed, for heavy load; a builtin engine (Python, no install) for quick checks and CI.

## Quick start

```bash
pip install k11-performance-qa-mcp      # or: uvx k11-performance-qa-mcp
k11-performance-qa-mcp init             # writes .qa-mcp/perf.yaml
k11-performance-qa-mcp baseline         # on main: run and save the baseline
k11-performance-qa-mcp run              # on a branch: run, compare, write the report
```

In Claude Code, as a plugin with the `/perf-check` and `/perf-baseline` commands:

```text
/plugin marketplace add K11-Software-Solutions/k11techlab-performance-qa-mcp
/plugin install performance-qa@k11-performance-qa
```

Or as an MCP server in any client:

```json
{ "mcpServers": { "performance-qa": { "command": "uvx", "args": ["k11-performance-qa-mcp"] } } }
```

## Configure

Environments and auth come from `.qa-mcp/api-test.yaml` (shared with API Functional QA). Load settings live in `.qa-mcp/perf.yaml`:

```yaml
environment: staging
engine: auto                         # k6 if installed, else builtin
profile: { vus: 10, duration: 2m, ramp_up: 20s, think_time_ms: 200 }

scenarios:
  browse:
    flows:
      - { flow: tests/api/view_order.yaml, weight: 3 }   # 3x more often
      - tests/api/cancel_order.yaml

slos:
  "GET /orders/{id}": { p95_ms: 250, error_rate: 0.01 }
  "POST /orders":     { p95_ms: 600 }
  "*":                { p95_ms: 800, error_rate: 0.02 }

regression: { metric: p95, tolerance_pct: 10, min_delta_ms: 10, min_samples: 30, p_value: 0.01 }
guardrails: { max_vus: 50, max_duration: 10m, max_rps: 200, abort_error_rate: 0.5 }
```

Each virtual user runs a flow's fixtures once, then repeats: pick a flow by weight, run its HTTP steps in order (IDs saved by one step feed the next), run its cleanup, think, repeat. Database checks are skipped under load; they check correctness, not speed, and still run in your functional tests.

## How a regression is decided

For each endpoint, the run is compared with the saved baseline on the configured metric (p95 by default). It's a **regression** only when all of these hold:

1. it's slower by more than `tolerance_pct` (default 10%);
2. it's slower by more than `min_delta_ms` (default 10 ms), so 2 ms → 3 ms isn't "50% slower";
3. both runs have at least `min_samples` requests for that endpoint;
4. a one-sided Mann-Whitney U test says the new latencies are higher with p < `p_value` (default 0.01).

A rise in the error rate of more than 1 percentage point is reported separately. Improvements are reported too.

## What the report contains

`.qa-mcp/perf-report/perf-report.json` holds the verdict, per-endpoint metrics and status codes, SLO results, the baseline comparison (with p-values), findings, error examples, a per-second timeline and the profile and engine used. `perf-report.md` is ready for a pull request or CI step summary. Findings:

| Finding | Release Gate rule | Default outcome |
|---|---|---|
| 5xx responses under load | `perf.server_errors` | block |
| An SLO missed | `perf.slo_breached` | block |
| Run aborted (too many failures) | `perf.aborted` | block |
| Significant regression vs baseline | `perf.regression` | needs a person |
| Run refused by guardrails / didn't start | `perf.not_run` | needs a person |
| No baseline, or too few samples | `perf.no_baseline`, `perf.insufficient_samples` | note |

## Tools (MCP)

| Tool | What it does |
|---|---|
| `perf_config` | Environments (and whether load is allowed, and why), scenarios and their flows, SLOs, guardrails, the engine that will run, saved baselines |
| `run_load_test` | Run a scenario; returns the verdict, findings, per-endpoint latency and the baseline comparison; writes the report |
| `save_baseline` | Save the last completed run (or a fresh run) as the baseline |
| `get_report` | The last report, or one endpoint's detail: percentiles, status codes, SLOs, comparison, error examples |
| `init_perf_config` | Write a starter `.qa-mcp/perf.yaml` |

## Command line

```text
k11-performance-qa-mcp run      [--scenario S] [--env E] [--vus N] [--duration 2m] [--ramp-up 20s] [--max-rps N]
                                [--engine auto|k6|builtin] [--no-compare] [--fail-on any|slo|regression|never]
k11-performance-qa-mcp baseline [same options]  |  baseline --from-last-run
k11-performance-qa-mcp init
```

In GitHub Actions it writes the Markdown report to the step summary and sets `verdict`, `p95`, `slo_breaches` and `regressions` outputs. See [docs/perf-tests.yml](docs/perf-tests.yml) for a workflow that saves the baseline on main and compares every pull request with it.

## Safety

- **No production load:** environments named like production are refused unless they set both `allow_load: true` and `production_load_acknowledged: true` in `api-test.yaml`. Other environments need a local host, a test-like name (`staging`, `qa`, `dev`, …) or `allow_load: true`.
- **Writes follow API Functional QA's rules:** a scenario with data-changing calls is refused on an environment that doesn't allow writes, before any request is sent.
- **Caps:** `max_vus`, `max_duration` and `max_rps`; the run stops when the error rate passes `abort_error_rate`.
- **Secrets** come from `${ENV:NAME}` and never appear in reports or the generated k6 script; k6 receives auth headers through its environment.

## Engines

| | builtin | k6 |
|---|---|---|
| Install | none | [install k6](https://grafana.com/docs/k6/latest/set-up/install-k6/) (or set `K6_PATH`) |
| Load | up to a few hundred requests per second | thousands per second |
| Checks | status and value checks from the flow | status codes (value checks run in functional tests) |
| Fixtures and auth | yes | yes (prepared in Python, handed to k6) |
| Database cleanup steps | yes | no; use a disposable test database |

Keep the engine the same between a baseline and the runs compared with it; the report warns when they differ.

## Demo

`demo/shop` is a small order API with switches to simulate a bad release: `--slow-ms 80` slows `GET /orders/{id}`, `--error-every 5` makes every fifth call fail.

```bash
python demo/shop/server.py --port 8080 &
cd demo/shop && SHOP_URL=http://127.0.0.1:8080 SHOP_TOKEN=test-token k11-performance-qa-mcp baseline --scenario browse
# restart the server with --slow-ms 80, then:
SHOP_URL=http://127.0.0.1:8080 SHOP_TOKEN=test-token k11-performance-qa-mcp run --scenario browse
# ❌ regression: GET /orders/{id}: p95 32.1 → 92.1 ms (+186.9%, p=0.0000)
```

## Part of the K11 QA suite

[Playwright QA](https://github.com/K11-Software-Solutions/k11techlab-playwright-qa-mcp) · [Selenium QA](https://github.com/K11-Software-Solutions/k11techlab-selenium-qa-mcp) · [API Contract QA](https://github.com/K11-Software-Solutions/k11techlab-api-contract-qa-mcp) · [API Functional QA](https://github.com/K11-Software-Solutions/k11techlab-api-functional-qa-mcp) · [LLM Assure](https://github.com/K11-Software-Solutions/k11techlab-llm-assurance-toolkit) · **Performance QA** · [Release Gate](https://github.com/K11-Software-Solutions/k11techlab-release-gate-mcp) · [Release Readiness Agent](https://github.com/K11-Software-Solutions/k11techlab-release-readiness-agent)

## Development

```bash
python -m venv .venv && .venv/Scripts/pip install -e ".[dev]"   # bin/pip on macOS/Linux
pytest   # statistics, guardrails, real load against the demo shop, CLI, MCP; the k6 test runs when k6 is installed
```

## Roadmap

- LLM performance and cost: time to first token, tokens per second, cost per request and per conversation.
- Open-model (arrival-rate) scenarios and soak tests.
- Comparing two branches deployed side by side.

---

Built by [K11 Software Solutions](https://k11softwaresolutions.com). MIT licensed.
