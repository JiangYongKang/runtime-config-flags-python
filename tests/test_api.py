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


# --------------------------------------------------------------------- #
# 变更留痕 / 历史版本 / 回滚 的 HTTP 接口
# --------------------------------------------------------------------- #
@pytest.fixture
def history_client(tmp_path):
    a = tmp_path / "a.json"
    a.write_text(
        json.dumps(
            {
                "config": {"timeout_ms": 1000, "max_connections": 100},
                "features": {"f": {"percentage": 10}},
            }
        ),
        encoding="utf-8",
    )
    b = tmp_path / "b.json"
    b.write_text(
        json.dumps(
            {
                "config": {"timeout_ms": 2000, "max_connections": 100},
                "features": {"f": {"percentage": 20}},
            }
        ),
        encoding="utf-8",
    )
    logger.info("[测试输入配置] a/b 两版本已就绪")
    svc = RuntimeConfigService([str(a)], history_limit=100)
    svc.bootstrap()
    svc.reload([str(b)])  # v2
    app = create_app(svc)
    with TestClient(app) as c:
        yield c, svc, a, b


def test_changes_endpoint_lists_accepted_and_rejected(history_client):
    c, _, bad_dir, _ = history_client
    bad = bad_dir.parent / "bad.json"
    bad.write_text(json.dumps({"config": {"timeout_ms": -1}}), encoding="utf-8")
    r = c.post("/reload", json={"sources": [str(bad)]})
    assert r.status_code == 422

    body = c.get("/changes").json()
    codes = [(ch["kind"], ch["outcome"], ch["error_code"]) for ch in body["changes"]]
    logger.info("[HTTP 留痕] %s", codes)
    assert ("bootstrap", "accepted", "") in codes
    assert ("reload", "accepted", "") in codes
    assert ("reload", "rejected", "value_out_of_range") in codes
    assert body["limit"] >= 1


def test_history_list_and_detail_endpoints(history_client):
    c, _, _, _ = history_client
    body = c.get("/history").json()
    assert body["versions"] == [1, 2]
    assert body["current_version"] == 2

    one = c.get("/history/1").json()["snapshot"]
    assert one["version"] == 1
    assert one["config"]["timeout_ms"] == 1000
    assert one["flags"]["f"]["percentage"] == 10
    logger.info("[HTTP 历史查询] v1=%s", one["config"])

    r = c.get("/history/999")
    assert r.status_code == 404
    assert r.json()["error_code"] == "history_version_not_found"


def test_rollback_endpoint_success_and_conflict(history_client):
    c, svc, _, _ = history_client
    r = c.post("/rollback", json={"target_version": 1})
    assert r.status_code == 200
    body = r.json()
    assert body["rolled_back_from"] == 1
    assert body["snapshot"]["version"] == 3  # 全新版本号
    assert body["snapshot"]["config"]["timeout_ms"] == 1000
    assert body["snapshot"]["flags"]["f"]["percentage"] == 10
    logger.info("[HTTP 回滚] 目标 v1 -> 全新 v3，当前 /health=%s", c.get("/health").json())

    # 过期 CAS 回滚 -> 409，状态不变
    r = c.post("/rollback", json={"target_version": 1, "expected_version": 1})
    assert r.status_code == 409
    assert r.json()["error_code"] == "version_conflict"
    assert c.get("/health").json()["version"] == 3
