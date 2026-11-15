import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHOP = ROOT / "demo" / "shop"
sys.path.insert(0, str(SHOP))

from server import Shop  # noqa: E402


@pytest.fixture
def project(tmp_path):
    """A copy of the demo shop project: flows, api-test.yaml and perf.yaml."""
    dest = tmp_path / "shop"
    shutil.copytree(SHOP, dest, ignore=shutil.ignore_patterns("__pycache__", "server.py", "*.db"))
    return dest


@pytest.fixture
def shop(project, monkeypatch):
    s = Shop(project / "shop.db", 0)
    monkeypatch.setenv("SHOP_URL", s.url)
    monkeypatch.setenv("SHOP_TOKEN", "test-token")
    monkeypatch.setenv("QA_MCP_ROOT", str(project))
    yield s
    s.close()


def set_perf(project: Path, **changes) -> None:
    """Edit the demo's perf.yaml (top-level keys)."""
    import yaml

    p = project / ".qa-mcp" / "perf.yaml"
    data = yaml.safe_load(p.read_text("utf8"))
    for k, v in changes.items():
        if isinstance(v, dict) and isinstance(data.get(k), dict):
            data[k] = {**data[k], **v}
        else:
            data[k] = v
    p.write_text(yaml.safe_dump(data, sort_keys=False), "utf8")
