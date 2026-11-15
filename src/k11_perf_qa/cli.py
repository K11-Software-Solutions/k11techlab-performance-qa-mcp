"""Command line: start the MCP server (default), run load tests in CI, or save a baseline."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__

USAGE = f"""k11-performance-qa-mcp {__version__}

Usage:
  k11-performance-qa-mcp                     start the MCP server on stdio
  k11-performance-qa-mcp run [opts]          run a load test, compare with the baseline, write reports
  k11-performance-qa-mcp baseline [opts]     run a load test and save it as the baseline (do this on main)
  k11-performance-qa-mcp baseline --from-last-run   save the last completed run as the baseline
  k11-performance-qa-mcp init                write a starter .qa-mcp/perf.yaml
  k11-performance-qa-mcp --version

run / baseline options:
  --scenario NAME      scenario from .qa-mcp/perf.yaml (default: default, or every flow in tests/api)
  --env NAME           environment from .qa-mcp/api-test.yaml
  --vus N  --duration 30s  --ramp-up 5s  --max-rps N   override the profile (still capped by guardrails)
  --engine NAME        auto (default) | k6 | builtin
  --no-compare         don't compare with the baseline
  --report-dir PATH    default .qa-mcp/perf-report
  --fail-on LEVEL      any (default) | slo (SLOs, 5xx, aborted) | regression (regressions, aborted) | never

The project is QA_MCP_ROOT or the current directory.
"""

FAIL_ON = {"any": {"not_run", "aborted", "server_errors", "slo_breach", "regression", "error_rate_regression"},
           "slo": {"not_run", "aborted", "server_errors", "slo_breach"},
           "regression": {"not_run", "aborted", "regression", "error_rate_regression"}, "never": set()}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # e.g. "→" on a Windows console
        except (AttributeError, ValueError):
            pass
    if not argv:
        from .server import serve

        serve()
        return 0
    cmd = argv[0]
    if cmd in ("--version", "-v"):
        print(__version__)
        return 0
    if cmd in ("--help", "-h", "help"):
        print(USAGE)
        return 0
    root = Path(os.environ.get("QA_MCP_ROOT") or os.getcwd()).resolve()
    if cmd == "init":
        from .perfconfig import PERF_CONFIG, init_text

        p = root / PERF_CONFIG
        if p.exists():
            print(f"{PERF_CONFIG} already exists", file=sys.stderr)
            return 1
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(init_text(), "utf8")
        print(f"Wrote {PERF_CONFIG}")
        return 0
    if cmd not in ("run", "baseline"):
        print(f"Unknown command: {cmd}\n\n{USAGE}", file=sys.stderr)
        return 2
    p = argparse.ArgumentParser(prog=f"k11-performance-qa-mcp {cmd}")
    p.add_argument("--scenario")
    p.add_argument("--env")
    p.add_argument("--vus", type=int)
    p.add_argument("--duration")
    p.add_argument("--ramp-up")
    p.add_argument("--max-rps", type=float)
    p.add_argument("--engine", choices=["auto", "k6", "builtin"])
    p.add_argument("--no-compare", action="store_true")
    p.add_argument("--report-dir", default=".qa-mcp/perf-report")
    p.add_argument("--fail-on", default="any", choices=sorted(FAIL_ON))
    p.add_argument("--from-last-run", action="store_true")
    a = p.parse_args(argv[1:])

    from k11_api_qa.runner import FlowError

    from . import report
    from .run import run

    if cmd == "baseline" and a.from_last_run:
        s = root / a.report_dir / "samples.json"
        r = root / a.report_dir / "perf-report.json"
        if not s.is_file() or not r.is_file():
            print("No previous run found", file=sys.stderr)
            return 1
        doc = json.loads(r.read_text("utf8"))
        if doc.get("status") != "completed":
            print(f"The last run {doc.get('status')}; it can't be a baseline", file=sys.stderr)
            return 1
        path = report.save_baseline(root, json.loads(s.read_text("utf8")))
        print(f"Baseline saved: {path.relative_to(root).as_posix()}")
        return 0
    try:
        out = run(root, a.scenario, a.env, {"vus": a.vus, "duration": a.duration, "ramp_up": a.ramp_up,
                                            "max_rps": a.max_rps}, a.engine, not a.no_compare, a.report_dir,
                  save_as_baseline=(cmd == "baseline"))
    except (ValueError, FlowError, OSError) as e:
        print(f"{cmd} failed: {e}", file=sys.stderr)
        return 1
    doc = out["report"]
    s = doc["summary"]
    print(f"{doc['status']}: {s['requests']} requests, {s['rps']} req/s, error rate {s['error_rate']:.2%}, "
          f"p95 {s['p95']} ms, {s['slo_breaches']} SLO breach(es), {s['regressions']} regression(s). "
          f"Report: {out['paths']['markdown']}")
    for f in doc["findings"]:
        print(f"  - {f['type']}: {f['message']}")
    if cmd == "baseline":
        if out.get("baseline_saved"):
            print(f"Baseline saved: {out['baseline_saved']}")
        else:
            print(out.get("baseline_error", "Baseline not saved"), file=sys.stderr)
            return 1
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf8") as fh:
            fh.write(f"verdict={s['verdict']}\nrequests={s['requests']}\np95={s['p95']}\n"
                     f"slo_breaches={s['slo_breaches']}\nregressions={s['regressions']}\nreport={out['paths']['markdown']}\n")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf8") as fh:
            fh.write((root / out["paths"]["markdown"]).read_text("utf8") + "\n")
    if cmd == "baseline":
        return 0
    return 1 if any(f["type"] in FAIL_ON[a.fail_on] for f in doc["findings"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
