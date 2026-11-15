"""Scenarios and load generation.

A scenario is a weighted set of API Functional flows (``tests/api/*.yaml``). Each virtual user
(VU) runs fixtures once, then repeats: pick a flow by weight, run its HTTP steps in order (values
saved by one step feed the next), run its cleanup, think, repeat. Database steps are skipped
under load (they check correctness, not speed). Latency is measured per endpoint:
"METHOD /path/{param}".

Engines: ``builtin`` (threads + httpx, no install needed, good up to a few hundred requests per
second) and ``k6`` (see k6engine.py) for heavier load. Both produce the same RunResult.
"""

from __future__ import annotations

import copy
import random
import re
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from k11_api_qa.config import Config as ApiConfig
from k11_api_qa.runner import SAFE_METHODS, FlowError, Runner, flow_files, load_flow
from k11_api_qa.values import Vars

from .perfconfig import PerfConfig, Profile, check_guardrails, effective_rps, load_allowed

VAR = re.compile(r"\$\{([^}]+)\}")
MAX_EXAMPLES = 20


def endpoint_key(step: dict) -> str:
    method, _, template = str(step["call"]).strip().partition(" ")
    template = VAR.sub(lambda m: "{" + m.group(1).split(".")[-1] + "}", template.strip().split("?")[0])
    return f"{method.upper()} {template}"


@dataclass
class ScenarioFlow:
    file: str
    flow: dict
    weight: float = 1.0


@dataclass
class RunResult:
    status: str  # completed | aborted | blocked | error
    environment: str
    scenario: str
    engine: str
    profile: dict
    started_at: str = ""
    duration_s: float = 0.0
    reason: str = ""
    samples: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    errors: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    server_errors: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    check_failures: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    codes: dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))
    examples: list[dict] = field(default_factory=list)
    timeline: dict[int, list[int]] = field(default_factory=lambda: defaultdict(lambda: [0, 0]))
    flows: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    iterations: int = 0

    def requests(self, ep: str) -> int:
        return len(self.samples.get(ep, [])) + self.transport_failures(ep)

    def transport_failures(self, ep: str) -> int:
        return self.codes.get(ep, Counter()).get(0, 0)

    @property
    def endpoints(self) -> list[str]:
        return sorted(set(self.samples) | set(self.errors) | set(self.codes))


class Collector:
    def __init__(self, result: RunResult):
        self.r = result
        self.lock = threading.Lock()
        self.t0 = time.perf_counter()
        self.total = 0
        self.failed = 0

    def add(self, ep: str, ms: float | None, status: int | None, failed: bool, check_failed: bool, error: str = ""):
        sec = int(time.perf_counter() - self.t0)
        with self.lock:
            self.total += 1
            if ms is not None:
                self.r.samples[ep].append(ms)
            self.r.codes[ep][status or 0] += 1
            if failed:
                self.failed += 1
                self.r.errors[ep] += 1
                if len(self.r.examples) < MAX_EXAMPLES:
                    self.r.examples.append({"endpoint": ep, "status": status, "error": error[:200]})
            if status and status >= 500:
                self.r.server_errors[ep] += 1
            if check_failed:
                self.r.check_failures[ep] += 1
            t = self.r.timeline[sec]
            t[0] += 1
            t[1] += int(failed)

    def error_rate(self) -> float:
        with self.lock:
            return self.failed / self.total if self.total else 0.0


class RateLimiter:
    """Shared across VUs: at most `rps` request starts per second."""

    def __init__(self, rps: float | None):
        self.interval = 1.0 / rps if rps else 0.0
        self.next = time.perf_counter()
        self.lock = threading.Lock()

    def wait(self, stop: threading.Event) -> None:
        if not self.interval:
            return
        with self.lock:
            now = time.perf_counter()
            slot = max(self.next, now)
            self.next = slot + self.interval
        delay = slot - time.perf_counter()
        if delay > 0:
            stop.wait(delay)


# ---- scenarios ----------------------------------------------------------------------------------

def resolve_scenario(api_cfg: ApiConfig, scen: dict) -> list[ScenarioFlow]:
    root = api_cfg.root
    entries = scen.get("flows") or []
    out: list[ScenarioFlow] = []
    if not entries:
        tags = set(scen.get("tags") or [])
        for p in flow_files(root, None):
            flow = load_flow(p)
            if tags and not tags & set(flow.get("tags") or []):
                continue
            out.append(ScenarioFlow(p.relative_to(root).as_posix(), flow))
    else:
        for e in entries:
            path, weight = (e, 1.0) if isinstance(e, str) else (e.get("flow"), float(e.get("weight", 1)))
            if not path:
                raise ValueError("Each scenario flow needs a path ('flow: tests/api/x.yaml')")
            files = flow_files(root, [str(path)])
            if not files:
                raise ValueError(f"Flow not found: {path}")
            for p in files:
                out.append(ScenarioFlow(p.relative_to(root).as_posix(), load_flow(p), weight))
    out = [f for f in out if any(s.get("call") for s in f.flow["steps"])]
    if not out:
        raise ValueError("The scenario has no flows with HTTP steps (add flows to tests/api/ or list them under scenarios)")
    if any(f.weight <= 0 for f in out):
        raise ValueError("Flow weights must be positive")
    return out


def precheck(api_cfg: ApiConfig, perf: PerfConfig, flows: list[ScenarioFlow], env_name: str | None,
             profile: Profile) -> tuple[str, dict, str, list[str]]:
    """(environment name, env, base_url, problems). Problems mean no request may be sent."""
    name, env = api_cfg.environment(env_name or perf.environment or None)
    base = Vars().sub(env.get("base_url", "")) if "${" in str(env.get("base_url", "")) else env.get("base_url", "")
    problems: list[str] = []
    if not str(base).startswith(("http://", "https://")):
        problems.append(f"Environment '{name}' needs base_url (http:// or https://)")
        return name, env, str(base), problems
    ok, why = load_allowed(name, env, str(base))
    if not ok:
        problems.append(f"Load refused on '{name}': {why}")
    problems += check_guardrails(profile, perf.guardrails)
    from k11_api_qa.config import writes_allowed

    w_ok, w_why = writes_allowed(name, env, str(base))
    if not w_ok:
        writers = sorted({f"{f.file}: {s['call']}" for f in flows for s in f.flow["steps"]
                          if s.get("call") and str(s["call"]).split(" ", 1)[0].upper() not in SAFE_METHODS})
        if writers:
            problems.append(f"Data-changing calls are refused on '{name}' ({w_why}): " + "; ".join(writers[:5]))
    return name, env, str(base), problems


# ---- builtin engine -----------------------------------------------------------------------------

def _status_ok(sr) -> bool:
    return all(a["ok"] for a in sr.assertions if a["check"].startswith("status") or a["check"] == "no server error")


def run_builtin(api_cfg: ApiConfig, perf: PerfConfig, flows: list[ScenarioFlow], env_name: str, profile: Profile,
                scenario: str, stop: threading.Event | None = None) -> RunResult:
    stop = stop or threading.Event()
    res = RunResult("completed", env_name, scenario, "builtin", profile.as_dict(),
                    started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    res.flows = [{"file": f.file, "name": f.flow["name"], "weight": f.weight,
                  "http_steps": sum(1 for s in f.flow["steps"] if s.get("call")),
                  "db_steps_skipped": sum(1 for s in f.flow["steps"] if s.get("db"))} for f in flows]
    if any(f["db_steps_skipped"] for f in res.flows):
        res.notes.append("Database steps are skipped under load; they still run in API functional tests.")
    col = Collector(res)
    limiter = RateLimiter(effective_rps(profile, perf.guardrails))
    weights = [f.weight for f in flows]
    deadline = time.perf_counter() + profile.duration_s
    iterations = [0]
    it_lock = threading.Lock()
    setup_errors: list[str] = []

    def vu(i: int) -> None:
        delay = profile.ramp_up_s * i / profile.vus
        if delay and stop.wait(delay):
            return
        runner = Runner(api_cfg, env_name, spec=None)
        rng = random.Random(1000 + i)
        bases: dict[int, Any] = {}
        try:
            while time.perf_counter() < deadline and not stop.is_set():
                idx = rng.choices(range(len(flows)), weights)[0]
                flow = flows[idx].flow
                auth = flow.get("auth", runner.env.get("auth", "default"))
                if idx not in bases:
                    v = Vars({"env": {"name": runner.env_name, "base_url": runner.base_url}})
                    pending: list = []
                    try:
                        v.values.update(v.sub(flow.get("vars") or {}))
                        bad = None
                        for fx in flow.get("fixtures") or []:
                            sr = runner._fixture(fx, v, auth, pending)
                            if sr.status != "passed":
                                bad = f"{flows[idx].file}: fixture {fx}: {sr.error or sr.status}"
                                break
                    except ValueError as e:
                        bad = f"{flows[idx].file}: {v.mask(str(e))}"
                    bases[idx] = None if bad else (v, pending)
                    if bad:
                        with it_lock:
                            if bad not in setup_errors:
                                setup_errors.append(bad)
                if bases[idx] is None:
                    if all(bases.get(k, 1) is None for k in range(len(flows))):
                        return
                    continue
                base, _ = bases[idx]
                v = Vars(copy.deepcopy(base.values))
                v.secrets = base.secrets
                for n, step in enumerate(flow["steps"], 1):
                    if not step.get("call") or stop.is_set():
                        continue
                    limiter.wait(stop)
                    sr = runner._step(step, v, auth, n)
                    ep = endpoint_key(step)
                    status = (sr.response or {}).get("status") if sr.response else None
                    if sr.ms is None and status is None:
                        col.add(ep, None, 0 if sr.status == "failed" else None, True, False, sr.error or sr.status)
                        break
                    ok = _status_ok(sr)
                    checks_bad = any(not a["ok"] for a in sr.assertions
                                     if not (a["check"].startswith("status") or a["check"] == "no server error"))
                    detail = "; ".join(f"{a['check']}: {a.get('detail', '')}" for a in sr.assertions if not a["ok"])
                    col.add(ep, float(sr.ms or 0), status, not ok, checks_bad, v.mask(detail))
                    if not ok or any(not a["ok"] and a["check"].startswith("save ") for a in sr.assertions):
                        break  # later steps depend on this one
                for k, step in enumerate(flow.get("cleanup") or [], 1):
                    runner._step(step, v, auth, k, cleanup=True)
                with it_lock:
                    iterations[0] += 1
                if profile.think_time_ms:
                    stop.wait(profile.think_time_ms / 1000 * rng.uniform(0.5, 1.5))
        finally:
            for item in bases.values():
                if item:
                    v, pending = item
                    for kind, spec in reversed(pending):
                        runner._step(spec, v, "default", 0, cleanup=True)
            runner.close()

    threads = [threading.Thread(target=vu, args=(i,), daemon=True) for i in range(profile.vus)]
    started = time.perf_counter()
    for t in threads:
        t.start()
    abort_rate = float(perf.guardrails.get("abort_error_rate") or 1.0)
    abort_min = int(perf.guardrails.get("abort_min_requests") or 50)
    while any(t.is_alive() for t in threads):
        time.sleep(0.1)
        if col.total >= abort_min and col.error_rate() > abort_rate and not stop.is_set():
            res.status = "aborted"
            res.reason = (f"error rate {col.error_rate():.0%} exceeded guardrails.abort_error_rate "
                          f"({abort_rate:.0%}) after {col.total} requests")
            stop.set()
        if time.perf_counter() > deadline + 30:
            stop.set()
    for t in threads:
        t.join(timeout=5)
    res.duration_s = round(time.perf_counter() - started, 2)
    res.iterations = iterations[0]
    if setup_errors:
        res.notes += [f"setup failed: {e}" for e in setup_errors[:5]]
        if col.total == 0:
            res.status, res.reason = "error", "No requests were sent: " + setup_errors[0]
    elif col.total == 0 and res.status == "completed":
        res.status, res.reason = "error", "No requests were sent"
    return res


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def rel(root: Path, p: Path) -> str:
    return p.relative_to(root).as_posix() if p.is_relative_to(root) else str(p)
