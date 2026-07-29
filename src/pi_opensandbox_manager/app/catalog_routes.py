from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI
from sqlalchemy import desc, select

from ..database import ManagerDatabase
from ..models import ModelDeployment, RunnerPolicy
from ..openapi_docs import api_doc
from ..schemas import ModelOut, PolicyOut
from ..security import Principal
from .helpers import _policy_out


def register_catalog_routes(
    app: FastAPI, db_control: ManagerDatabase, service_principal: Any
) -> None:

    @app.get(
        "/v1/catalog/models",
        response_model=list[ModelOut],
        **api_doc(
            summary="列出可用模型",
            description=(
                "返回当前已发布的模型目录。创建 Session 时应把其中的 `slug` 作为 "
                "`model_slug`，并确保所选 Runner Policy 也允许该模型。\n\n"
                "需要 service token 的 `catalog:read` scope。Catalog 只描述模型能力，"
                "不返回提供商密钥，也不会验证 LiteLLM 路由当前是否健康。"
            ),
            tag="Catalog（目录）",
            operation_id="list_manager_models",
            response_description="按 slug 排序的已发布模型列表。",
        ),
    )
    async def models(
        caller: Principal = Depends(service_principal),
    ) -> list[ModelOut]:
        caller.require("catalog:read")
        with db_control.session() as db:
            records = list(
                db.scalars(
                    select(ModelDeployment)
                    .where(ModelDeployment.state == "published")
                    .order_by(ModelDeployment.slug)
                )
            )
            return [ModelOut.model_validate(item) for item in records]

    @app.get(
        "/v1/catalog/policies",
        response_model=list[PolicyOut],
        **api_doc(
            summary="列出可选 Runner Policy",
            description=(
                "每个 slug 只返回最新的已发布 revision。业务系统只保存并提交 `slug`；CPU、"
                "内存、预算、速率、并发和 egress 等内部规则由 Manager 管理，不通过该目录下放。\n\n"
                "创建或更新用户 Instance 时，把所选 `slug` 传给 "
                "`PUT /v1/instances/{subject_ref}`。发布相同 slug 的新 revision 后，再次 ensure "
                "会触发该 Instance 重新 provision 和凭据轮换。\n\n"
                "需要 service token 的 `catalog:read` scope。"
            ),
            tag="Catalog（目录）",
            operation_id="list_manager_policies",
            response_description="每个 Policy slug 的最新已发布 revision。",
        ),
    )
    async def policies(
        caller: Principal = Depends(service_principal),
    ) -> list[PolicyOut]:
        caller.require("catalog:read")
        with db_control.session() as db:
            records = list(
                db.scalars(
                    select(RunnerPolicy)
                    .where(RunnerPolicy.state == "published")
                    .order_by(RunnerPolicy.slug, desc(RunnerPolicy.revision))
                )
            )
            latest: dict[str, RunnerPolicy] = {}
            for item in records:
                latest.setdefault(item.slug, item)
            return [_policy_out(item) for item in latest.values()]

