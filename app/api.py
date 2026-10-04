"""HTTP 接口（FastAPI）。

端点：
  GET  /health                       存活与当前版本
  GET  /config                       当前完整配置（含版本与合并依据）
  GET  /config/{key}                 单个配置字段
  GET  /flags                        当前开关清单
  GET  /flags/{flag}?subject=...      开关判定（返回完整依据）
  POST /reload                       热更新（可选 sources / expected_version）
  GET  /changes                      变更留痕（成功/被拒，原因可区分，有界）
  GET  /history                      仍在保留范围内的历史版本号列表
  GET  /history/{version}            某个历史版本的完整配置与开关
  POST /rollback                     一键回滚（target_version，可选 expected_version）

错误统一转换为带 ``error_code`` 的 JSON：
  NO_SNAPSHOT -> 503；VERSION_CONFLICT -> 409；
  HISTORY_VERSION_NOT_FOUND -> 404；其余配置类错误 -> 422。
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.errors import ConfigError, ErrorCode
from app.service import RuntimeConfigService

_STATUS_BY_CODE = {
    ErrorCode.NO_SNAPSHOT: 503,
    ErrorCode.VERSION_CONFLICT: 409,
    ErrorCode.HISTORY_VERSION_NOT_FOUND: 404,
}


class ReloadRequest(BaseModel):
    """/reload 请求体（字段均可选）。"""

    sources: list[str] | None = None
    expected_version: int | None = None


class RollbackRequest(BaseModel):
    """/rollback 请求体。"""

    target_version: int
    expected_version: int | None = None


def create_app(service: RuntimeConfigService | None = None) -> FastAPI:
    """构造 FastAPI 应用并注入服务实例。"""
    app = FastAPI(
        title="runtime-config-flags",
        version="0.10.0",
        description="运行时配置与特性开关服务（多源合并 / 原子热更新 / 灰度判定 / 变更留痕 / 历史回滚）",
    )
    app.state.service = service

    @app.exception_handler(ConfigError)
    async def _handle_config_error(request: Request, exc: ConfigError) -> JSONResponse:
        return config_error_handler(request, exc)

    @app.get("/health")
    def health() -> dict:
        svc = app.state.service
        snap = _try_snapshot(svc) if svc is not None else None
        if snap is None:
            return {"status": "degraded", "version": None}
        return {"status": "ok", "version": snap.version}

    @app.get("/config")
    def get_config() -> dict:
        return app.state.service.snapshot().to_public_dict()

    @app.get("/config/{key}")
    def get_value(key: str) -> dict:
        svc = app.state.service
        return {"version": svc.version(), "key": key, "value": svc.get(key)}

    @app.get("/flags")
    def list_flags() -> dict:
        snap = app.state.service.snapshot()
        return {"version": snap.version, "flags": snap.to_public_dict()["flags"]}

    @app.get("/flags/{flag}")
    def check_flag(flag: str, subject: str) -> dict:
        return app.state.service.evaluate_flag(flag, subject).to_dict()

    @app.post("/reload")
    def reload(body: ReloadRequest) -> dict:
        snap = app.state.service.reload(
            body.sources, expected_version=body.expected_version
        )
        return {"status": "reloaded", "snapshot": snap.to_public_dict()}

    # ------------------------------------------------------------------ #
    # 变更留痕 / 历史版本 / 回滚
    # ------------------------------------------------------------------ #
    @app.get("/changes")
    def list_changes() -> dict:
        svc = app.state.service
        entries = svc.list_changes()
        return {
            "limit": svc.audit_limit,
            "total": len(entries),
            "changes": [e.to_dict() for e in entries],
        }

    @app.get("/history")
    def list_history() -> dict:
        svc = app.state.service
        return {
            "limit": svc.history_limit,
            "versions": svc.list_history_versions(),
            "current_version": svc.try_current_version(),
        }

    @app.get("/history/{version}")
    def get_history(version: int) -> dict:
        snap = app.state.service.historical_snapshot(version)
        return {"snapshot": snap.to_public_dict()}

    @app.post("/rollback")
    def rollback(body: RollbackRequest) -> dict:
        svc = app.state.service
        snap = svc.rollback(
            body.target_version, expected_version=body.expected_version
        )
        return {
            "status": "rolled_back",
            "rolled_back_from": body.target_version,
            "snapshot": snap.to_public_dict(),
        }

    return app


def _try_snapshot(service: RuntimeConfigService):
    try:
        return service.snapshot()
    except ConfigError:
        return None


def config_error_handler(request: Request, exc: ConfigError) -> JSONResponse:
    """把 ConfigError 转成结构化 JSON 响应。"""
    status = _STATUS_BY_CODE.get(exc.code, 422)
    return JSONResponse(status_code=status, content=exc.to_dict())
