"""JMeter results (JTL) imported into the same analysis: SLOs, baselines, regressions, Release Gate evidence."""

import json
import random

import anyio
import pytest

from k11_perf_qa.cli import main
from k11_perf_qa.jmeter import JtlError, load_jtl
from k11_perf_qa.run import import_results

HEADER = ("timeStamp,elapsed,label,responseCode,responseMessage,threadName,dataType,success,failureMessage,bytes,"
          "sentBytes,grpThreads,allThreads,URL,Latency,IdleTime,Connect")


def write_csv(path, means: dict[str, float], n=200, seed=1, error_every=0, extra=()):
    """A JTL in JMeter's default CSV format: one row per sample, 4 threads, 20 s."""
    rng = random.Random(seed)
    t0 = 1_763_300_000_000
    lines = [HEADER]
    i = 0
    for k in range(n):
        for label, mean in means.items():
            i += 1
            ms = max(1, int(rng.gauss(mean, mean * 0.15)))
            bad = error_every and i % error_every == 0
            rc, msg, ok = ("500", "Internal Server Error", "false") if bad else ("200", "OK", "true")
            lines.append(f"{t0 + k * 100},{ms},{label},{rc},{msg},Users 1-{k % 4 + 1},text,{ok},,512,180,4,4,"
                         f"http://shop.test/x,{ms - 1},0,2")
    lines.append(f"{t0 + 5000},900,Checkout,200,\"Number of samples in transaction : 3, number of failing samples : 0\","
                 f"Users 1-1,,true,,1536,540,4,4,null,850,0,6")  # Transaction Controller: skipped
    lines.append(f"{t0 + 6000},21000,GET /orders/{{id}},Non HTTP response code: java.net.SocketTimeoutException,"
                 f"\"Non HTTP response message: Read timed out\",Users 1-2,text,false,,0,0,4,4,http://shop.test/x,0,0,2")
    lines += list(extra)
    path.write_text("\n".join(lines) + "\n", "utf8")
    return path


def test_csv_jtl_becomes_per_endpoint_samples(tmp_path):
    f = write_csv(tmp_path / "results.jtl", {"GET /orders/{id}": 40, "POST /orders": 120}, n=50, error_every=25)
    r = load_jtl(f, "jmeter", "staging")
    assert r.engine == "jmeter" and r.profile["vus"] == 4 and r.duration_s == 27.0  # the 21 s timeout ends last
    assert "Checkout" not in r.samples                      # transaction controller skipped
    assert r.codes["GET /orders/{id}"][0] == 1               # the socket timeout: an error, not a latency sample
    assert len(r.samples["GET /orders/{id}"]) + len(r.samples["POST /orders"]) == 100
    assert sum(r.server_errors.values()) == 4 and sum(r.errors.values()) == 5
    assert any("Read timed out" in e["error"] or e["status"] == 500 for e in r.examples)


def test_xml_jtl_with_transactions_and_embedded_resources(tmp_path):
    f = tmp_path / "results.xml"
    f.write_text("""<?xml version="1.0" encoding="UTF-8"?>
<testResults version="1.2">
<httpSample t="40" lt="38" ts="1763300000000" s="true" lb="GET /orders/{id}" rc="200" rm="OK" tn="T 1-1" na="2"/>
<httpSample t="55" lt="50" ts="1763300000100" s="false" lb="GET /orders/{id}" rc="503" rm="Unavailable" tn="T 1-2" na="2">
  <assertionResult><name>status</name><failure>true</failure><failureMessage>expected 200</failureMessage></assertionResult>
</httpSample>
<sample t="300" ts="1763300000200" s="true" lb="Checkout" rm="Number of samples in transaction : 2, number of failing samples : 0" tn="T 1-1" na="2">
  <httpSample t="120" ts="1763300000200" s="true" lb="POST /orders" rc="201" rm="Created" tn="T 1-1" na="2">
    <httpSample t="5" ts="1763300000210" s="true" lb="http://shop.test/logo.png" rc="200" rm="OK" tn="T 1-1" na="2"/>
  </httpSample>
  <httpSample t="80" ts="1763300000330" s="true" lb="GET /orders" rc="200" rm="OK" tn="T 1-1" na="2"/>
</sample>
</testResults>""", "utf8")
    r = load_jtl(f, "jmeter", "qa")
    assert sorted(r.samples) == ["GET /orders", "GET /orders/{id}", "POST /orders"]  # children, not the embedded image
    assert r.server_errors["GET /orders/{id}"] == 1 and r.examples[0]["error"] == "expected 200"


def test_bad_files_are_explained(tmp_path):
    (tmp_path / "no-header.jtl").write_text("1763300000000,40,GET /x,200,OK,T,text,true\n", "utf8")
    with pytest.raises(JtlError, match="header row"):
        load_jtl(tmp_path / "no-header.jtl", "s", "e")
    (tmp_path / "other.xml").write_text("<html/>", "utf8")
    with pytest.raises(JtlError, match="testResults"):
        load_jtl(tmp_path / "other.xml", "s", "e")
    with pytest.raises(JtlError, match="not found"):
        load_jtl(tmp_path / "missing.jtl", "s", "e")


def test_import_baseline_then_regression_and_slos(project):
    write_csv(project / "jmeter-main.jtl", {"GET /orders/{id}": 40, "POST /orders": 120}, seed=1)
    first = import_results(project, "jmeter-main.jtl", environment="staging", save_as_baseline=True)
    assert first["baseline_saved"] == ".qa-mcp/perf-baseline/jmeter-staging.json"
    write_csv(project / "jmeter-pr.jtl", {"GET /orders/{id}": 90, "POST /orders": 121}, seed=2)
    doc = import_results(project, "jmeter-pr.jtl", environment="staging")["report"]
    assert doc["engine"] == "jmeter" and doc["status"] == "completed"
    assert [r["endpoint"] for r in doc["comparison"]["regressions"]] == ["GET /orders/{id}"]
    # the demo's perf.yaml SLO for GET /orders/{id} is p95 <= 250 ms: still met at ~90 ms
    assert all(s["ok"] for s in doc["slos"])
    md = (project / ".qa-mcp/perf-report/perf-report.md").read_text("utf8")
    assert "imported from JMeter results `jmeter-pr.jtl`" in md and "🔺" in md
    # the report is Release Gate evidence like any other run
    from_gate = json.loads((project / ".qa-mcp/perf-report/perf-report.json").read_text("utf8"))
    assert from_gate["tool"].startswith("k11-performance-qa-mcp") and "findings" in from_gate


def test_import_cli_and_paths_stay_inside_the_project(project, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("QA_MCP_ROOT", str(project))
    write_csv(project / "r.jtl", {"GET /orders/{id}": 40}, error_every=10)
    assert main(["import", "r.jtl", "--env", "staging"]) == 1          # 5xx in the results
    assert "server_errors: GET /orders/{id}" in capsys.readouterr().out
    assert main(["import", "r.jtl", "--env", "staging", "--fail-on", "never"]) == 0
    outside = write_csv(tmp_path / "outside.jtl", {"GET /x": 10})
    assert main(["import", str(outside)]) == 1
    assert "import failed" in capsys.readouterr().err


def test_mcp_import_tool(project, monkeypatch):
    from mcp.shared.memory import create_connected_server_and_client_session

    from k11_perf_qa.server import create_server

    monkeypatch.setenv("QA_MCP_ROOT", str(project))
    write_csv(project / "r.jtl", {"GET /orders/{id}": 40})

    async def go():
        async with create_connected_server_and_client_session(create_server()._mcp_server) as c:
            r = json.loads((await c.call_tool("import_jmeter_results", {"path": "r.jtl", "environment": "staging",
                                                                         "save_as_baseline": True})).content[0].text)
            assert r["engine"] == "jmeter" and r["baseline_saved"] == ".qa-mcp/perf-baseline/jmeter-staging.json"
            assert (await c.call_tool("import_jmeter_results", {"path": "../../etc/passwd"})).isError

    anyio.run(go)
