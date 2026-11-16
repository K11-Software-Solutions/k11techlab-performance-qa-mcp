"""One load test (or imported JMeter results), end to end: run, analyse, compare, report."""

from __future__ import annotations

import threading
from pathlib import Path

from k11_api_qa import config as api_config

from . import analyze, perfconfig, report
from .engine import RunResult, now_iso, precheck, rel, resolve_scenario, run_builtin
from .k6engine import find_k6, run_k6


def run(root: Path, scenario: str | None = None, environment: str | None = None, overrides: dict | None = None,
        engine: str | None = None, compare_baseline: bool = True, report_dir: str = ".qa-mcp/perf-report",
        save_as_baseline: bool = False, stop: threading.Event | None = None) -> dict:
    root = Path(root).resolve()
    api_cfg = api_config.load(root)
    perf = perfconfig.load(root)
    scen_name, scen = perf.scenario(scenario)
    profile = perf.profile_for(scen, overrides)
    flows = resolve_scenario(api_cfg, scen)
    env_name, env, base_url, problems = precheck(api_cfg, perf, flows, environment, profile)

    choice = engine or perf.engine
    k6 = find_k6() if choice in ("auto", "k6") else None
    if choice == "k6" and not k6:
        problems.append("engine k6 requested but k6 is not installed (https://grafana.com/docs/k6/latest/set-up/install-k6/)")

    if problems:
        res = RunResult("blocked", env_name, scen_name, k6 and "k6" or "builtin", profile.as_dict(),
                        started_at=now_iso(), reason="; ".join(problems))
    elif k6:
        res = run_k6(api_cfg, perf, flows, env_name, env, base_url, profile, scen_name, k6, stop)
    else:
        res = run_builtin(api_cfg, perf, flows, env_name, profile, scen_name, stop)

    return finish(root, perf, res, compare_baseline, report_dir, save_as_baseline)


def import_results(root: Path, path: str, scenario: str | None = None, environment: str | None = None,
                   compare_baseline: bool = True, report_dir: str = ".qa-mcp/perf-report",
                   save_as_baseline: bool = False) -> dict:
    """Analyse a JMeter results file (JTL, CSV or XML) inside the project like a run of our own."""
    from k11_contract_qa.spec import inside

    from .jmeter import load_jtl

    root = Path(root).resolve()
    perf = perfconfig.load(root)
    res = load_jtl(inside(root, path), scenario or "jmeter", environment or perf.environment or "default")
    return finish(root, perf, res, compare_baseline, report_dir, save_as_baseline)


def finish(root: Path, perf: perfconfig.PerfConfig, res: RunResult, compare_baseline: bool, report_dir: str,
           save_as_baseline: bool) -> dict:
    scen_name, env_name = res.scenario, res.environment
    eps = analyze.endpoint_metrics(res)
    total = analyze.overall(res, eps)
    slo_results = analyze.check_slos(eps, perf.slos) if res.status in ("completed", "aborted") else []
    baseline, bpath = report.load_baseline(root, scen_name, env_name)
    comparison = None
    if compare_baseline and baseline and res.status == "completed" and not save_as_baseline:
        comparison = analyze.compare(res, eps, baseline, perf.regression)
        if baseline.get("engine") and baseline["engine"] != res.engine:
            res.notes.append(f"Baseline was measured with the {baseline['engine']} engine and this run with "
                             f"{res.engine}; latency definitions differ slightly.")
    fs = analyze.findings(res, eps, slo_results, comparison)
    verdict = analyze.verdict(fs)
    doc = report.build(res, eps, total, slo_results, comparison, fs, verdict,
                       rel(root, bpath) if comparison else None)
    samples = report.samples_doc(res, eps)
    from k11_contract_qa.spec import inside

    paths = report.write(doc, samples, inside(root, report_dir))
    out = {"report": doc, "paths": {k: rel(root, v) for k, v in paths.items()}}
    if save_as_baseline:
        if res.status != "completed":
            out["baseline_saved"] = None
            out["baseline_error"] = f"Not saved: the run {res.status} ({res.reason})"
        else:
            out["baseline_saved"] = rel(root, report.save_baseline(root, samples))
    return out
