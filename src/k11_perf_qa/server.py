"""MCP server: load and performance testing as release evidence."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

from . import __version__, perfconfig, report
from .k6engine import find_k6


def project_root() -> Path:
    return Path(os.environ.get("QA_MCP_ROOT") or os.getcwd()).resolve()


def _brief(doc: dict, limit: int = 25) -> dict:
    eps = sorted(doc["endpoints"], key=lambda e: (-e["errors"], -e["p95"]))[:limit]
    return {"verdict": doc["summary"]["verdict"], "status": doc["status"], "reason": doc["reason"],
            "environment": doc["environment"], "scenario": doc["scenario"], "engine": doc["engine"],
            "profile": doc["profile"], "summary": doc["summary"], "findings": doc["findings"],
            "endpoints": [{k: e[k] for k in ("endpoint", "requests", "errors", "error_rate", "server_errors", "p50", "p95",
                                             "p99", "max", "rps")} for e in eps],
            "comparison": None if not doc["comparison"] else {
                k: doc["comparison"][k] for k in ("baseline_generated_at", "metric", "tolerance_pct", "min_delta_ms",
                                                  "p_value", "regressions", "improvements", "insufficient_samples",
                                                  "error_rate_regressions", "missing_endpoints")},
            "notes": doc["notes"]}


def create_server() -> FastMCP:
    mcp = FastMCP(
        "k11-performance-qa",
        instructions=(
            "Load and performance testing that produces release evidence. Scenarios reuse k11 API Functional QA flows "
            "(tests/api/*.yaml) as realistic traffic; environments and auth come from .qa-mcp/api-test.yaml; SLOs, "
            "scenarios and guardrails from .qa-mcp/perf.yaml. Start with perf_config. run_load_test measures latency "
            "percentiles, errors and throughput per endpoint, checks SLOs and compares with the saved baseline; a "
            "regression needs to exceed the tolerance, a minimum number of ms AND be statistically significant. "
            "Save a baseline from main (or a trusted release) with save_baseline. Load is refused on production-like "
            "environments; never raise guardrails or point a test at production to make it pass."
        ),
    )
    mcp._mcp_server.version = __version__

    @mcp.tool(title="Performance config")
    def perf_config() -> dict:
        """Environments (and whether load is allowed and why), scenarios with their flows, SLOs, regression settings,
        guardrails, the engine that would run (k6 if installed, else builtin) and the saved baselines."""
        from k11_api_qa import config as api_config

        from .engine import resolve_scenario

        root = project_root()
        api_cfg = api_config.load(root)
        perf = perfconfig.load(root)
        envs = {}
        for name, env in api_cfg.environments.items():
            base = str(env.get("base_url", ""))
            ok, why = perfconfig.load_allowed(name, env, base if not base.startswith("${") else "http://unresolved")
            envs[name] = {"base_url": base, "load_allowed": ok, "why": why}
        scens = {}
        for name in (perf.scenarios or {"default": {}}):
            try:
                n, sc = perf.scenario(name)
                flows = resolve_scenario(api_cfg, sc)
                scens[n] = {"flows": [{"file": f.file, "name": f.flow["name"], "weight": f.weight} for f in flows],
                            "profile": perf.profile_for(sc).as_dict()}
            except ValueError as e:
                scens[name] = {"error": str(e)}
        base_dir = root / report.BASELINE_DIR
        return {"config": perfconfig.PERF_CONFIG if perf.exists else None, "default_environment": perf.environment
                or api_cfg.default_environment, "environments": envs, "scenarios": scens, "slos": perf.slos,
                "regression": perf.regression, "guardrails": perf.guardrails,
                "engine": {"configured": perf.engine, "k6": find_k6(), "would_use": "k6" if perf.engine != "builtin" and find_k6() else "builtin"},
                "baselines": sorted(p.name for p in base_dir.glob("*.json")) if base_dir.is_dir() else []}

    @mcp.tool(title="Run load test")
    def run_load_test(scenario: Optional[str] = None, environment: Optional[str] = None, vus: Optional[int] = None,
                      duration: Optional[str] = None, ramp_up: Optional[str] = None, max_rps: Optional[float] = None,
                      engine: Optional[str] = None, compare_baseline: bool = True) -> dict:
        """Run a load test and write .qa-mcp/perf-report/ (JSON evidence for the Release Gate, Markdown, samples).
        Returns the verdict, findings (server errors, SLO breaches, statistically significant regressions), per-endpoint
        latency (p50/p95/p99), errors and throughput, and the baseline comparison. Overrides (vus, duration like '30s',
        ramp_up, max_rps, engine 'k6'|'builtin') are still capped by the guardrails. Refused on production-like
        environments and when data-changing flows target a read-only environment."""
        from .run import run

        out = run(project_root(), scenario, environment,
                  {"vus": vus, "duration": duration, "ramp_up": ramp_up, "max_rps": max_rps}, engine, compare_baseline)
        return {**_brief(out["report"]), "paths": out["paths"]}

    @mcp.tool(title="Save baseline")
    def save_baseline(from_last_run: bool = True, scenario: Optional[str] = None, environment: Optional[str] = None,
                      vus: Optional[int] = None, duration: Optional[str] = None) -> dict:
        """Save the baseline later runs are compared with. from_last_run=True uses the last completed run's samples
        (.qa-mcp/perf-report/samples.json); otherwise runs the scenario now and saves it. Do this on main or a release
        you trust, not on the branch under test."""
        root = project_root()
        if from_last_run:
            s = root / ".qa-mcp/perf-report/samples.json"
            r = root / ".qa-mcp/perf-report/perf-report.json"
            if not s.is_file() or not r.is_file():
                raise ValueError("No previous run found; run_load_test first, or call with from_last_run=false")
            doc = json.loads(r.read_text("utf8"))
            if doc.get("status") != "completed":
                raise ValueError(f"The last run {doc.get('status')} ({doc.get('reason')}); it can't be a baseline")
            p = report.save_baseline(root, json.loads(s.read_text("utf8")))
            return {"saved": p.relative_to(root).as_posix(), "from": doc["generatedAt"], "endpoints": len(doc["endpoints"])}
        from .run import run

        out = run(root, scenario, environment, {"vus": vus, "duration": duration}, None, save_as_baseline=True)
        return {"saved": out.get("baseline_saved"), "error": out.get("baseline_error"), **_brief(out["report"])}

    @mcp.tool(title="Get report")
    def get_report(endpoint: Optional[str] = None) -> dict:
        """The last run's report. With endpoint ('GET /orders/{id}'), that endpoint's full detail: latency
        percentiles, status codes, SLO results, baseline comparison and example errors."""
        p = project_root() / ".qa-mcp/perf-report/perf-report.json"
        if not p.is_file():
            raise ValueError("No report yet; run_load_test first")
        doc = json.loads(p.read_text("utf8"))
        if not endpoint:
            return _brief(doc)
        ep = next((e for e in doc["endpoints"] if e["endpoint"] == endpoint), None)
        if ep is None:
            raise ValueError(f"No endpoint '{endpoint}'. Known: {[e['endpoint'] for e in doc['endpoints']]}")
        cmp_row = next((r for r in (doc["comparison"] or {}).get("endpoints", []) if r["endpoint"] == endpoint), None)
        return {"endpoint": ep, "slos": [s for s in doc["slos"] if s["endpoint"] == endpoint], "comparison": cmp_row,
                "error_examples": [x for x in doc["error_examples"] if x["endpoint"] == endpoint],
                "findings": [f for f in doc["findings"] if f.get("endpoint") == endpoint]}

    @mcp.tool(title="Create performance config")
    def init_perf_config(overwrite: bool = False) -> dict:
        """Write a starter .qa-mcp/perf.yaml (scenario, profile, SLOs, regression settings, guardrails). Environments
        and auth stay in .qa-mcp/api-test.yaml."""
        p = project_root() / perfconfig.PERF_CONFIG
        if p.exists() and not overwrite:
            raise ValueError(f"{perfconfig.PERF_CONFIG} already exists (overwrite=true to replace it)")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(perfconfig.init_text(), "utf8")
        return {"written": perfconfig.PERF_CONFIG, "content": perfconfig.init_text()}

    return mcp


def serve() -> None:
    create_server().run()
