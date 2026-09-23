"""API 集成：状态码可区分、错误结构稳定、热更新与未初始化行为确定。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from runtime.api import create_app
from runtime.store import ConfigStore

from .conftest import log_case


def good_raw(source_id="svc", priority=10, **over):
    raw = {"source_id": source_id, "priority": priority, "settings": {
        "max_connections": 10, "request_timeout_ms": 100,
        "sample_rate": 0.2, "log_level": "INFO"},
        "flags": {"rollout": {"enabled": True, "percentage": 100,
                              "overrides": {}}}}
    raw["settings"].update(over.pop("settings", {}))
    raw.update(over)
    return raw


@pytest.fixture
def client():
    store = ConfigStore()
    store.load_sources([good_raw()])
    return TestClient(create_app(store, config_path=None))


def test_health_and_config(client):
    h = client.get("/health").json()
    assert h["initialized"] is True and h["version"] == 1
    cfg = client.get("/config").json()
    assert cfg["settings"]["max_connections"] == 10
    assert "rollout" in cfg["flags"]
    log_case("GET /health + /config", raws=[good_raw()], version=1,
             result={"health": h, "settings": cfg["settings"]})


def test_evaluate_endpoint_returns_basis(client):
    r = client.get("/flags/rollout/evaluate", params={"subject": "alice"})
    assert r.status_code == 200
    body = r.json()
    assert body["on"] is True
    assert body["reason"] == "percentage_full"
    assert body["basis"]["bucket"] == body["basis"]["bucket"]
    assert body["version"] == 1
    log_case("GET 开关判定（含依据）", raws=[good_raw()], version=1,
             result=body)


def test_unknown_flag_404(client):
    r = client.get("/flags/nope/evaluate", params={"subject": "a"})
    assert r.status_code == 404
    assert r.json()["issues"][0]["code"] == "UNKNOWN_FLAG"
    log_case("未知开关 -> 404 可区分",
             error=r.json()["issues"][0]["code"])


def test_invalid_reload_422_and_rollback(client):
    before = client.get("/config").json()
    r = client.post("/admin/reload", json={"sources": [
        {"source_id": "bad", "priority": 1,
         "settings": {"max_connections": 0}}]})
    assert r.status_code == 422
    codes = [i["code"] for i in r.json()["issues"]]
    assert "SETTING_OUT_OF_RANGE" in codes
    after = client.get("/config").json()
    assert after["version"] == before["version"] == 1
    assert after["settings"] == before["settings"]
    log_case("POST 无效热更新 -> 422 且回滚",
             raws={"sources": [{"max_connections": 0}]},
             version=before["version"], error=codes)


def test_valid_reload_publishes_new_version(client):
    r = client.post("/admin/reload", json={"sources": [good_raw(
        settings={"log_level": "DEBUG"})]})
    assert r.status_code == 200 and r.json()["version"] == 2
    assert r.json()["config"]["settings"]["log_level"] == "DEBUG"
    log_case("POST 有效热更新 -> 新版本原子可见", version=2,
             result={"log_level": "DEBUG"})


def test_conditional_reload_conflict_409(client):
    body = {"sources": [good_raw()], "expected_version": 99}
    r = client.post("/admin/reload", json=body)
    assert r.status_code == 409
    assert r.json()["issues"][0]["code"] == "VERSION_MISMATCH"
    assert client.get("/health").json()["version"] == 1
    log_case("POST 基于过期版本更新 -> 409", version=1,
             error=r.json()["issues"][0]["code"])


def test_malformed_body_422(client):
    r = client.post("/admin/reload", json={"nope": []})
    assert r.status_code == 422
    assert r.json()["issues"][0]["code"] == "NOT_A_MAPPING"
    log_case("POST 非法请求体 -> 422",
             error=r.json()["issues"][0]["code"])


def test_rejected_history_endpoint(client):
    client.post("/admin/reload", json={"sources": [
        {"source_id": "b", "priority": 1,
         "settings": {"sample_rate": 5}}]})
    hist = client.get("/admin/rejected").json()["rejected_updates"]
    assert hist and hist[-1]["stayed_on_version"] == 1
    assert any(i["code"] == "SETTING_OUT_OF_RANGE"
               for i in hist[-1]["issues"])
    log_case("GET 拒绝留痕（失败原因可追溯）",
             error=hist[-1]["issues"])


def test_uninitialized_app_returns_503():
    app = create_app(ConfigStore(), config_path=None)
    c = TestClient(app)
    assert c.get("/health").json()["initialized"] is False
    assert c.get("/config").status_code == 503
    r = c.get("/flags/rollout/evaluate", params={"subject": "a"})
    assert r.status_code == 503
    assert r.json()["issues"][0]["code"] == "NOT_INITIALIZED"
    log_case("未初始化状态下读取 -> 503 且行为确定",
             error=r.json()["issues"][0]["code"])


def test_startup_with_invalid_config_file_is_explained(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"sources": [
        {"source_id": "x", "priority": 1,
         "settings": {"max_connections": 10_000_000}}]}), encoding="utf-8")
    app = create_app(config_path=bad)
    c = TestClient(app)
    h = c.get("/health").json()
    assert h["initialized"] is False
    assert h["startup"]["loaded"] is False
    assert h["startup"]["issues"][0]["code"] == "SETTING_OUT_OF_RANGE"
    log_case("启动时配置非法 -> 不静默，health 暴露拒绝原因",
             raws=str(bad), error=h["startup"]["issues"])


def test_startup_with_missing_file_is_explained(tmp_path):
    app = create_app(config_path=tmp_path / "missing.json")
    c = TestClient(app)
    h = c.get("/health").json()
    assert h["initialized"] is False
    assert h["startup"]["issues"][0]["code"] == "NOT_INITIALIZED"
    log_case("启动时配置缺失 -> 未初始化状态可解释",
             error=h["startup"]["issues"][0])
