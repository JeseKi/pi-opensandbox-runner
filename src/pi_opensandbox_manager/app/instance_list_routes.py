from __future__ import annotations

from typing import Any, Literal

from fastapi import Depends, FastAPI, Query
from sqlalchemy import and_, desc, or_, select

from ..database import ManagerDatabase
from ..models import RunnerInstance, RunnerPolicy
from ..openapi_docs import api_doc
from ..schemas import InstanceOut, InstancePage
from ..security import Principal
from .helpers import _instance_out
from .pagination import _decode_page_cursor, _encode_page_cursor


def register_instance_list_routes(
    app: FastAPI, db_control: ManagerDatabase, service_principal: Any
) -> None:

    @app.get(
        "/v1/instances",
        response_model=InstancePage,
        **api_doc(
            summary="分页列出 Runner Instance",
            description=(
                "列出当前 service token 所属 consumer 的 Runner Instance，用于管理界面、状态巡检、"
                "失败排查和孤儿资源核对；不会返回其他 consumer 的记录或任何运行凭据。\n\n"
                "结果固定按 `created_at DESC, id DESC` 排序。首次请求省略 cursor，后续把响应中的 "
                "`next_cursor` 原样传回；cursor 是不透明实现细节，不要解析、拼接或长期保存。"
                "可以用 `q` 对 `subject_ref` 做不区分大小写的包含搜索，也可以按精确 `state` 和 "
                "`policy_slug` 筛选；多个条件按 AND 组合，翻页期间应保持筛选条件不变。\n\n"
                "需要 service token 的 `instances:read` scope。最大每页 500 条。"
            ),
            tag="Instance（运行实例）",
            operation_id="list_manager_instances",
            response_description="当前 consumer 的一页 Instance 和下一页游标。",
        ),
    )
    async def list_instances(
        cursor: str | None = Query(
            default=None,
            min_length=1,
            max_length=1024,
            description="上一页返回的 next_cursor；首次请求省略。",
        ),
        limit: int = Query(
            default=100,
            ge=1,
            le=500,
            description="每页条数，默认 100，最大 500。",
        ),
        state_filter: Literal[
            "provisioning",
            "ready",
            "stopping",
            "stopped",
            "destroying",
            "destroyed",
            "failed",
        ]
        | None = Query(
            default=None,
            alias="state",
            description="精确筛选 Instance 生命周期状态。",
        ),
        policy_slug: str | None = Query(
            default=None,
            min_length=1,
            max_length=120,
            description="精确筛选当前应用的 Policy slug。",
        ),
        q: str | None = Query(
            default=None,
            min_length=1,
            max_length=160,
            pattern=r".*\S.*",
            description="对 subject_ref 做不区分大小写的包含搜索。",
        ),
        caller: Principal = Depends(service_principal),
    ) -> InstancePage:
        caller.require("instances:read")
        assert caller.consumer_id is not None
        position = _decode_page_cursor(cursor, "instance") if cursor else None
        with db_control.session() as db:
            statement = select(RunnerInstance).where(
                RunnerInstance.consumer_id == caller.consumer_id
            )
            if q is not None:
                statement = statement.where(
                    RunnerInstance.subject_ref.icontains(q.strip(), autoescape=True)
                )
            if state_filter is not None:
                statement = statement.where(RunnerInstance.state == state_filter)
            if policy_slug is not None:
                statement = statement.join(
                    RunnerPolicy, RunnerPolicy.id == RunnerInstance.policy_id
                ).where(RunnerPolicy.slug == policy_slug)
            if position is not None:
                statement = statement.where(
                    or_(
                        RunnerInstance.created_at < position.created_at,
                        and_(
                            RunnerInstance.created_at == position.created_at,
                            RunnerInstance.id < position.item_id,
                        ),
                    )
                )
            records = list(
                db.scalars(
                    statement.order_by(
                        desc(RunnerInstance.created_at),
                        desc(RunnerInstance.id),
                    ).limit(limit + 1)
                )
            )
            has_more = len(records) > limit
            page_records = records[:limit]
            items: list[InstanceOut] = []
            for instance in page_records:
                policy = db.get(RunnerPolicy, instance.policy_id)
                assert policy is not None
                items.append(_instance_out(instance, policy))
            next_cursor = (
                _encode_page_cursor(
                    page_records[-1].created_at,
                    page_records[-1].id,
                )
                if has_more and page_records
                else None
            )
            return InstancePage(
                items=items,
                next_cursor=next_cursor,
                has_more=has_more,
            )
