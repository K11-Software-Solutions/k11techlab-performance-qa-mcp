"""The k6 engine: the same flows, run by Grafana k6 for heavier load.

Python does what k6 can't: fixtures (database inserts) per virtual user and auth (including
OAuth). k6 does the HTTP: each VU repeats weighted flows, chains saved values, checks the expected
status, and tags every request with its endpoint. Results come back from k6's JSON output into the
same RunResult as the builtin engine, so reports and baselines don't depend on the engine.

Secrets never go into the generated script: auth headers reach k6 through an environment
variable, and ${ENV:NAME} references are read by k6 from the environment at run time.
Differences from the builtin engine: only status codes are checked (value checks run in API
functional tests), step-level auth overrides use the flow's auth, and database cleanup steps
don't run (use a disposable test database, or the builtin engine).
"""

from __future__ import annotations

import json
import re
import os
import shutil
import subprocess
import tempfile
import threading
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from k11_api_qa import auth as authmod
from k11_api_qa.config import Config as ApiConfig
from k11_api_qa.runner import Runner
from k11_api_qa.values import Vars

from .engine import MAX_EXAMPLES, RunResult, ScenarioFlow, endpoint_key
from .perfconfig import PerfConfig, Profile, effective_rps


def find_k6() -> str | None:
    p = os.environ.get("K6_PATH")
    if p and Path(p).is_file():
        return p
    return shutil.which("k6")


def _js_steps(steps: list[dict]) -> list[dict]:
    out = []
    for s in steps:
        if not s.get("call"):
            continue
        method, _, template = str(s["call"]).strip().partition(" ")
        exp = (s.get("expect") or {}).get("status")
        out.append({"key": endpoint_key(s), "method": method.upper(), "template": template.strip(),
                    "path": s.get("path") or {}, "query": s.get("query") or {}, "headers": s.get("headers") or {},
                    **({"body": s["body"]} if "body" in s else {}), **({"form": s["form"]} if "form" in s else {}),
                    "expect_status": exp, "save": s.get("save") or {}})
    return out


SCRIPT = r"""
import http from 'k6/http';
import { sleep } from 'k6';
import { Rate } from 'k6/metrics';

const FLOWS = __FLOWS__;
const BASE = __BASE__.replace(/\/+$/, '');
const THINK_MS = __THINK__;
const AUTH = JSON.parse(__ENV.K11_PERF_AUTH || '{}');
const FIX = __ENV.K11_PERF_FIXTURES ? JSON.parse(open(__ENV.K11_PERF_FIXTURES)) : [];
const failed = new Rate('k11_failed');
const TOTAL_W = FLOWS.reduce((a, f) => a + f.weight, 0);

export const options = __OPTIONS__;

function hex(n) { let s = ''; for (let i = 0; i < n; i++) s += Math.floor(Math.random() * 16).toString(16); return s; }
function uuid() { return `${hex(8)}-${hex(4)}-4${hex(3)}-${(8 + Math.floor(Math.random() * 4)).toString(16)}${hex(3)}-${hex(12)}`; }
function lookup(name, vars) {
  name = name.trim();
  if (name.startsWith('ENV:')) { const k = name.slice(4); if (!(k in __ENV)) throw new Error(`Environment variable ${k} is not set`); return __ENV[k]; }
  const gen = { uuid: uuid, now: () => new Date().toISOString().slice(0, 19) + '+00:00', today: () => new Date().toISOString().slice(0, 10),
    random_int: () => 1000 + Math.floor(Math.random() * 998999), random_email: () => `qa+${hex(10)}@example.com`,
    random_string: () => Array.from({ length: 10 }, () => 'abcdefghijklmnopqrstuvwxyz'[Math.floor(Math.random() * 26)]).join('') }[name];
  if (gen) return gen();
  let cur = vars;
  for (const part of name.split('.')) {
    if (cur !== null && typeof cur === 'object' && part in cur) cur = cur[part];
    else throw new Error(`Unknown variable \${${name}}`);
  }
  return cur;
}
function sub(v, vars) {
  if (typeof v === 'string') {
    const m = v.trim().match(/^\$\{([^}]+)\}$/);
    if (m) return lookup(m[1], vars);
    return v.replace(/\$\{([^}]+)\}/g, (_, n) => String(lookup(n, vars)));
  }
  if (Array.isArray(v)) return v.map((x) => sub(x, vars));
  if (v !== null && typeof v === 'object') { const o = {}; for (const [k, x] of Object.entries(v)) o[sub(k, vars)] = sub(x, vars); return o; }
  return v;
}
function jsonPath(doc, path) {
  if (path === '$' || path === '') return [true, doc];
  if (!path.startsWith('$')) path = '$.' + path;
  const re = /\.([A-Za-z_@$][\w@$-]*)|\[(\d+)\]|\['([^']*)'\]/y;
  let cur = doc, pos = 1;
  while (pos < path.length) {
    re.lastIndex = pos; const m = re.exec(path); if (!m) return [false, null]; pos = re.lastIndex;
    const key = m[1] !== undefined ? m[1] : (m[3] !== undefined ? m[3] : Number(m[2]));
    if (cur === null || typeof cur !== 'object' || !(key in cur)) return [false, null];
    cur = cur[key];
  }
  return [true, cur];
}
function statusMatches(status, exp) {
  if (Array.isArray(exp)) return exp.some((e) => statusMatches(status, e));
  if (exp !== null && typeof exp === 'object') {
    if ('not' in exp) return !statusMatches(status, exp.not);
    if ('in' in exp) return statusMatches(status, exp.in);
    return false;
  }
  const s = String(exp).toLowerCase();
  if (s.length === 3 && s.endsWith('xx')) return String(status)[0] === s[0];
  return status === Number(s);
}
function pick() { let r = Math.random() * TOTAL_W; for (let i = 0; i < FLOWS.length; i++) { r -= FLOWS[i].weight; if (r <= 0) return i; } return FLOWS.length - 1; }

export default function () {
  const fi = pick();
  const flow = FLOWS[fi];
  const fx = FIX.length ? (FIX[(__VU - 1) % FIX.length][fi] || {}) : {};
  if (FIX.length && FIX[(__VU - 1) % FIX.length][fi] === null) return;
  const vars = Object.assign({ env: { name: __ENV.K11_PERF_ENV || '', base_url: BASE } }, JSON.parse(JSON.stringify(fx)));
  for (const st of flow.steps) {
    let ok = false;
    let res = null;
    try {
      let path = sub(st.template, vars);
      for (const [k, v] of Object.entries(sub(st.path, vars))) path = path.split('{' + k + '}').join(encodeURIComponent(String(v)));
      let url = BASE + '/' + String(path).replace(/^\/+/, '');
      const q = sub(st.query, vars);
      const qs = Object.entries(q).map(([k, v]) => encodeURIComponent(k) + '=' + encodeURIComponent(String(v))).join('&');
      if (qs) url += (url.includes('?') ? '&' : '?') + qs;
      const headers = Object.assign({}, AUTH[String(fi)] || {}, sub(st.headers, vars));
      let body = null;
      if ('body' in st) { body = JSON.stringify(sub(st.body, vars)); headers['Content-Type'] = 'application/json'; }
      else if ('form' in st) { body = sub(st.form, vars); }
      res = http.request(st.method, url, body, { headers, tags: { name: st.key }, redirects: 0,
        responseCallback: http.expectedStatuses({ min: 100, max: 599 }) });
      const exp = st.expect_status;
      ok = res.status !== 0 && (exp !== null && exp !== undefined ? statusMatches(res.status, exp) : res.status < 400);
      if (res.status >= 500 && !(exp !== null && exp !== undefined && statusMatches(res.status, exp))) ok = false;
      if (ok) {
        for (const [k, src] of Object.entries(st.save)) {
          let found = false, val = null;
          if (src === 'status') { found = true; val = res.status; }
          else if (String(src).toLowerCase().startsWith('header:')) { val = res.headers[String(src).split(':')[1].trim()]; found = val !== undefined; }
          else { let doc = null; try { doc = res.json(); } catch (e) { doc = null; } [found, val] = jsonPath(doc, String(src)); }
          if (!found) { ok = false; break; }
          vars[k] = val;
        }
      }
    } catch (e) {
      ok = false;
    }
    failed.add(!ok, { name: st.key });
    if (!ok) break;
  }
  for (const st of flow.cleanup) {
    try {
      let path = sub(st.template, vars);
      for (const [k, v] of Object.entries(sub(st.path, vars))) path = path.split('{' + k + '}').join(encodeURIComponent(String(v)));
      http.request(st.method, BASE + '/' + String(path).replace(/^\/+/, ''), null,
        { headers: Object.assign({}, AUTH[String(fi)] || {}), tags: { name: 'cleanup' }, redirects: 0,
          responseCallback: http.expectedStatuses({ min: 100, max: 599 }) });
    } catch (e) { /* nothing to clean up */ }
  }
  if (THINK_MS > 0) sleep((THINK_MS / 1000) * (0.5 + Math.random()));
}
"""


def build_script(flows: list[ScenarioFlow], base_url: str, profile: Profile, perf: PerfConfig) -> str:
    js_flows = [{"file": f.file, "weight": f.weight, "steps": _js_steps(f.flow["steps"]),
                 "cleanup": _js_steps(f.flow.get("cleanup") or [])} for f in flows]
    hold = max(profile.duration_s - profile.ramp_up_s, 1)
    stages = ([{"duration": f"{profile.ramp_up_s:g}s", "target": profile.vus}] if profile.ramp_up_s else []) + \
             [{"duration": f"{hold:g}s", "target": profile.vus}]
    options: dict = {"scenarios": {"load": {"executor": "ramping-vus", "startVUs": 0 if profile.ramp_up_s else profile.vus,
                                            "stages": stages, "gracefulRampDown": "0s", "gracefulStop": "5s"}},
                     "thresholds": {"k11_failed": [{"threshold": f"rate<={float(perf.guardrails.get('abort_error_rate') or 1):g}",
                                                    "abortOnFail": True, "delayAbortEval": "5s"}]},
                     "summaryTrendStats": ["avg", "p(95)"], "noConnectionReuse": False}
    rps = effective_rps(profile, perf.guardrails)
    if rps:
        options["rps"] = int(rps)
    return (SCRIPT.replace("__FLOWS__", json.dumps(js_flows)).replace("__BASE__", json.dumps(base_url))
            .replace("__THINK__", json.dumps(profile.think_time_ms)).replace("__OPTIONS__", json.dumps(options)))


def _prepare_fixtures(api_cfg: ApiConfig, env_name: str, flows: list[ScenarioFlow], vus: int,
                      cleanups: list) -> tuple[list, list[str]]:
    if not any(f.flow.get("fixtures") or f.flow.get("vars") for f in flows):
        return [], []
    runner = Runner(api_cfg, env_name, spec=None)
    data, errors = [], []
    try:
        for _ in range(vus):
            row = []
            for f in flows:
                v = Vars({})
                pending: list = []
                auth = f.flow.get("auth", runner.env.get("auth", "default"))
                try:
                    v.values.update(v.sub(f.flow.get("vars") or {}))
                    ok = True
                    for fx in f.flow.get("fixtures") or []:
                        sr = runner._fixture(fx, v, auth, pending)
                        if sr.status != "passed":
                            errors.append(f"{f.file}: fixture {fx}: {sr.error or sr.status}")
                            ok = False
                            break
                except ValueError as e:
                    errors.append(f"{f.file}: {v.mask(str(e))}")
                    ok = False
                cleanups.append((v, pending))
                row.append(json.loads(json.dumps(v.values, default=str)) if ok else None)
            data.append(row)
    finally:
        runner.close()
    return data, sorted(set(errors))


def _auth_headers(api_cfg: ApiConfig, env: dict, flows: list[ScenarioFlow]) -> dict[str, dict]:
    out = {}
    for i, f in enumerate(flows):
        out[str(i)] = authmod.headers(f.flow.get("auth", env.get("auth", "default")), api_cfg.auth, Vars(), api_cfg.timeout_s)
    return out


def run_k6(api_cfg: ApiConfig, perf: PerfConfig, flows: list[ScenarioFlow], env_name: str, env: dict, base_url: str,
           profile: Profile, scenario: str, k6_path: str, stop: threading.Event | None = None) -> RunResult:
    res = RunResult("completed", env_name, scenario, "k6", profile.as_dict(),
                    started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    res.flows = [{"file": f.file, "name": f.flow["name"], "weight": f.weight,
                  "http_steps": sum(1 for s in f.flow["steps"] if s.get("call")),
                  "db_steps_skipped": sum(1 for s in f.flow["steps"] if s.get("db"))} for f in flows]
    res.notes.append("k6 engine: status codes are checked; value and database checks run in API functional tests.")
    if any(s.get("db") for f in flows for s in (f.flow.get("cleanup") or [])):
        res.notes.append("k6 engine: database cleanup steps don't run under k6; use a disposable test database.")
    cleanups: list = []
    work = Path(tempfile.mkdtemp(prefix="k11perf-"))
    try:
        fixtures, errors = _prepare_fixtures(api_cfg, env_name, flows, profile.vus, cleanups)
        if errors:
            res.notes += [f"setup failed: {e}" for e in errors[:5]]
        if fixtures and all(all(x is None for x in row) for row in fixtures):
            res.status, res.reason = "error", "No requests were sent: " + (errors[0] if errors else "fixtures failed")
            return res
        script = work / "scenario.js"
        script.write_text(build_script(flows, base_url, profile, perf), "utf8")
        env_vars = {**os.environ, "K11_PERF_AUTH": json.dumps(_auth_headers(api_cfg, env, flows)),
                    "K11_PERF_ENV": env_name, "K6_NO_USAGE_REPORT": "true"}
        if fixtures:
            (work / "fixtures.json").write_text(json.dumps(fixtures), "utf8")
            env_vars["K11_PERF_FIXTURES"] = str(work / "fixtures.json")
        raw = work / "raw.json"
        cmd = [k6_path, "run", "--quiet", "--out", f"json={raw}", str(script)]
        started = datetime.now(timezone.utc)
        proc = subprocess.Popen(cmd, cwd=work, env=env_vars, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", errors="replace")
        try:
            _, err = proc.communicate(timeout=profile.duration_s + 120)
        except subprocess.TimeoutExpired:
            proc.kill()
            _, err = proc.communicate()
            res.status, res.reason = "error", "k6 did not finish in time"
        res.duration_s = round((datetime.now(timezone.utc) - started).total_seconds(), 2)
        if proc.returncode == 99:
            res.status = "aborted"
            res.reason = (f"error rate exceeded guardrails.abort_error_rate "
                          f"({float(perf.guardrails.get('abort_error_rate') or 1):.0%})")
        elif proc.returncode not in (0, 99) and res.status != "error":
            tail = " ".join((err or "").strip().splitlines()[-3:])[:300]
            res.status, res.reason = "error", f"k6 exited with code {proc.returncode}: {tail}"
        if raw.is_file():
            _parse(raw, res)
        if res.status == "completed" and not res.samples and not res.errors:
            res.status, res.reason = "error", "k6 sent no requests"
        return res
    finally:
        if cleanups:
            runner = Runner(api_cfg, env_name, spec=None)
            try:
                for v, pending in cleanups:
                    for _, spec in reversed(pending):
                        runner._step(spec, v, "default", 0, cleanup=True)
            finally:
                runner.close()
        shutil.rmtree(work, ignore_errors=True)


_TS = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:?\d\d)?$")


def _ts(value) -> datetime | None:
    """k6 timestamps carry nanoseconds (and Python 3.10 accepts only 3 or 6 fraction digits)."""
    m = _TS.match(str(value or "").strip())
    if not m:
        return None
    tz = m.group(3) or "+00:00"
    tz = "+00:00" if tz == "Z" else (tz if ":" in tz else f"{tz[:3]}:{tz[3:]}")
    return datetime.fromisoformat(f"{m.group(1)}.{(m.group(2) or '0').ljust(6, '0')[:6]}{tz}")


def _parse(raw: Path, res: RunResult) -> None:
    t0 = None
    with raw.open(encoding="utf8") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("type") != "Point":
                continue
            d = rec.get("data") or {}
            tags = d.get("tags") or {}
            ep = tags.get("name")
            if not ep or ep == "cleanup":
                continue
            t = _ts(d.get("time"))
            if t and t0 is None:
                t0 = t
            sec = int((t - t0).total_seconds()) if (t and t0) else 0
            metric = rec.get("metric")
            if metric == "http_req_duration":
                status = int(tags.get("status") or 0)
                res.codes[ep][status] += 1
                if status:
                    res.samples[ep].append(float(d.get("value", 0)))
                if status >= 500:
                    res.server_errors[ep] += 1
                res.timeline[sec][0] += 1
            elif metric == "k11_failed" and d.get("value"):
                res.errors[ep] += 1
                res.timeline[sec][1] += 1
                if len(res.examples) < MAX_EXAMPLES:
                    res.examples.append({"endpoint": ep, "status": None, "error": "unexpected status or missing value"})
    for ep, c in res.codes.items():
        if not isinstance(c, Counter):
            res.codes[ep] = Counter(c)
