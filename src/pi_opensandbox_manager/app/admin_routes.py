from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI

from ..database import ManagerDatabase
from ..openapi_docs import api_doc
from ..problems import ManagerProblem
from ..schemas import ModelOut, PolicyOut
from ..security import Principal


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
                "模型目录由 `RUNNER_MANAGER_CATALOG_PATH` 指向的配置文件管理；此写接口保留"
                "仅用于向旧客户端返回稳定的迁移错误。"
            ),
            tag="Admin（内部管理）",
            operation_id="create_manager_model",
            response_description="新发布的模型目录项。",
        ),
    )
    async def create_model(_: Principal = Depends(admin_dependency)) -> ModelOut:
        raise ManagerProblem(409, "catalog_file_managed", "models are managed by catalog file")

    @app.post(
        "/admin/v1/policies",
        response_model=PolicyOut,
        status_code=201,
        **api_doc(
            summary="发布 Runner Policy revision",
            description=(
                "Runner Policy 由 `RUNNER_MANAGER_CATALOG_PATH` 指向的配置文件管理；此写接口"
                "保留仅用于向旧客户端返回稳定的迁移错误。"
            ),
            tag="Admin（内部管理）",
            operation_id="create_manager_policy_revision",
            response_description="新发布的 Policy revision。",
        ),
    )
    async def create_policy(_: Principal = Depends(admin_dependency)) -> PolicyOut:
        raise ManagerProblem(409, "catalog_file_managed", "policies are managed by catalog file")
