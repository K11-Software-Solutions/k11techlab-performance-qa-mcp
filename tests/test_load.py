"""Real load against the demo shop: baselines, regressions, errors, guardrails, CLI and MCP."""

import json

import anyio
import pytest
from conftest import set_perf

from k11_perf_qa.cli import main
from k11_perf_qa.k6engine import find_k6
from k11_perf_qa.run import run

FAST = {"duration": "4s", "ramp_up": "0s"}


def test_baseline_then_regression_is_caught(project, shop):
    first = run(project, "browse", None, FAST, "builtin", save_as_baseline=True)
    assert first["report"]["status"] == "completed" and first["baseline_saved"] == ".qa-mcp/perf-baseline/browse-local.json"
    assert first["report"]["summary"]["requests"] > 100 and first["report"]["summary"]["errors"] == 0
    shop.slow_ms = 80
    second = run(project, "browse", None, FAST, "builtin")
    doc = second["report"]
    regs = {r["endpoint"] for r in doc["comparison"]["regressions"]}
    assert "GET /orders/{id}" in regs
    assert doc["summary"]["verdict"] == "fail"
    assert any(f["type"] == "regression" and f["endpoint"] == "GET /orders/{id}" for f in doc["findings"])
    md = (project / second["paths"]["markdown"]).read_text("utf8")
    assert "🔺" in md and "GET /orders/{id}" in md


def test_no_change_is_not_a_regression(project, shop):
    # this test is about regressions only: SLOs are loose so a busy machine can't fail it
    set_perf(project, regression={"tolerance_pct": 100, "min_delta_ms": 100},
             slos={"*": {"p95_ms": 10000, "error_rate": 0.5}})
    run(project, "browse", None, FAST, "builtin", save_as_baseline=True)
    doc = run(project, "browse", None, FAST, "builtin")["report"]
    assert doc["comparison"]["regressions"] == [], doc["findings"]
    assert doc["summary"]["verdict"] == "pass", doc["findings"]


def test_server_errors_and_slo_breach_under_load(project, shop):
    shop.error_every = 5
    doc = run(project, "browse", None, FAST, "builtin", compare_baseline=False)["report"]
    types = {(f["type"], f.get("endpoint")) for f in doc["findings"]}
    assert ("server_errors", "GET /orders/{id}") in types
    assert ("slo_breach", "GET /orders/{id}") in types  # error_rate SLO 1%
    ep = next(e for e in doc["endpoints"] if e["endpoint"] == "GET /orders/{id}")
    assert ep["server_errors"] > 0 and ep["status_codes"].get("500")
    assert doc["error_examples"] and doc["error_examples"][0]["endpoint"] == "GET /orders/{id}"


def test_abort_when_most_requests_fail(project, shop):
    set_perf(project, guardrails={"abort_error_rate": 0.2, "abort_min_requests": 20})
    shop.error_every = 1
    doc = run(project, "browse", None, {"duration": "20s", "ramp_up": "0s"}, "builtin")["report"]
    assert doc["status"] == "aborted" and doc["duration_s"] < 15
    assert doc["findings"][0]["type"] == "aborted"


def test_production_is_refused_before_any_request(project, shop):
    before = shop._gets
    doc = run(project, "browse", "prod", FAST, "builtin")["report"]
    assert doc["status"] == "blocked" and "looks like production" in doc["reason"]
    assert doc["summary"]["requests"] == 0 and shop._gets == before


def test_guardrails_and_read_only_environments(project, shop):
    doc = run(project, "browse", None, {"vus": 500}, "builtin")["report"]
    assert doc["status"] == "blocked" and "max_vus" in doc["reason"]
    api = project / ".qa-mcp" / "api-test.yaml"
    api.write_text(api.read_text("utf8").replace("  prod:", "  perf:\n    base_url: \"${ENV:SHOP_URL}\"\n"
                                                 "    allow_load: true\n    allow_writes: false\n  prod:"), "utf8")
    doc = run(project, "browse", "perf", FAST, "builtin")["report"]
    assert doc["status"] == "blocked" and "Data-changing calls are refused" in doc["reason"]


def test_cli_run_baseline_and_fail_on(project, shop, capsys):
    assert main(["baseline", "--scenario", "browse", "--duration", "3s", "--ramp-up", "0s", "--engine", "builtin"]) == 0
    assert (project / ".qa-mcp/perf-baseline/browse-local.json").is_file()
    shop.slow_ms = 80
    args = ["run", "--scenario", "browse", "--duration", "3s", "--ramp-up", "0s", "--engine", "builtin"]
    assert main(args) == 1
    assert main(args + ["--fail-on", "slo"]) == 0  # slower, but within the SLOs
    assert main(args + ["--fail-on", "never"]) == 0
    out = capsys.readouterr().out
    assert "regression: GET /orders/{id}" in out
    assert main(["baseline", "--from-last-run"]) == 0


def test_mcp_tools(project, shop):
    from mcp.shared.memory import create_connected_server_and_client_session

    from k11_perf_qa.server import create_server

    async def go():
        async with create_connected_server_and_client_session(create_server()._mcp_server) as c:
            tools = {t.name for t in (await c.list_tools()).tools}
            assert tools == {"perf_config", "run_load_test", "save_baseline", "get_report", "init_perf_config"}
            cfg = json.loads((await c.call_tool("perf_config", {})).content[0].text)
            assert cfg["environments"]["prod"]["load_allowed"] is False
            assert [f["file"] for f in cfg["scenarios"]["browse"]["flows"]] == ["tests/api/view_order.yaml",
                                                                                 "tests/api/cancel_order.yaml"]
            r = json.loads((await c.call_tool("run_load_test", {"scenario": "browse", "duration": "3s", "ramp_up": "0s",
                                                                 "engine": "builtin"})).content[0].text)
            assert r["status"] == "completed" and r["paths"]["json"] == ".qa-mcp/perf-report/perf-report.json"
            saved = json.loads((await c.call_tool("save_baseline", {})).content[0].text)
            assert saved["saved"] == ".qa-mcp/perf-baseline/browse-local.json"
            one = json.loads((await c.call_tool("get_report", {"endpoint": "GET /orders/{id}"})).content[0].text)
            assert one["endpoint"]["requests"] > 0
            bad = await c.call_tool("init_perf_config", {})
            assert bad.isError  # perf.yaml exists already

    anyio.run(go)


@pytest.mark.skipif(not find_k6(), reason="k6 not installed")
def test_k6_engine_matches_builtin_findings(project, shop):
    first = run(project, "browse", None, FAST, "k6", save_as_baseline=True)
    assert first["report"]["engine"] == "k6" and first["report"]["status"] == "completed", first["report"]["reason"]
    assert first["report"]["summary"]["requests"] > 50 and first["report"]["summary"]["errors"] == 0
    shop.slow_ms = 80
    doc = run(project, "browse", None, FAST, "k6")["report"]
    assert "GET /orders/{id}" in {r["endpoint"] for r in doc["comparison"]["regressions"]}
    shop.slow_ms, shop.error_every = 0, 5
    doc = run(project, "browse", None, FAST, "k6", compare_baseline=False)["report"]
    assert any(f["type"] == "server_errors" for f in doc["findings"])
