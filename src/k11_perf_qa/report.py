"""Reports and baselines.

    .qa-mcp/perf-report/perf-report.json   evidence for the Release Gate (metrics, SLOs, comparison, findings)
    .qa-mcp/perf-report/perf-report.md     for pull requests and CI step summaries
    .qa-mcp/perf-report/samples.json       latency samples (downsampled), so this run can become a baseline
    .qa-mcp/perf-baseline/<scenario>-<environment>.json   the baseline runs are compared with

Save a baseline from main (or a release you trust); compare every change with it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import __version__
from .engine import now_iso
from .stats import downsample

BASELINE_DIR = ".qa-mcp/perf-baseline"
ICON = {"pass": "✅", "fail": "❌"}
SAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def baseline_path(root: Path, scenario: str, environment: str) -> Path:
    return Path(root) / BASELINE_DIR / f"{SAFE.sub('_', scenario)}-{SAFE.sub('_', environment)}.json"


def build(res, eps, total, slo_results, comparison, fs, verdict, baseline_file: str | None) -> dict:
    return {"tool": f"k11-performance-qa-mcp {__version__}", "generatedAt": now_iso(),
            "environment": res.environment, "scenario": res.scenario, "engine": res.engine, "profile": res.profile,
            "status": res.status, "reason": res.reason, "started_at": res.started_at, "duration_s": res.duration_s,
            "summary": {**total, "endpoints": len(eps), "slo_breaches": sum(1 for s in slo_results if not s["ok"]),
                        "regressions": len(comparison["regressions"]) if comparison else 0, "verdict": verdict},
            "endpoints": eps, "slos": slo_results, "baseline": baseline_file, "comparison": comparison,
            "findings": fs, "flows": res.flows, "error_examples": res.examples,
            "timeline": [{"second": s, "requests": v[0], "errors": v[1]} for s, v in sorted(res.timeline.items())],
            "notes": res.notes}


def samples_doc(res, eps) -> dict:
    return {"tool": f"k11-performance-qa-mcp {__version__}", "generatedAt": now_iso(), "environment": res.environment,
            "scenario": res.scenario, "engine": res.engine, "profile": res.profile, "endpoints": eps,
            "samples": {ep: downsample(v) for ep, v in res.samples.items()}}


def to_markdown(doc: dict) -> str:
    s = doc["summary"]
    v = s["verdict"]
    L = [f"## Performance: {ICON[v]} {'passed' if v == 'pass' else 'needs attention'}", "",
         f"Scenario **{doc['scenario']}** on **{doc['environment']}** ({doc['engine']} engine): "
         f"{doc['profile']['vus']} virtual users for {doc['profile']['duration_s']:g}s, "
         f"**{s['requests']} requests** at {s['rps']} req/s, error rate {s['error_rate']:.2%}, "
         f"p95 {s['p95']} ms, p99 {s['p99']} ms.", ""]
    if doc["status"] != "completed":
        L += [f"> ⛔ **{doc['status']}**: {doc['reason']}", ""]
    if doc["findings"]:
        L += ["### Findings", ""] + [f"- **{f['type'].replace('_', ' ')}**: {f['message']}" for f in doc["findings"]] + [""]
    if doc["endpoints"]:
        L += ["### Endpoints", "", "| Endpoint | Requests | Errors | p50 | p95 | p99 | Max | vs baseline |",
              "|---|---:|---:|---:|---:|---:|---:|---|"]
        cmp_rows = {r["endpoint"]: r for r in (doc["comparison"] or {}).get("endpoints", [])}
        for e in doc["endpoints"]:
            r = cmp_rows.get(e["endpoint"])
            vs = "–"
            if r and r.get("status") in ("regression", "improvement", "unchanged"):
                mark = {"regression": "🔺", "improvement": "🟢", "unchanged": "≈"}[r["status"]]
                vs = f"{mark} {r['delta_pct']:+}% ({r['baseline']} → {r['current']} ms)"
                if r["status"] == "unchanged" and abs(r["delta_pct"] or 0) > (doc["comparison"] or {}).get("tolerance_pct", 0):
                    vs += ", not significant"
            elif r:
                vs = r["status"].replace("_", " ")
            L.append(f"| `{e['endpoint']}` | {e['requests']} | {e['errors']} ({e['error_rate']:.1%}) | {e['p50']} | "
                     f"{e['p95']} | {e['p99']} | {e['max']} | {vs} |")
        L.append("")
    bad = [x for x in doc["slos"] if not x["ok"]]
    if doc["slos"]:
        L += [f"SLOs: {len(doc['slos']) - len(bad)}/{len(doc['slos'])} met.", ""]
    c = doc["comparison"]
    if c:
        L += [f"Compared with the baseline from {c['baseline_generated_at']} on **{c['metric']}**: a regression needs "
              f"> {c['tolerance_pct']:g}% and > {c['min_delta_ms']:g} ms slower, with p < {c['p_value']:g}.", ""]
    elif doc.get("baseline") is None and doc["status"] == "completed":
        L += ["No baseline yet: save one from main with `k11-performance-qa-mcp baseline` to catch regressions.", ""]
    for n in doc.get("notes") or []:
        L.append(f"> {n}")
    L += ["", f"<sub>{doc['tool']} · {doc['generatedAt']}</sub>", ""]
    return "\n".join(L)


def write(doc: dict, samples: dict, out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {"json": out_dir / "perf-report.json", "markdown": out_dir / "perf-report.md",
             "samples": out_dir / "samples.json"}
    paths["json"].write_text(json.dumps(doc, indent=2, default=str), "utf8")
    paths["markdown"].write_text(to_markdown(doc), "utf8")
    paths["samples"].write_text(json.dumps(samples, default=str), "utf8")
    return paths


def save_baseline(root: Path, samples: dict) -> Path:
    p = baseline_path(root, samples["scenario"], samples["environment"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(samples, default=str), "utf8")
    return p


def load_baseline(root: Path, scenario: str, environment: str) -> tuple[dict | None, Path]:
    p = baseline_path(root, scenario, environment)
    if not p.is_file():
        return None, p
    return json.loads(p.read_text("utf8")), p
