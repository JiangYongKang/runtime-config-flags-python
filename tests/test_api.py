"""HTTP 端到端测试：配置查看、开关判定、热更新、结构化错误。"""

from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.service import RuntimeConfigService

logger = logging.getLogger("runtime_config.tests")


@pytest.fixture
def client(tmp_path):
    base = tmp_path / "base.json"
    base.write_text(
        json.dumps(
            {
                "config": {"timeout_ms": 1000, "max_connections": 100},
                "features": {
                    "new_ui": {"default_on": False, "percentage": 0,
                               "overrides": {"tester": True}},
                    "full": {"default_on": False, "percentage": 100},
                },
            }
        ),
        encoding="utf-8",
    )
    logger.info("[测试输入配置] %s", json.loads(base.read_text()))
    svc = RuntimeConfigService([str(base)])
    svc.bootstrap()
    app = create_app(svc)
    with TestClient(app) as c:
        yield c, svc, base


def test_health_and_config(client):
    c, _, _ = client
    assert c.get("/health").json() == {"status": "ok", "version": 1}
    cfg = c.get("/config").json()
    assert cfg["version"] == 1
    assert cfg["config"]["timeout_ms"] == 1000


def test_get_single_value(client):
    c, _, _ = client
    r = c.get("/config/timeout_ms")
    assert r.status_code == 200 and r.json()["value"] == 1000


def test_unknown_field_and_flag_are_distinguished(client):
    c, _, _ = client
    r1 = c.get("/config/nope")
    r2 = c.get("/flags/nope", params={"subject": "u"})
    assert r1.status_code == 422 and r1.json()["error_code"] == "unknown_field"
    r2 = c.get("/flags/nope", params={"subject": "u"})
    assert r2.status_code == 422 and r2.json()["error_code"] == "unknown_flag"
    logger.info("[错误码可区分] %s vs %s", r1.json()["error_code"], r2.json()["error_code"])


def test_flag_override_and_full_rollout(client):
    c, _, _ = client
    r = c.get("/flags/new_ui", params={"subject": "tester"})
    body = r.json()
    assert body["on"] is True and body["reason"] == "override_on"
    logger.info("[判定依据] %s", body)
    assert c.get("/flags/full", params={"subject": "anyone"}).json()["reason"] == "gray_hit"


def test_reload_invalid_returns_422_and_keeps_version(client):
    c, svc, base = client
    bad = base.parent / "bad.json"
    bad.write_text(json.dumps({"config": {"timeout_ms": -7}}), encoding="utf-8")
    r = c.post("/reload", json={"sources": [str(bad)]})
    assert r.status_code == 422
    assert r.json()["error_code"] == "value_out_of_range"
    assert c.get("/health").json()["version"] == 1
    assert c.get("/config/timeout_ms").json()["value"] == 1000


def test_reload_success_and_cas_conflict(client, tmp_path):
    c, _, base = client
    r = c.post("/reload", json={"sources": [str(base)], "expected_version": 1})
    assert r.status_code == 200 and r.json()["snapshot"]["version"] == 2
    r = c.post("/reload", json={"sources": [str(base)], "expected_version": 1})
    assert r.status_code == 409 and r.json()["error_code"] == "version_conflict"


def test_no_snapshot_returns_503(tmp_path):
    svc = RuntimeConfigService([str(tmp_path / "missing.json")], strict=False)
    svc.bootstrap()
    with TestClient(create_app(svc)) as c:
        assert c.get("/health").json() == {"status": "degraded", "version": None}
        r = c.get("/config")
        assert r.status_code == 503 and r.json()["error_code"] == "no_snapshot"
        r = c.get("/flags/f", params={"subject": "u"})
        assert r.status_code == 503 and r.json()["error_code"] == "no_snapshot"
