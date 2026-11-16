import random

import pytest

from k11_perf_qa import analyze, perfconfig
from k11_perf_qa.engine import RunResult, endpoint_key
from k11_perf_qa.k6engine import build_script
from k11_perf_qa.stats import mann_whitney_greater, percentile, summarize


def test_percentiles_match_linear_interpolation():
    v = list(range(1, 101))
    assert percentile(v, 50) == 50.5
    assert percentile(v, 95) == pytest.approx(95.05)
    assert percentile([7], 99) == 7
    s = summarize([10, 20, 30, 40])
    assert s["avg"] == 25 and s["min"] == 10 and s["max"] == 40


def test_significance_separates_noise_from_real_slowdowns():
    rng = random.Random(1)
    base = [rng.gauss(50, 8) for _ in range(300)]
    same = [rng.gauss(50, 8) for _ in range(300)]
    slower = [rng.gauss(60, 8) for _ in range(300)]
    assert mann_whitney_greater(base, same) > 0.01
    assert mann_whitney_greater(base, slower) < 1e-6
    assert mann_whitney_greater(slower, base) > 0.99  # one-sided: faster is not "slower"
    assert mann_whitney_greater([1], [2]) == 1.0
    assert mann_whitney_greater([5] * 50, [5] * 50) == 1.0  # all ties


def test_durations_and_profiles():
    assert perfconfig.seconds("30s") == 30 and perfconfig.seconds("2m") == 120 and perfconfig.seconds("500ms") == 0.5
    assert perfconfig.seconds(12) == 12
    with pytest.raises(ValueError):
        perfconfig.seconds("soon")
    p = perfconfig.Profile.from_dict({"vus": 3, "duration": "10s", "ramp_up": "1m"})
    assert p.ramp_up_s == 10  # ramp-up never longer than the test
    with pytest.raises(ValueError):
        perfconfig.Profile.from_dict({"vus": 0})


@pytest.mark.parametrize("name,env,url,ok", [
    ("local", {}, "http://127.0.0.1:8080", True),
    ("staging", {}, "https://api.example.com", True),
    ("perf", {}, "https://api.example.com", False),
    ("perf", {"allow_load": True}, "https://api.example.com", True),
    ("prod", {}, "http://127.0.0.1:8080", False),
    ("prod", {"allow_load": True}, "http://127.0.0.1:8080", False),
    ("production", {"allow_load": True, "production_load_acknowledged": True}, "https://api.example.com", True),
])
def test_load_is_refused_on_production_like_environments(name, env, url, ok):
    assert perfconfig.load_allowed(name, env, url)[0] is ok


def test_guardrails_cap_the_profile():
    g = {**perfconfig.DEFAULT_GUARDRAILS, "max_vus": 10, "max_duration": "1m", "max_rps": 50}
    p = perfconfig.Profile.from_dict({"vus": 20, "duration": "5m", "max_rps": 100})
    problems = perfconfig.check_guardrails(p, g)
    assert len(problems) == 3
    assert perfconfig.effective_rps(perfconfig.Profile.from_dict({"vus": 1}), g) == 50


def test_endpoint_keys_group_by_template():
    assert endpoint_key({"call": "GET /orders/{id}"}) == "GET /orders/{id}"
    assert endpoint_key({"call": "delete /orders/${orderId}"}) == "DELETE /orders/{orderId}"
    assert endpoint_key({"call": "GET /orders?customerId=${fixture.customer.id}"}) == "GET /orders"


def _result(samples: dict, errors: dict | None = None) -> RunResult:
    r = RunResult("completed", "local", "s", "builtin", {"vus": 1, "duration_s": 10})
    r.duration_s = 10
    for ep, v in samples.items():
        r.samples[ep] = list(v)
        r.codes[ep][200] = len(v)
    for ep, n in (errors or {}).items():
        r.errors[ep] = n
        r.server_errors[ep] = n
    return r


def test_slos_and_most_specific_pattern():
    r = _result({"GET /orders/{id}": [100] * 50, "GET /orders": [900] * 50})
    eps = analyze.endpoint_metrics(r)
    res = analyze.check_slos(eps, {"GET /orders/{id}": {"p95_ms": 50}, "GET /orders*": {"p95_ms": 1000}, "*": {"p95_ms": 500}})
    by = {(x["endpoint"], x["pattern"]): x["ok"] for x in res}
    assert by == {("GET /orders/{id}", "GET /orders/{id}"): False, ("GET /orders", "GET /orders*"): True}
    with pytest.raises(ValueError):
        analyze.check_slos(eps, {"*": {"p95": 10}})


def test_comparison_needs_size_delta_and_significance():
    rng = random.Random(3)
    base = {"samples": {"A": [rng.gauss(50, 5) for _ in range(200)], "B": [rng.gauss(50, 5) for _ in range(200)],
                        "C": [50.0] * 10}}
    base["endpoints"] = analyze.endpoint_metrics(_result(base["samples"]))
    cur = _result({"A": [rng.gauss(80, 5) for _ in range(200)], "B": [rng.gauss(51, 5) for _ in range(200)],
                   "C": [90.0] * 10, "D": [1.0] * 40})
    reg = dict(perfconfig.DEFAULT_REGRESSION)
    c = analyze.compare(cur, analyze.endpoint_metrics(cur), base, reg)
    status = {r["endpoint"]: r["status"] for r in c["endpoints"]}
    assert status == {"A": "regression", "B": "unchanged", "C": "insufficient_samples", "D": "new"}
    fs = analyze.findings(cur, analyze.endpoint_metrics(cur), [], c)
    assert [f["type"] for f in fs] == ["regression", "insufficient_samples"]
    assert analyze.verdict(fs) == "fail"


def test_server_errors_are_findings_even_without_slos():
    r = _result({"A": [10] * 20}, errors={"A": 3})
    eps = analyze.endpoint_metrics(r)
    assert eps[0]["error_rate"] == round(3 / 20, 4)
    fs = analyze.findings(r, eps, [], None)
    assert fs[0]["type"] == "server_errors" and analyze.verdict(fs) == "fail"


def test_k6_script_has_no_secrets_and_tags_endpoints():
    from k11_perf_qa.engine import ScenarioFlow

    flow = {"name": "x", "steps": [{"call": "GET /orders/{id}", "path": {"id": "${orderId}"},
                                    "headers": {"X-Key": "${ENV:SECRET_KEY}"}, "expect": {"status": 200}}]}
    perf = perfconfig.PerfConfig(root=None)
    js = build_script([ScenarioFlow("tests/api/x.yaml", flow)], "http://127.0.0.1:1", perfconfig.Profile.from_dict({}), perf)
    assert '"key": "GET /orders/{id}"' in js and "${ENV:SECRET_KEY}" in js  # resolved by k6 at run time
    assert "K11_PERF_AUTH" in js and "abortOnFail" in js


def test_k6_timestamps_with_nanoseconds():
    from k11_perf_qa.k6engine import _ts

    a, b = _ts("2026-11-16T13:19:27.59090141+00:00"), _ts("2026-11-16T13:19:28.590901Z")
    assert (b - a).total_seconds() == 1.0
    assert _ts("2026-11-16T13:19:29.1-0700").utcoffset().total_seconds() == -7 * 3600
    assert _ts("not a time") is None
