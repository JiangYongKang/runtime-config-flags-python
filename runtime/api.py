"""HTTP 层：把配置中心暴露为可本地复现的接口。

端点：
  GET  /health                                 运行状态（是否初始化/当前版本）
  GET  /config                                当前生效配置全文（含 provenance）
  GET  /flags/{name}/evaluate?subject=...     开关判定（含依据）
  POST /admin/reload                          热更新（body: {"sources":[...]},
                                              可选 "expected_version"）
  GET  /admin/rejected                        最近被拒绝更新的留痕

错误响应统一结构：{"error": "...", "issues": [{code,path,message,source}]}，
原因码可区分、可追溯，HTTP 状态码：422 校验/合并冲突，409 版本冲突，
404 未知开关，503 尚未初始化。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from runtime.errors import ConfigRejected, NotInitializedError, ReasonCode
from runtime.model import FlagRule, MergedConfig
from runtime.store import ConfigStore

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "local.json"


def create_app(store: ConfigStore | None = None,
               *, config_path: str | Path | None = DEFAULT_CONFIG_PATH) -> FastAPI:
    store = store or ConfigStore()
    app = FastAPI(title="runtime-config-flags", version="0.1.0")
    app.state.store = store
    app.state.startup: dict[str, Any] = {"loaded": False, "issues": []}

    # ---- 启动时确定性加载本地配置；失败不静默：状态可从 /health 查看 ----
    if config_path is not None:
        path = Path(config_path)
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            store.load_sources(doc.get("sources", []))
            app.state.startup = {"loaded": True, "config_path": str(path),
                                 "issues": []}
        except FileNotFoundError:
            app.state.startup = {
                "loaded": False, "config_path": str(path),
                "issues": [{"code": ReasonCode.NOT_INITIALIZED.value,
                            "message": "本地配置文件不存在，服务以未初始化状态启动"}]}
        except ConfigRejected as exc:
            app.state.startup = {
                "loaded": False, "config_path": str(path),
                "issues": [_issue_dict(i) for i in exc.issues]}
        except json.JSONDecodeError as exc:
            app.state.startup = {
                "loaded": False, "config_path": str(path),
                "issues": [{"code": ReasonCode.NOT_A_MAPPING.value,
                            "message": f"配置文件不是合法 JSON：{exc}"}]}

    @app.exception_handler(ConfigRejected)
    async def _rejected_handler(_: Request, exc: ConfigRejected):
        codes = {i.code for i in exc.issues}
        if ReasonCode.VERSION_MISMATCH in codes:
            status = 409
        elif ReasonCode.UNKNOWN_FLAG in codes:
            status = 404
        else:
            status = 422
        return JSONResponse(
            status_code=status,
            content={"error": "配置被拒绝，当前生效状态未改变",
                     "issues": [_issue_dict(i) for i in exc.issues]})

    @app.exception_handler(NotInitializedError)
    async def _not_init_handler(_: Request, exc: NotInitializedError):
        return JSONResponse(
            status_code=503,
            content={"error": "配置尚未初始化",
                     "issues": [_issue_dict(exc.issue)]})

    @app.get("/health")
    def health():
        snap = store.current_version()
        return {
            "initialized": store.is_initialized(),
            "version": snap if store.is_initialized() else 0,
            "startup": app.state.startup,
        }

    @app.get("/config")
    def get_config():
        return _config_view(store.current())

    @app.get("/flags/{name}/evaluate")
    def evaluate_flag(name: str, subject: str):
        # 只取一次快照引用：整个请求（含判定依据中的 bucket/percentage）
        # 严格基于同一个版本
        snap = store.current()
        result = store.evaluate_snapshot(snap, name, subject)
        return {
            "flag": result.flag,
            "subject": result.subject,
            "on": result.on,
            "version": result.version,
            "reason": result.reason,
            "basis": {
                "bucket": result.bucket,
                "percentage": result.percentage,
                "detail": result.detail,
            },
        }

    @app.post("/admin/reload")
    async def reload(request: Request):
        body = await request.json()
        if not isinstance(body, dict) or not isinstance(
                body.get("sources"), list):
            return JSONResponse(
                status_code=422,
                content={"error": "请求体必须是包含 sources 数组的对象",
                         "issues": [{"code": ReasonCode.NOT_A_MAPPING.value,
                                     "path": "sources",
                                     "message": "sources 必须是数组",
                                     "source": None}]})
        expected = body.get("expected_version")
        if expected is None:
            merged = store.load_sources(body["sources"])
        else:
            merged = store.try_update(
                body["sources"], expected_version=int(expected))
        return {"ok": True, "version": merged.version,
                "config": _config_view(merged)}

    @app.get("/admin/rejected")
    def rejected():
        return {"rejected_updates": list(store.rejected_history())}

    return app


def _issue_dict(issue: Any) -> dict[str, Any]:
    return {"code": issue.code.value, "path": issue.path,
            "message": issue.message, "source": issue.source}


def _flag_view(rule: FlagRule) -> dict[str, Any]:
    return {
        "enabled": rule.enabled,
        "percentage": rule.percentage,
        "overrides": dict(sorted(rule.override_map().items())),
        "override_provenance": rule.provenance_map(),
    }


def _config_view(cfg: MergedConfig) -> dict[str, Any]:
    return {
        "version": cfg.version,
        "sources": list(cfg.sources),
        "settings": cfg.settings,
        "settings_provenance": cfg.settings_provenance,
        "flags": {name: _flag_view(rule)
                  for name, rule in sorted(cfg.flags.items())},
        "flags_provenance": cfg.flags_provenance,
        "created_at": cfg.created_at,
    }
