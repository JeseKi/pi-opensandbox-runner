from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from fastapi import Depends, FastAPI
from sqlalchemy import desc, select

from ..database import ManagerDatabase
from ..models import ModelDeployment, RunnerPolicy
from ..openapi_docs import api_doc
from ..problems import ManagerProblem
from ..schemas import AdminModelCreate, AdminPolicyCreate, ModelOut, PolicyOut
from ..security import Principal
from .helpers import _policy_out


def register_admin_routes(
    app: FastAPI,
    database: ManagerDatabase,
    admin_dependency: Any,
) -> None:
    @app.post(
        "/admin/v1/models",
        response_model=ModelOut,
        status_code=201,
        **api_doc(
            summary="发布模型目录项",
            description=(
                "使用 admin token 创建一个已发布模型。slug 已存在时返回 409，当前 API 不支持"
                "覆盖或自动增加模型 revision。\n\n"
                "此操作**只写 Manager Catalog**，不会创建 LiteLLM 路由，也不接收提供商明文密钥。"
                "调用前必须先配置并验证同名 LiteLLM alias，否则后续 provision 或模型请求会失败。"
            ),
            tag="Admin（内部管理）",
            operation_id="create_manager_model",
            response_description="新发布的模型目录项。",
        ),
    )
    async def create_model(
        payload: AdminModelCreate,
        _: Principal = Depends(admin_dependency),
    ) -> ModelOut:
        with database.session() as db:
            existing = db.scalar(
                select(ModelDeployment).where(ModelDeployment.slug == payload.slug)
            )
            if existing is not None:
                raise ManagerProblem(409, "model_exists", "model already exists")
            record = ModelDeployment(
                id=str(uuid4()), **payload.model_dump(), state="published", revision=1
            )
            db.add(record)
            db.flush()
            return ModelOut.model_validate(record)

    @app.post(
        "/admin/v1/policies",
        response_model=PolicyOut,
        status_code=201,
        **api_doc(
            summary="发布 Runner Policy revision",
            description=(
                "使用 admin token 发布 Policy。相同 slug 每次调用都会创建新的递增 revision，旧 "
                "revision 保留用于审计；Catalog 只返回最新已发布 revision。\n\n"
                "`default_model_slug` 必须包含在 `model_slugs` 中，所有模型必须已经发布。Policy "
                "同时控制 Sandbox CPU/内存、Session 上限、LiteLLM 预算/速率/并发和网络 egress。"
                "当前 egress 域名格式尚未完整自动校验，发布前必须人工复核。\n\n"
                "现有 Instance 不会被主动批量更新；业务系统再次 ensure 时才应用新 revision。"
            ),
            tag="Admin（内部管理）",
            operation_id="create_manager_policy_revision",
            response_description="新发布的 Policy revision。",
        ),
    )
    async def create_policy(
        payload: AdminPolicyCreate,
        _: Principal = Depends(admin_dependency),
    ) -> PolicyOut:
        if payload.default_model_slug not in payload.model_slugs:
            raise ManagerProblem(422, "policy_invalid", "default model must be allowed")
        with database.session() as db:
            existing = list(
                db.scalars(
                    select(ModelDeployment).where(
                        ModelDeployment.slug.in_(payload.model_slugs),
                        ModelDeployment.state == "published",
                    )
                )
            )
            if len(existing) != len(set(payload.model_slugs)):
                raise ManagerProblem(422, "policy_invalid", "policy references unknown models")
            revision = (
                db.scalar(
                    select(RunnerPolicy.revision)
                    .where(RunnerPolicy.slug == payload.slug)
                    .order_by(desc(RunnerPolicy.revision))
                )
                or 0
            ) + 1
            record = RunnerPolicy(
                id=str(uuid4()),
                slug=payload.slug,
                label=payload.label,
                revision=revision,
                state="published",
                model_slugs_json=json.dumps(payload.model_slugs),
                default_model_slug=payload.default_model_slug,
                cpu=payload.cpu,
                memory=payload.memory,
                max_active_sessions=payload.max_active_sessions,
                max_budget=payload.max_budget,
                budget_duration=payload.budget_duration,
                rpm_limit=payload.rpm_limit,
                tpm_limit=payload.tpm_limit,
                max_parallel_requests=payload.max_parallel_requests,
                egress_domains_json=json.dumps(payload.egress_domains),
            )
            db.add(record)
            db.flush()
            return _policy_out(record)
