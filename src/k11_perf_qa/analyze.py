"""From raw samples to evidence: per-endpoint metrics, SLO checks, baseline comparison, findings.

Findings (what the Release Gate reads):
  server_errors          5xx responses under load (a bug, whatever the latency)
  slo_breach             an endpoint missed an SLO from perf.yaml
  regression             slower than the baseline beyond tolerance, min_delta_ms AND significance
  error_rate_regression  more failed requests than the baseline
  aborted / not_run      the run stopped on the error-rate guardrail, or never started
  insufficient_samples   too few requests to compare an endpoint with its baseline
"""

from __future__ import annotations

import fnmatch
from typing import Any

from .engine import RunResult
from .stats import mann_whitney_greater, summarize

SLO_KEYS = {"avg_ms": "avg", "p50_ms": "p50", "p90_ms": "p90", "p95_ms": "p95", "p99_ms": "p99", "max_ms": "max",
            "error_rate": "error_rate", "min_rps": "rps"}


def endpoint_metrics(res: RunResult) -> list[dict]:
    dur = max(res.duration_s, 0.001)
    out = []
    for ep in res.endpoints:
        n = res.requests(ep)
        errs = res.errors.get(ep, 0)
        m = {"endpoint": ep, "requests": n, "errors": errs, "error_rate": round(errs / n, 4) if n else 0.0,
             "server_errors": res.server_errors.get(ep, 0), "check_failures": res.check_failures.get(ep, 0),
             "rps": round(n / dur, 2), "status_codes": {str(k): v for k, v in sorted(res.codes.get(ep, {}).items())},
             **summarize(res.samples.get(ep, []))}
        out.append(m)
    return out


def overall(res: RunResult, eps: list[dict]) -> dict:
    allv = [v for ep in res.samples.values() for v in ep]
    n = sum(e["requests"] for e in eps)
    errs = sum(e["errors"] for e in eps)
    return {"requests": n, "errors": errs, "error_rate": round(errs / n, 4) if n else 0.0,
            "server_errors": sum(e["server_errors"] for e in eps), "check_failures": sum(e["check_failures"] for e in eps),
            "rps": round(n / max(res.duration_s, 0.001), 2), "iterations": res.iterations, **summarize(allv)}


def _slo_for(ep: str, slos: dict[str, dict]) -> tuple[str | None, dict]:
    if ep in slos:
        return ep, slos[ep]
    for pat, spec in slos.items():
        if pat != "*" and fnmatch.fnmatchcase(ep, pat):
            return pat, spec
    if "*" in slos:
        return "*", slos["*"]
    return None, {}


def check_slos(eps: list[dict], slos: dict[str, dict]) -> list[dict]:
    out = []
    for e in eps:
        pat, spec = _slo_for(e["endpoint"], slos)
        for key, target in (spec or {}).items():
            if key not in SLO_KEYS:
                raise ValueError(f"Unknown SLO '{key}' for {pat} (use {sorted(SLO_KEYS)})")
            metric = SLO_KEYS[key]
            actual = e[metric]
            ok = actual >= float(target) if key == "min_rps" else actual <= float(target)
            if e["requests"] == 0 and key != "error_rate":
                continue
            out.append({"endpoint": e["endpoint"], "slo": key, "pattern": pat, "target": target, "actual": actual, "ok": ok})
    return out


def compare(res: RunResult, eps: list[dict], baseline: dict, reg: dict) -> dict:
    metric = reg["metric"]
    tol, min_ms, min_n, alpha = float(reg["tolerance_pct"]), float(reg["min_delta_ms"]), int(reg["min_samples"]), float(reg["p_value"])
    base_eps = {e["endpoint"]: e for e in baseline.get("endpoints", [])}
    base_samples = baseline.get("samples", {})
    rows, regressions, improvements, insufficient = [], [], [], []
    for e in eps:
        b = base_eps.get(e["endpoint"])
        if not b:
            rows.append({"endpoint": e["endpoint"], "status": "new"})
            continue
        cur, old = float(e[metric]), float(b[metric])
        delta = round(cur - old, 1)
        pct = round((cur - old) / old * 100, 1) if old else None
        n_cur, n_base = len(res.samples.get(e["endpoint"], [])), len(base_samples.get(e["endpoint"], []))
        row: dict[str, Any] = {"endpoint": e["endpoint"], "metric": metric, "baseline": old, "current": cur,
                               "delta_ms": delta, "delta_pct": pct, "samples": [n_base, n_cur]}
        if n_cur < min_n or n_base < min_n:
            row["status"] = "insufficient_samples"
            insufficient.append(row)
        else:
            p_slower = mann_whitney_greater(base_samples[e["endpoint"]], res.samples[e["endpoint"]])
            p_faster = mann_whitney_greater(res.samples[e["endpoint"]], base_samples[e["endpoint"]])
            row["p_value"] = p_slower
            if pct is not None and pct > tol and delta > min_ms and p_slower < alpha:
                row["status"] = "regression"
                regressions.append(row)
            elif pct is not None and pct < -tol and -delta > min_ms and p_faster < alpha:
                row["status"] = "improvement"
                row["p_value"] = p_faster
                improvements.append(row)
            else:
                row["status"] = "unchanged"
        er_delta = round(e["error_rate"] - float(b.get("error_rate", 0)), 4)
        row["error_rate"] = [b.get("error_rate", 0), e["error_rate"]]
        if er_delta > float(reg.get("error_rate_delta", 0.01)) and e["errors"]:
            row["error_rate_regression"] = er_delta
        rows.append(row)
    missing = sorted(set(base_eps) - {e["endpoint"] for e in eps})
    return {"baseline_generated_at": baseline.get("generatedAt"), "baseline_engine": baseline.get("engine"),
            "baseline_environment": baseline.get("environment"), "metric": metric, "tolerance_pct": tol,
            "min_delta_ms": min_ms, "min_samples": min_n, "p_value": alpha, "endpoints": rows,
            "regressions": regressions, "improvements": improvements, "insufficient_samples": insufficient,
            "error_rate_regressions": [r for r in rows if r.get("error_rate_regression")], "missing_endpoints": missing}


def findings(res: RunResult, eps: list[dict], slo_results: list[dict], comparison: dict | None) -> list[dict]:
    out: list[dict] = []
    if res.status == "blocked":
        out.append({"type": "not_run", "message": f"Load test did not run: {res.reason}"})
    elif res.status == "error":
        out.append({"type": "not_run", "message": f"Load test failed to run: {res.reason}"})
    elif res.status == "aborted":
        out.append({"type": "aborted", "message": f"Load test aborted: {res.reason}"})
    for e in eps:
        if e["server_errors"]:
            out.append({"type": "server_errors", "endpoint": e["endpoint"],
                        "message": f"{e['endpoint']}: {e['server_errors']} server error(s) (5xx) under load"})
    for s in slo_results:
        if not s["ok"]:
            unit = "" if s["slo"] in ("error_rate", "min_rps") else " ms"
            shown = f"{s['actual']:.2%}" if s["slo"] == "error_rate" else f"{s['actual']}{unit}"
            target = f"{float(s['target']):.2%}" if s["slo"] == "error_rate" else f"{s['target']}{unit}"
            out.append({"type": "slo_breach", "endpoint": s["endpoint"],
                        "message": f"{s['endpoint']}: {s['slo']} {shown} misses the SLO ({target})"})
    if comparison:
        for r in comparison["regressions"]:
            out.append({"type": "regression", "endpoint": r["endpoint"],
                        "message": f"{r['endpoint']}: {r['metric']} {r['baseline']} → {r['current']} ms "
                                   f"(+{r['delta_pct']}%, p={r['p_value']:.4f})"})
        for r in comparison["error_rate_regressions"]:
            out.append({"type": "error_rate_regression", "endpoint": r["endpoint"],
                        "message": f"{r['endpoint']}: error rate {r['error_rate'][0]:.2%} → {r['error_rate'][1]:.2%}"})
        for r in comparison["insufficient_samples"]:
            out.append({"type": "insufficient_samples", "endpoint": r["endpoint"],
                        "message": f"{r['endpoint']}: too few samples to compare ({r['samples'][0]} baseline, "
                                   f"{r['samples'][1]} now; need {comparison['min_samples']})"})
    return out


def verdict(fs: list[dict]) -> str:
    bad = {"not_run", "aborted", "server_errors", "slo_breach", "regression", "error_rate_regression"}
    return "fail" if any(f["type"] in bad for f in fs) else "pass"
