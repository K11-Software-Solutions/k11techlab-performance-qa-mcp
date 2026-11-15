"""Performance config: ``.qa-mcp/perf.yaml`` (environments and auth come from ``.qa-mcp/api-test.yaml``).

    environment: local                 # default environment for load runs
    engine: auto                       # auto (k6 if installed, else builtin) | k6 | builtin
    profile: { vus: 5, duration: 30s, ramp_up: 5s, think_time_ms: 100 }
    scenarios:
      checkout:
        flows:                         # API Functional flows reused as load scenarios
          - tests/api/order_total.yaml
          - { flow: tests/api/browse.yaml, weight: 3 }
        profile: { vus: 10 }           # optional overrides
    slos:                              # per endpoint ("METHOD /path/{param}"), glob patterns allowed
      "GET /orders/{id}": { p95_ms: 300, error_rate: 0.01 }
      "*": { p95_ms: 800, error_rate: 0.01 }
    regression: { metric: p95, tolerance_pct: 10, min_delta_ms: 10, min_samples: 30, p_value: 0.01 }
    guardrails: { max_vus: 50, max_duration: 10m, max_rps: 200, abort_error_rate: 0.5, abort_min_requests: 50 }

Load is refused on an environment unless it is on a local host, its name looks like a test
environment, or it sets ``allow_load: true`` in api-test.yaml. Environments named like production
also need ``production_load_acknowledged: true``. Data-changing calls additionally follow API
Functional QA's write rules.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml
from k11_api_qa.config import LOCAL_HOST, PROD_NAME, TEST_NAME

PERF_CONFIG = ".qa-mcp/perf.yaml"
METRICS = ("avg", "p50", "p90", "p95", "p99", "max")

DEFAULT_PROFILE = {"vus": 5, "duration": "30s", "ramp_up": "5s", "think_time_ms": 100, "max_rps": None}
DEFAULT_REGRESSION = {"metric": "p95", "tolerance_pct": 10.0, "min_delta_ms": 10.0, "min_samples": 30, "p_value": 0.01,
                      "error_rate_delta": 0.01}
DEFAULT_GUARDRAILS = {"max_vus": 50, "max_duration": "10m", "max_rps": None, "abort_error_rate": 0.5,
                      "abort_min_requests": 50}
_DUR = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h)?\s*$", re.I)


def seconds(value: Any) -> float:
    """'30s', '2m', '1h', '500ms' or a number of seconds."""
    if isinstance(value, (int, float)):
        return float(value)
    m = _DUR.match(str(value))
    if not m:
        raise ValueError(f"Invalid duration {value!r} (use e.g. 30s, 2m, 500ms)")
    n, unit = float(m.group(1)), (m.group(2) or "s").lower()
    return n * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]


@dataclass
class Profile:
    vus: int = 5
    duration_s: float = 30
    ramp_up_s: float = 5
    think_time_ms: float = 100
    max_rps: float | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "Profile":
        d = {**DEFAULT_PROFILE, **{k: v for k, v in (d or {}).items() if v is not None}}
        p = cls(vus=int(d["vus"]), duration_s=seconds(d["duration"]), ramp_up_s=seconds(d.get("ramp_up", 0)),
                think_time_ms=float(d.get("think_time_ms", 0)), max_rps=float(d["max_rps"]) if d.get("max_rps") else None)
        if p.vus < 1:
            raise ValueError("vus must be at least 1")
        if p.duration_s <= 0:
            raise ValueError("duration must be positive")
        p.ramp_up_s = min(max(p.ramp_up_s, 0), p.duration_s)
        return p

    def as_dict(self) -> dict:
        return {"vus": self.vus, "duration_s": self.duration_s, "ramp_up_s": self.ramp_up_s,
                "think_time_ms": self.think_time_ms, "max_rps": self.max_rps}


@dataclass
class PerfConfig:
    root: Path
    environment: str | None = None
    engine: str = "auto"
    profile: dict = field(default_factory=dict)
    scenarios: dict[str, dict] = field(default_factory=dict)
    slos: dict[str, dict] = field(default_factory=dict)
    regression: dict = field(default_factory=lambda: dict(DEFAULT_REGRESSION))
    guardrails: dict = field(default_factory=lambda: dict(DEFAULT_GUARDRAILS))
    exists: bool = False

    def scenario(self, name: str | None) -> tuple[str, dict]:
        if name and name in self.scenarios:
            return name, self.scenarios[name]
        if name and name != "default":
            raise ValueError(f"Unknown scenario '{name}'. Known: {sorted(self.scenarios) or ['(none: all flows in tests/api)']}")
        if "default" in self.scenarios:
            return "default", self.scenarios["default"]
        if len(self.scenarios) == 1:
            return next(iter(self.scenarios.items()))
        return "default", {}  # every flow in tests/api, equal weight

    def profile_for(self, scenario: dict, overrides: dict | None = None) -> Profile:
        return Profile.from_dict({**DEFAULT_PROFILE, **(self.profile or {}), **(scenario.get("profile") or {}),
                                  **{k: v for k, v in (overrides or {}).items() if v is not None}})


def load(root: Path) -> PerfConfig:
    path = Path(root) / PERF_CONFIG
    data: dict[str, Any] = {}
    if path.is_file():
        data = yaml.safe_load(path.read_text("utf8")) or {}
    unknown = set(data) - {"environment", "engine", "profile", "scenarios", "slos", "regression", "guardrails"}
    if unknown:
        raise ValueError(f"{PERF_CONFIG}: unknown key(s) {sorted(unknown)}")
    engine = str(data.get("engine", "auto"))
    if engine not in ("auto", "k6", "builtin"):
        raise ValueError(f"{PERF_CONFIG}: engine must be auto, k6 or builtin")
    reg = {**DEFAULT_REGRESSION, **(data.get("regression") or {})}
    if reg["metric"] not in METRICS:
        raise ValueError(f"{PERF_CONFIG}: regression.metric must be one of {list(METRICS)}")
    return PerfConfig(root=Path(root), environment=data.get("environment"), engine=engine,
                      profile=data.get("profile") or {}, scenarios=data.get("scenarios") or {},
                      slos=data.get("slos") or {}, regression=reg,
                      guardrails={**DEFAULT_GUARDRAILS, **(data.get("guardrails") or {})}, exists=path.is_file())


def load_allowed(env_name: str, env: dict, base_url: str) -> tuple[bool, str]:
    """Whether load may be generated against this environment, and why."""
    if PROD_NAME.search(env_name):
        if env.get("allow_load") and env.get("production_load_acknowledged"):
            return True, "production load explicitly acknowledged"
        return False, (f"'{env_name}' looks like production; load tests are refused "
                       "(set allow_load and production_load_acknowledged to override)")
    if "allow_load" in env:
        return bool(env["allow_load"]), ("allow_load: true" if env["allow_load"] else "allow_load: false")
    host = urlsplit(base_url).hostname or ""
    if LOCAL_HOST.match(host):
        return True, "local host"
    if TEST_NAME.search(env_name):
        return True, f"'{env_name}' is a test environment"
    return False, f"'{env_name}' is not marked as a test environment; set allow_load: true in api-test.yaml to permit load"


def check_guardrails(profile: Profile, guardrails: dict) -> list[str]:
    """Problems that stop the run before any request is sent."""
    out = []
    if profile.vus > int(guardrails.get("max_vus") or 50):
        out.append(f"{profile.vus} virtual users exceeds guardrails.max_vus ({guardrails.get('max_vus')})")
    max_d = seconds(guardrails.get("max_duration") or "10m")
    if profile.duration_s > max_d:
        out.append(f"duration {profile.duration_s:g}s exceeds guardrails.max_duration ({max_d:g}s)")
    cap = guardrails.get("max_rps")
    if cap and profile.max_rps and profile.max_rps > float(cap):
        out.append(f"max_rps {profile.max_rps:g} exceeds guardrails.max_rps ({cap})")
    return out


def effective_rps(profile: Profile, guardrails: dict) -> float | None:
    caps = [x for x in (profile.max_rps, guardrails.get("max_rps")) if x]
    return float(min(caps)) if caps else None


def init_text() -> str:
    return """# k11 Performance QA (https://github.com/K11-Software-Solutions/k11techlab-performance-qa-mcp)
# Environments and auth come from .qa-mcp/api-test.yaml (k11 API Functional QA).
environment: local
engine: auto                 # k6 if installed, else the builtin engine
profile: { vus: 5, duration: 30s, ramp_up: 5s, think_time_ms: 100 }

scenarios:
  default:
    flows: []                # e.g. [tests/api/browse.yaml, { flow: tests/api/checkout.yaml, weight: 2 }]
                             # empty: every flow in tests/api

slos:
  "*": { p95_ms: 800, error_rate: 0.01 }
  # "GET /orders/{id}": { p95_ms: 300, p99_ms: 800 }

regression: { metric: p95, tolerance_pct: 10, min_delta_ms: 10, min_samples: 30, p_value: 0.01 }
guardrails: { max_vus: 50, max_duration: 10m, abort_error_rate: 0.5 }
"""
