from __future__ import annotations

import asyncio
import base64
import binascii
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, cast
from uuid import uuid4

from fastapi import (
    Body,
    Depends,
    FastAPI,
    Header,
    Path,
    Query,
    Request,
    Response,
    status,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import and_, desc, or_, select
from sqlalchemy.orm import Session

from ..schemas import CommandCreate
from .clients import BridgeClient, UpstreamProblem, as_manager_problem
from .config import ManagerSettings
from .crypto import CredentialCipher
from .database import ManagerDatabase
from .models import (
    ManagerOperation,
    ModelDeployment,
    RunnerInstance,
    RunnerPolicy,
    SessionBinding,
    TurnBinding,
)
from .openapi_docs import APP_DESCRIPTION, OPENAPI_TAGS, api_doc
from .problems import ManagerProblem, problem_response
from .schemas import (
    AcceptedOperation,
    AdminModelCreate,
    AdminPolicyCreate,
    EventOut,
    EventPage,
    InstanceEnsure,
    InstanceOut,
    InstancePage,
    ModelOut,
    OperationOut,
    PolicyOut,
    SessionEnsure,
    SessionOut,
    SessionPage,
    TurnOut,
    TurnSubmit,
)
from .security import Authenticator, ManagerBearerCredentials, Principal, bootstrap
from .service.long_tasks import OperationExecutor
from .service.short_transactions import (
    enqueue_instance_operation,
    ensure_instance,
    seed_catalog,
)


@dataclass(frozen=True)
class BridgeConnection:
    instance_id: str
    bridge_url: str
    bridge_token: str


@dataclass(frozen=True)
class PageCursor:
    created_at: datetime
    item_id: str


def _json(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    parsed = json.loads(value)
    return parsed if isinstance(parsed, dict) else None


def _encode_page_cursor(created_at: datetime, item_id: str) -> str:
    payload = json.dumps(
        [1, created_at.isoformat(), item_id],
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_page_cursor(value: str, resource: str) -> PageCursor:
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.urlsafe_b64decode(value + padding).decode()
        payload = json.loads(decoded)
        if (
            not isinstance(payload, list)
            or len(payload) != 3
            or payload[0] != 1
            or not isinstance(payload[1], str)
            or not isinstance(payload[2], str)
            or not payload[2]
        ):
            raise ValueError("invalid cursor payload")
        return PageCursor(
            created_at=datetime.fromisoformat(payload[1]),
            item_id=payload[2],
        )
    except (
        binascii.Error,
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ) as exc:
        raise ManagerProblem(
            422,
            "invalid_cursor",
            f"{resource} cursor is invalid",
        ) from exc


def create_manager_app(
    settings: ManagerSettings | None = None,
    database: ManagerDatabase | None = None,
) -> FastAPI:
    resolved = settings or ManagerSettings.from_env()
    db_control = database or ManagerDatabase(resolved)
    authenticator = Authenticator(db_control)
    cipher = CredentialCipher(resolved.credential_encryption_key)
    executor = OperationExecutor(db_control, resolved)
    stop_event = asyncio.Event()

    async def operation_loop() -> None:
        while not stop_event.is_set():
            worked = await asyncio.to_thread(executor.execute_next)
            if not worked:
                with suppress(TimeoutError):
                    await asyncio.wait_for(
                        stop_event.wait(), timeout=resolved.operation_poll_seconds
                    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db_control.initialize()
        bootstrap(db_control, resolved)
        with db_control.session() as db:
            seed_catalog(db)
        app.state.worker_alive = True
        worker = asyncio.create_task(operation_loop())
        try:
            yield
        finally:
            stop_event.set()
            await worker
            app.state.worker_alive = False
            db_control.dispose()

    app = FastAPI(
        title="Pi Runner Manager 内部 API",
        version="1.0.0",
        summary="统一管理 Pi/OpenSandbox Runner 的内部控制面",
        description=APP_DESCRIPTION,
        lifespan=lifespan,
        openapi_url="/v1/openapi.json",
        docs_url="/v1/docs",
        openapi_tags=OPENAPI_TAGS,
    )
    app.state.database = db_control
    app.state.worker_alive = False

    @app.middleware("http")
    async def request_id(request: Request, call_next: Any) -> Response:
        value = request.headers.get("X-Request-ID") or uuid4().hex
        request.state.request_id = value[:64]
        response = cast(Response, await call_next(request))
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["Runner-Protocol-Version"] = "1"
        return response

    @app.exception_handler(ManagerProblem)
    async def manager_problem(request: Request, exc: ManagerProblem) -> JSONResponse:
        return problem_response(exc, request)

    @app.exception_handler(RequestValidationError)
    async def validation_problem(request: Request, exc: RequestValidationError) -> JSONResponse:
        return problem_response(
            ManagerProblem(
                422,
                "validation_error",
                "request validation failed",
                extra={"errors": exc.errors()},
            ),
            request,
        )

    def principal(
        credentials: ManagerBearerCredentials,
    ) -> Principal:
        token = credentials.credentials if credentials is not None else None
        return authenticator.principal(token)

    def service_principal(
        value: Principal = Depends(principal),
    ) -> Principal:
        if value.consumer_id is None:
            raise ManagerProblem(403, "consumer_required", "consumer token required")
        return value

    def admin_principal(value: Principal = Depends(principal)) -> Principal:
        if value.kind != "admin":
            raise ManagerProblem(403, "admin_required", "admin token required")
        return value

    @app.get("/healthz", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def ready(request: Request) -> dict[str, str]:
        if not request.app.state.worker_alive:
            raise ManagerProblem(
                503, "worker_not_ready", "operation worker is not running", retryable=True
            )
        return {"status": "ready", "protocol_version": "1"}

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
                "可以按精确 `state` 和 `policy_slug` 筛选，翻页期间应保持筛选条件不变。\n\n"
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
        caller: Principal = Depends(service_principal),
    ) -> InstancePage:
        caller.require("instances:read")
        assert caller.consumer_id is not None
        position = _decode_page_cursor(cursor, "instance") if cursor else None
        with db_control.session() as db:
            statement = select(RunnerInstance).where(
                RunnerInstance.consumer_id == caller.consumer_id
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

    @app.put(
        "/v1/instances/{subject_ref}",
        response_model=AcceptedOperation,
        status_code=status.HTTP_202_ACCEPTED,
        **api_doc(
            summary="幂等确保 Runner Instance",
            description=(
                "为当前 consumer 的业务主体创建或校正 Runner Instance。`subject_ref` 通常使用"
                "调用方稳定的用户 ID，例如 `user-42`，不能使用昵称或邮箱等可变值。\n\n"
                "- `(consumer, subject_ref)` 是幂等键；安全重试不会创建第二个 Instance。\n"
                "- 新建、Policy revision 变化、stopped/failed/destroyed 状态会排队 provision。\n"
                "- 已经 ready 且 Policy 未变化时，返回最近成功 Operation，不会轮换凭据。\n"
                "- 返回 `202` 后轮询 `operation.id`；只有 `instance.state=ready` "
                "才能创建 Session。\n\n"
                "需要 `instances:write` scope。"
            ),
            tag="Instance（运行实例）",
            operation_id="ensure_manager_instance",
            response_description="Instance 快照和需要轮询的 Operation。",
        ),
    )
    async def put_instance(
        payload: InstanceEnsure,
        subject_ref: str = Path(
            min_length=1,
            max_length=120,
            description="当前 consumer 内稳定且唯一的业务主体标识，例如 user-42。",
            examples=["user-42"],
        ),
        caller: Principal = Depends(service_principal),
    ) -> AcceptedOperation:
        caller.require("instances:write")
        assert caller.consumer_id is not None
        with db_control.session() as db:
            instance, operation = ensure_instance(
                db,
                consumer_id=caller.consumer_id,
                subject_ref=subject_ref,
                payload=payload,
                cipher=cipher,
            )
            db.flush()
            policy = db.get(RunnerPolicy, instance.policy_id)
            assert policy is not None
            return AcceptedOperation(
                instance=_instance_out(instance, policy),
                operation=_operation_out(operation),
            )

    @app.get(
        "/v1/instances/{subject_ref}",
        response_model=InstanceOut,
        **api_doc(
            summary="查询 Runner Instance",
            description=(
                "返回当前 consumer 下指定 Instance 的最新状态、阶段、Policy revision 和失败信息。"
                "provisioning/stopping/destroying 是过渡态；ready 才能访问 Session。\n\n"
                "需要 `instances:read` scope。"
            ),
            tag="Instance（运行实例）",
            operation_id="get_manager_instance",
            response_description="Instance 当前快照。",
        ),
    )
    async def get_instance(
        subject_ref: str,
        caller: Principal = Depends(service_principal),
    ) -> InstanceOut:
        caller.require("instances:read")
        with db_control.session() as db:
            instance = _owned_instance(db, caller, subject_ref)
            policy = db.get(RunnerPolicy, instance.policy_id)
            assert policy is not None
            return _instance_out(instance, policy)

    @app.post(
        "/v1/instances/{subject_ref}:reconcile",
        response_model=AcceptedOperation,
        status_code=202,
        **api_doc(
            summary="重新校正 Runner Instance",
            description=(
                "为现有 Instance 排队一次 provision 校正，用于恢复 Sandbox/LiteLLM key 与 Manager "
                "记录不一致的情况。它复用现有 Instance 凭据，不等同于切换 Policy，"
                "也不保证轮换 key。\n\n"
                "对 stopped 或 destroyed Instance 应调用 ensure，而不是 reconcile。"
                "返回 `202` 后轮询 "
                "Operation。需要 `instances:write` scope。"
            ),
            tag="Instance（运行实例）",
            operation_id="reconcile_manager_instance",
            response_description="更新后的 Instance 快照和 provision Operation。",
        ),
    )
    async def reconcile_instance(
        subject_ref: str,
        caller: Principal = Depends(service_principal),
    ) -> AcceptedOperation:
        caller.require("instances:write")
        with db_control.session() as db:
            instance = _owned_instance(db, caller, subject_ref)
            instance.state = "provisioning"
            operation = enqueue_instance_operation(db, instance=instance, kind="provision")
            db.flush()
            policy = db.get(RunnerPolicy, instance.policy_id)
            assert policy is not None
            return AcceptedOperation(
                instance=_instance_out(instance, policy),
                operation=_operation_out(operation),
            )

    @app.post(
        "/v1/instances/{subject_ref}:stop",
        response_model=AcceptedOperation,
        status_code=202,
        **api_doc(
            summary="停止 Runner Instance",
            description=(
                "异步停止 OpenSandbox，但保留 Manager 记录、命名持久卷和恢复所需元数据。停止过程中"
                "不能访问 Session。需要恢复时再次调用 ensure。\n\n"
                "重复请求会复用同类型的活动 Operation。需要 `instances:write` scope。"
            ),
            tag="Instance（运行实例）",
            operation_id="stop_manager_instance",
            response_description="Instance 快照和 stop Operation。",
        ),
    )
    async def stop_instance(
        subject_ref: str,
        caller: Principal = Depends(service_principal),
    ) -> AcceptedOperation:
        return _instance_operation(db_control, caller, subject_ref, "stop")

    @app.post(
        "/v1/instances/{subject_ref}:destroy",
        response_model=AcceptedOperation,
        status_code=202,
        **api_doc(
            summary="销毁 Runner Instance",
            description=(
                "高风险异步操作：销毁 Sandbox 并撤销运行凭据。必须设置请求头 "
                "`X-Confirm-Destroy`，值与路径中的 `subject_ref` 完全一致。\n\n"
                "当前实现**保留命名持久卷**，因此不是数据擦除接口；之后 ensure 可以重新 provision。"
                "重复请求会复用同类型的活动 Operation。需要 `instances:write` scope。"
            ),
            tag="Instance（运行实例）",
            operation_id="destroy_manager_instance",
            response_description="Instance 快照和 destroy Operation。",
        ),
    )
    async def destroy_instance(
        subject_ref: str,
        confirmation: Annotated[
            str | None,
            Header(
                alias="X-Confirm-Destroy",
                description="必须与路径中的 subject_ref 完全一致，用于防止误销毁。",
            ),
        ] = None,
        caller: Principal = Depends(service_principal),
    ) -> AcceptedOperation:
        if confirmation != subject_ref:
            raise ManagerProblem(
                422,
                "destroy_confirmation_required",
                "X-Confirm-Destroy must exactly match subject_ref",
            )
        return _instance_operation(db_control, caller, subject_ref, "destroy")

    @app.get(
        "/v1/operations/{operation_id}",
        response_model=OperationOut,
        **api_doc(
            summary="查询异步 Operation",
            description=(
                "轮询 provision、stop 或 destroy 的执行状态。建议带退避轮询；`succeeded` 和 "
                "`failed` 是终态。`pending` 可能处于 queued 或 retry_wait，"
                "`attempt` 表示已执行次数。\n\n"
                "只能读取当前 consumer 创建的 Operation。"
            ),
            tag="Instance（运行实例）",
            operation_id="get_manager_operation",
            response_description="Operation 当前状态。",
        ),
    )
    async def get_operation(
        operation_id: str,
        caller: Principal = Depends(service_principal),
    ) -> OperationOut:
        with db_control.session() as db:
            operation = db.get(ManagerOperation, operation_id)
            if operation is None or operation.consumer_id != caller.consumer_id:
                raise ManagerProblem(404, "operation_not_found", "operation not found")
            return _operation_out(operation)

    @app.get(
        "/v1/instances/{subject_ref}/sessions",
        response_model=SessionPage,
        **api_doc(
            summary="分页列出 Instance 的 Session",
            description=(
                "列出当前 consumer 指定 Instance 中由 Manager 建立且仍保留绑定记录的 Session，"
                "用于管理界面、数据核对、灾难恢复和孤儿 Session 排查。\n\n"
                "结果来自 Manager 数据库快照，不会逐个请求 Pi Bridge，因此不会产生 N+1 上游调用；"
                "`state` 可能短暂滞后。需要准确状态时调用单 Session GET，该接口会刷新 Bridge 状态。"
                "已通过 DELETE 删除绑定的 Session 不会出现在列表中。\n\n"
                "结果固定按 `created_at DESC, id DESC` 排序。首次请求省略 cursor，后续把 "
                "`next_cursor` 原样传回，并保持 `state`、`model_slug` 筛选条件不变。"
                "Instance 即使 stopped、destroyed 或 failed 仍可读取历史绑定快照。\n\n"
                "需要 service token 的 `sessions:read` scope；最大每页 500 条。"
            ),
            tag="Session（会话）",
            operation_id="list_manager_sessions",
            response_description="指定 Instance 的一页 Session 和下一页游标。",
        ),
    )
    async def list_sessions(
        subject_ref: str,
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
        state_filter: Literal["provisioning", "ready", "running", "failed"]
        | None = Query(
            default=None,
            alias="state",
            description="精确筛选 Manager 数据库中的 Session 状态。",
        ),
        model_slug: str | None = Query(
            default=None,
            min_length=1,
            max_length=120,
            description="精确筛选 Session 使用的 model slug。",
        ),
        caller: Principal = Depends(service_principal),
    ) -> SessionPage:
        caller.require("sessions:read")
        position = _decode_page_cursor(cursor, "session") if cursor else None
        with db_control.session() as db:
            instance = _owned_instance(db, caller, subject_ref)
            statement = select(SessionBinding).where(
                SessionBinding.instance_id == instance.id
            )
            if state_filter is not None:
                statement = statement.where(SessionBinding.state == state_filter)
            if model_slug is not None:
                statement = statement.where(SessionBinding.model_slug == model_slug)
            if position is not None:
                statement = statement.where(
                    or_(
                        SessionBinding.created_at < position.created_at,
                        and_(
                            SessionBinding.created_at == position.created_at,
                            SessionBinding.external_session_id < position.item_id,
                        ),
                    )
                )
            records = list(
                db.scalars(
                    statement.order_by(
                        desc(SessionBinding.created_at),
                        desc(SessionBinding.external_session_id),
                    ).limit(limit + 1)
                )
            )
            has_more = len(records) > limit
            page_records = records[:limit]
            next_cursor = (
                _encode_page_cursor(
                    page_records[-1].created_at,
                    page_records[-1].external_session_id,
                )
                if has_more and page_records
                else None
            )
            return SessionPage(
                items=[_session_out(item) for item in page_records],
                next_cursor=next_cursor,
                has_more=has_more,
            )

    @app.put(
        "/v1/instances/{subject_ref}/sessions/{session_id}",
        response_model=SessionOut,
        **api_doc(
            summary="幂等确保 Session",
            description=(
                "在 ready Instance 中创建或取得一个 Pi Session。`session_id` 由调用方生成，并在"
                "该 Instance 内稳定唯一；网络超时后可使用相同 ID 和请求体安全重试。\n\n"
                "`model_slug` 必须由 Instance 当前 Policy 允许。`legacy_bridge_session_id` 和 "
                "`legacy_cwd` 仅供旧数据迁移，新调用方不要设置。首次调用可能同步等待 Bridge 创建"
                "会话，但不会运行 Agent Turn。\n\n需要 `sessions:write` scope。"
            ),
            tag="Session（会话）",
            operation_id="ensure_manager_session",
            response_description="已创建或已存在的 Session。",
        ),
    )
    async def put_session(
        payload: SessionEnsure,
        subject_ref: str,
        session_id: str,
        caller: Principal = Depends(service_principal),
    ) -> SessionOut:
        caller.require("sessions:write")
        connection, binding = _prepare_session(
            db_control, cipher, caller, subject_ref, session_id, payload
        )
        if binding.bridge_session_id is None:
            client = BridgeClient(resolved, connection.bridge_url, connection.bridge_token)
            try:
                bridge_id = _find_bridge_session(
                    client, binding.cwd, payload.legacy_bridge_session_id
                )
                if bridge_id is None:
                    created = await asyncio.to_thread(
                        client.create_session,
                        {
                            "name": payload.title,
                            "model": payload.model_slug,
                            "cwd": binding.cwd,
                        },
                    )
                    bridge_id = str(created["id"])
            except UpstreamProblem as exc:
                raise as_manager_problem(exc) from exc
            finally:
                client.close()
            with db_control.session() as db:
                current = db.get(SessionBinding, binding.id)
                if current is not None:
                    current.bridge_session_id = bridge_id
                    current.state = "ready"
                    binding = current
        return _session_out(binding)

    @app.get(
        "/v1/instances/{subject_ref}/sessions/{session_id}",
        response_model=SessionOut,
        **api_doc(
            summary="查询 Session",
            description=(
                "查询 Session，并同步刷新 Bridge 的运行状态。如果 Agent 已停止生成，Manager 会把"
                "对应 running Turn 收敛为 succeeded，并清除 `active_turn_id`。\n\n"
                "该读取因此会更新 Manager 状态；Instance 必须为 ready。需要 `sessions:read` scope。"
            ),
            tag="Session（会话）",
            operation_id="get_manager_session",
            response_description="Session 当前状态。",
        ),
    )
    async def get_session(
        subject_ref: str,
        session_id: str,
        caller: Principal = Depends(service_principal),
    ) -> SessionOut:
        caller.require("sessions:read")
        connection, binding = _session_connection(
            db_control, cipher, caller, subject_ref, session_id
        )
        if binding.bridge_session_id:
            client = BridgeClient(resolved, connection.bridge_url, connection.bridge_token)
            try:
                remote = await asyncio.to_thread(client.get_session, binding.bridge_session_id)
            except UpstreamProblem as exc:
                raise as_manager_problem(exc) from exc
            finally:
                client.close()
            is_streaming = bool(remote.get("is_streaming"))
            with db_control.session() as db:
                current = db.get(SessionBinding, binding.id)
                if current is not None:
                    current.state = "running" if is_streaming else "ready"
                    if not is_streaming and current.active_turn_id:
                        turn = db.scalar(
                            select(TurnBinding).where(
                                TurnBinding.session_binding_id == current.id,
                                TurnBinding.external_turn_id == current.active_turn_id,
                            )
                        )
                        if turn is not None and turn.status == "running":
                            turn.status = "succeeded"
                        current.active_turn_id = None
                    binding = current
        return _session_out(binding)

    @app.delete(
        "/v1/instances/{subject_ref}/sessions/{session_id}",
        status_code=204,
        **api_doc(
            summary="删除 Session",
            description=(
                "删除 Bridge 中的 Pi Session 以及 Manager 的绑定记录。"
                "Bridge 已不存在时仍按成功处理。"
                "调用方应先处理活动 Turn；删除后使用相同 `session_id` "
                "可以创建一个全新 Session。\n\n"
                "这不会删除整个 Instance 或其工作区命名卷。需要 `sessions:write` scope。"
            ),
            tag="Session（会话）",
            operation_id="delete_manager_session",
            response_description="删除成功，无响应体。",
        ),
    )
    async def delete_session(
        subject_ref: str,
        session_id: str,
        caller: Principal = Depends(service_principal),
    ) -> Response:
        caller.require("sessions:write")
        connection, binding = _session_connection(
            db_control, cipher, caller, subject_ref, session_id
        )
        if binding.bridge_session_id:
            client = BridgeClient(resolved, connection.bridge_url, connection.bridge_token)
            try:
                await asyncio.to_thread(client.delete_session, binding.bridge_session_id)
            except UpstreamProblem as exc:
                if exc.status_code != 404:
                    raise as_manager_problem(exc) from exc
            finally:
                client.close()
        with db_control.session() as db:
            current = db.get(SessionBinding, binding.id)
            if current is not None:
                db.delete(current)
        return Response(status_code=204)

    @app.put(
        "/v1/instances/{subject_ref}/sessions/{session_id}/turns/{turn_id}",
        response_model=TurnOut,
        status_code=202,
        **api_doc(
            summary="幂等提交 Agent Turn",
            description=(
                "把用户输入提交给指定 Pi Session。`turn_id` 由调用方生成，"
                "同时作为幂等键；调用方应先"
                "在自己的数据库中持久化任务与入队记录，再调用本接口。\n\n"
                "- 相同 Turn ID 与相同 input：返回已有 Turn，可安全重试。\n"
                "- 相同 Turn ID 与不同 input：返回 `409 idempotency_conflict`。\n"
                "- 同一 Session 已有活动 Turn：返回 `409 turn_active`。\n"
                "- 返回 `202/running` 只表示 Bridge 已接受；通过事件和 GET Turn 跟踪终态。\n\n"
                "需要 `sessions:write` scope。"
            ),
            tag="Turn（任务轮次）",
            operation_id="submit_manager_turn",
            response_description="已接受或幂等命中的 Turn。",
        ),
    )
    async def put_turn(
        payload: TurnSubmit,
        subject_ref: str,
        session_id: str,
        turn_id: str,
        caller: Principal = Depends(service_principal),
    ) -> TurnOut:
        caller.require("sessions:write")
        connection, binding = _session_connection(
            db_control, cipher, caller, subject_ref, session_id
        )
        if not binding.bridge_session_id:
            raise ManagerProblem(
                409,
                "session_not_ready",
                "runtime session is not ready",
                retryable=True,
            )
        with db_control.session() as db:
            current = db.get(SessionBinding, binding.id)
            assert current is not None
            turn = db.scalar(
                select(TurnBinding).where(
                    TurnBinding.session_binding_id == current.id,
                    TurnBinding.external_turn_id == turn_id,
                )
            )
            if turn is not None:
                if turn.input_text != payload.input:
                    raise ManagerProblem(
                        409,
                        "idempotency_conflict",
                        "turn id was already used with a different input",
                    )
                return _turn_out(turn)
            if current.active_turn_id:
                raise ManagerProblem(409, "turn_active", "another turn is already active")
            turn = TurnBinding(
                id=str(uuid4()),
                session_binding_id=current.id,
                external_turn_id=turn_id,
                input_text=payload.input,
                status="queued",
            )
            db.add(turn)
            db.flush()
            turn_db_id = turn.id
        client = BridgeClient(resolved, connection.bridge_url, connection.bridge_token)
        try:
            result = await asyncio.to_thread(
                client.prompt,
                binding.bridge_session_id,
                _marked_input(turn_id, payload.input),
                idempotency_key=turn_id,
            )
        except UpstreamProblem as exc:
            with db_control.session() as db:
                current_turn = db.get(TurnBinding, turn_db_id)
                if current_turn is not None:
                    current_turn.status = "failed"
                    current_turn.problem_json = json.dumps(
                        {
                            "status": exc.status_code,
                            "code": exc.code,
                            "detail": exc.detail,
                            "retryable": exc.retryable,
                        }
                    )
            raise as_manager_problem(exc) from exc
        finally:
            client.close()
        with db_control.session() as db:
            current_turn = db.get(TurnBinding, turn_db_id)
            current_session = db.get(SessionBinding, binding.id)
            assert current_turn is not None and current_session is not None
            current_turn.status = "running"
            current_turn.command_id = (
                str(result["command_id"]) if result.get("command_id") else None
            )
            current_session.state = "running"
            current_session.active_turn_id = turn_id
            return _turn_out(current_turn)

    @app.get(
        "/v1/instances/{subject_ref}/sessions/{session_id}/turns/{turn_id}",
        response_model=TurnOut,
        **api_doc(
            summary="查询 Turn",
            description=(
                "查询 Turn 状态。该接口会先刷新 Session 状态，"
                "因此 Agent 已停止生成时，running Turn "
                "可在本次读取中收敛为 succeeded。\n\n"
                "`succeeded`、`cancelled` 和 `failed` 是终态。事件内容仍需通过 events 接口读取。"
            ),
            tag="Turn（任务轮次）",
            operation_id="get_manager_turn",
            response_description="Turn 当前状态。",
        ),
    )
    async def get_turn(
        subject_ref: str,
        session_id: str,
        turn_id: str,
        caller: Principal = Depends(service_principal),
    ) -> TurnOut:
        await get_session(subject_ref, session_id, caller)
        with db_control.session() as db:
            binding = _owned_session(db, caller, subject_ref, session_id)
            turn = db.scalar(
                select(TurnBinding).where(
                    TurnBinding.session_binding_id == binding.id,
                    TurnBinding.external_turn_id == turn_id,
                )
            )
            if turn is None:
                raise ManagerProblem(404, "turn_not_found", "turn not found")
            return _turn_out(turn)

    @app.post(
        "/v1/instances/{subject_ref}/sessions/{session_id}/turns/{turn_id}:cancel",
        response_model=TurnOut,
        status_code=202,
        **api_doc(
            summary="取消活动 Turn",
            description=(
                "请求 Bridge 中止当前生成，并把 Manager Turn 标记为 cancelled。若 Bridge 已经停止，"
                "仍会收敛本地状态；不存在的 Turn 返回 404。\n\n"
                "取消是尽力而为操作，调用方仍应继续读取事件并刷新 Session。需要 "
                "`sessions:write` scope。"
            ),
            tag="Turn（任务轮次）",
            operation_id="cancel_manager_turn",
            response_description="取消后的 Turn 状态。",
        ),
    )
    async def cancel_turn(
        subject_ref: str,
        session_id: str,
        turn_id: str,
        caller: Principal = Depends(service_principal),
    ) -> TurnOut:
        caller.require("sessions:write")
        connection, binding = _session_connection(
            db_control, cipher, caller, subject_ref, session_id
        )
        if binding.active_turn_id == turn_id and binding.bridge_session_id:
            client = BridgeClient(resolved, connection.bridge_url, connection.bridge_token)
            try:
                await asyncio.to_thread(client.abort, binding.bridge_session_id)
            except UpstreamProblem as exc:
                if exc.status_code != 409:
                    raise as_manager_problem(exc) from exc
            finally:
                client.close()
        with db_control.session() as db:
            current = _owned_session(db, caller, subject_ref, session_id)
            turn = db.scalar(
                select(TurnBinding).where(
                    TurnBinding.session_binding_id == current.id,
                    TurnBinding.external_turn_id == turn_id,
                )
            )
            if turn is None:
                raise ManagerProblem(404, "turn_not_found", "turn not found")
            turn.status = "cancelled"
            if current.active_turn_id == turn_id:
                current.active_turn_id = None
                current.state = "ready"
            return _turn_out(turn)

    @app.get(
        "/v1/instances/{subject_ref}/sessions/{session_id}/events",
        response_model=EventPage,
        **api_doc(
            summary="增量读取 Session 事件",
            description=(
                "从 cursor 之后批量读取 Pi/Bridge 事件。首次从 `cursor=0` 开始，之后把响应中的 "
                "`next_cursor` 原样用于下一次请求；没有新事件时返回空 items 和不前进的 cursor。\n\n"
                "调用方必须按 `seq` 去重并持久化游标，以 `turn_id` 关联业务 Turn；未知 `type` 应"
                "忽略或透传，不能使消费循环失败。当前接口是轮询而非 SSE。\n\n"
                "需要 `sessions:read` scope。"
            ),
            tag="Turn（任务轮次）",
            operation_id="list_manager_session_events",
            response_description="事件批次和下一游标。",
        ),
    )
    async def events(
        subject_ref: str,
        session_id: str,
        cursor: int = Query(
            default=0,
            ge=0,
            description="上次响应 next_cursor 的整数值；首次请求使用 0。",
        ),
        limit: int = Query(
            default=100,
            ge=1,
            le=1000,
            description="单次最多返回的事件数，范围 1 至 1000。",
        ),
        caller: Principal = Depends(service_principal),
    ) -> EventPage:
        caller.require("sessions:read")
        connection, binding = _session_connection(
            db_control, cipher, caller, subject_ref, session_id
        )
        if not binding.bridge_session_id:
            return EventPage(items=[], next_cursor=str(cursor))
        client = BridgeClient(resolved, connection.bridge_url, connection.bridge_token)
        try:
            values, next_cursor = await asyncio.to_thread(
                client.event_batch, binding.bridge_session_id, cursor, limit
            )
        except UpstreamProblem as exc:
            raise as_manager_problem(exc) from exc
        finally:
            client.close()
        items = [_event_out(value, session_id, binding.active_turn_id) for value in values]
        return EventPage(items=items, next_cursor=str(next_cursor))

    register_data_routes(app, db_control, resolved, cipher, service_principal)
    register_admin_routes(app, db_control, admin_principal)
    return app


def _owned_instance(db: Session, caller: Principal, subject_ref: str) -> RunnerInstance:
    instance = db.scalar(
        select(RunnerInstance).where(
            RunnerInstance.consumer_id == caller.consumer_id,
            RunnerInstance.subject_ref == subject_ref,
        )
    )
    if instance is None:
        raise ManagerProblem(404, "instance_not_found", "instance not found")
    return instance


def _owned_session(
    db: Session, caller: Principal, subject_ref: str, session_id: str
) -> SessionBinding:
    instance = _owned_instance(db, caller, subject_ref)
    binding = db.scalar(
        select(SessionBinding).where(
            SessionBinding.instance_id == instance.id,
            SessionBinding.external_session_id == session_id,
        )
    )
    if binding is None:
        raise ManagerProblem(404, "session_not_found", "session not found")
    return binding


def _connection(instance: RunnerInstance, cipher: CredentialCipher) -> BridgeConnection:
    if instance.state != "ready" or not instance.bridge_url or not instance.bridge_token_encrypted:
        raise ManagerProblem(
            409,
            "instance_not_ready",
            f"runner instance is {instance.state}:{instance.phase}",
            retryable=True,
        )
    return BridgeConnection(
        instance_id=instance.id,
        bridge_url=instance.bridge_url,
        bridge_token=cipher.decrypt(instance.bridge_token_encrypted),
    )


def _prepare_session(
    database: ManagerDatabase,
    cipher: CredentialCipher,
    caller: Principal,
    subject_ref: str,
    session_id: str,
    payload: SessionEnsure,
) -> tuple[BridgeConnection, SessionBinding]:
    with database.session() as db:
        instance = _owned_instance(db, caller, subject_ref)
        connection = _connection(instance, cipher)
        binding = db.scalar(
            select(SessionBinding).where(
                SessionBinding.instance_id == instance.id,
                SessionBinding.external_session_id == session_id,
            )
        )
        if binding is None:
            binding = SessionBinding(
                id=str(uuid4()),
                instance_id=instance.id,
                external_session_id=session_id,
                bridge_session_id=payload.legacy_bridge_session_id,
                title=payload.title,
                model_slug=payload.model_slug,
                cwd=payload.legacy_cwd or f"/root/workspace/sessions/{session_id}",
                state="provisioning",
            )
            db.add(binding)
            db.flush()
        return connection, binding


def _session_connection(
    database: ManagerDatabase,
    cipher: CredentialCipher,
    caller: Principal,
    subject_ref: str,
    session_id: str,
) -> tuple[BridgeConnection, SessionBinding]:
    with database.session() as db:
        instance = _owned_instance(db, caller, subject_ref)
        binding = _owned_session(db, caller, subject_ref, session_id)
        return _connection(instance, cipher), binding


def _find_bridge_session(client: BridgeClient, cwd: str, legacy_id: str | None) -> str | None:
    for item in client.list_sessions():
        if legacy_id and item.get("id") == legacy_id:
            return legacy_id
        if item.get("cwd") == cwd and item.get("id"):
            return str(item["id"])
    return None


def _marked_input(turn_id: str, value: str) -> str:
    return f"<!-- pi-manager-turn:{turn_id} -->\n{value}"


def _event_out(value: dict[str, Any], session_id: str, turn_id: str | None) -> EventOut:
    event = value.get("event")
    data = event if isinstance(event, dict) else {"value": event}
    request_id = data.get("request_id")
    return EventOut(
        seq=int(value.get("seq") or 0),
        session_id=session_id,
        turn_id=str(request_id) if request_id else turn_id,
        occurred_at=str(value.get("timestamp") or datetime.now(UTC).isoformat()),
        type=str(data.get("type") or f"{value.get('source', 'runner')}.event"),
        data=data,
    )


def _instance_out(instance: RunnerInstance, policy: RunnerPolicy) -> InstanceOut:
    return InstanceOut(
        id=instance.id,
        subject_ref=instance.subject_ref,
        policy_slug=policy.slug,
        policy_revision=policy.revision,
        state=instance.state,
        phase=instance.phase,
        problem=_json(instance.problem_json),
        created_at=instance.created_at,
        updated_at=instance.updated_at,
        ready_at=instance.ready_at,
    )


def _operation_out(operation: ManagerOperation) -> OperationOut:
    return OperationOut(
        id=operation.id,
        kind=operation.kind,
        status=operation.status,
        phase=operation.phase,
        attempt=operation.attempt,
        problem=_json(operation.problem_json),
        created_at=operation.created_at,
        updated_at=operation.updated_at,
        finished_at=operation.finished_at,
    )


def _policy_out(policy: RunnerPolicy) -> PolicyOut:
    return PolicyOut(
        slug=policy.slug,
        label=policy.label,
        revision=policy.revision,
        models=json.loads(policy.model_slugs_json),
        default_model_slug=policy.default_model_slug,
        state=policy.state,
    )


def _session_out(binding: SessionBinding) -> SessionOut:
    return SessionOut(
        id=binding.external_session_id,
        state=binding.state,
        title=binding.title,
        model_slug=binding.model_slug,
        active_turn_id=binding.active_turn_id,
        problem=_json(binding.problem_json),
        created_at=binding.created_at,
        updated_at=binding.updated_at,
    )


def _turn_status(
    value: str,
) -> Literal["queued", "running", "succeeded", "cancelled", "failed"]:
    if value == "queued":
        return "queued"
    if value == "running":
        return "running"
    if value == "succeeded":
        return "succeeded"
    if value == "cancelled":
        return "cancelled"
    if value == "failed":
        return "failed"
    raise ManagerProblem(500, "invalid_turn_status", f"stored turn status is invalid: {value}")


def _turn_out(turn: TurnBinding) -> TurnOut:
    return TurnOut(
        id=turn.external_turn_id,
        status=_turn_status(turn.status),
        command_id=turn.command_id,
        problem=_json(turn.problem_json),
        created_at=turn.created_at,
        updated_at=turn.updated_at,
    )


def _instance_operation(
    database: ManagerDatabase,
    caller: Principal,
    subject_ref: str,
    kind: str,
) -> AcceptedOperation:
    caller.require("instances:write")
    with database.session() as db:
        instance = _owned_instance(db, caller, subject_ref)
        operation = enqueue_instance_operation(db, instance=instance, kind=kind)
        db.flush()
        policy = db.get(RunnerPolicy, instance.policy_id)
        assert policy is not None
        return AcceptedOperation(
            instance=_instance_out(instance, policy),
            operation=_operation_out(operation),
        )


def register_data_routes(
    app: FastAPI,
    database: ManagerDatabase,
    settings: ManagerSettings,
    cipher: CredentialCipher,
    service_dependency: Any,
) -> None:
    async def workspace_proxy(
        *,
        method: str,
        subject_ref: str,
        session_id: str,
        path: str,
        caller: Principal,
        content: bytes | None = None,
        content_type: str | None = None,
        if_match: str | None = None,
    ) -> Response:
        scope = "workspace:read" if method == "GET" else "workspace:write"
        caller.require(scope)
        connection, binding = _session_connection(
            database, cipher, caller, subject_ref, session_id
        )
        target = f"{binding.cwd}/{path.lstrip('/')}".rstrip("/")
        upstream_path = "/files/content" if method != "GET" or path else "/files"
        kwargs: dict[str, Any] = {"params": {"path": target}}
        headers: dict[str, str] = {}
        if method == "PUT":
            kwargs["content"] = content or b""
            if content_type:
                headers["Content-Type"] = content_type
        if if_match:
            headers["If-Match"] = if_match
        if headers:
            kwargs["headers"] = headers
        client = BridgeClient(settings, connection.bridge_url, connection.bridge_token)
        try:
            upstream = await asyncio.to_thread(
                client.passthrough, method, upstream_path, **kwargs
            )
        except UpstreamProblem as exc:
            raise as_manager_problem(exc) from exc
        finally:
            client.close()
        return Response(
            upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type"),
            headers={
                name: value
                for name in ("etag", "content-disposition", "content-range")
                if (value := upstream.headers.get(name))
            },
        )

    @app.get(
        "/v1/instances/{subject_ref}/sessions/{session_id}/workspace/{path:path}",
        **api_doc(
            summary="列出目录或读取文件",
            description=(
                "预览能力。`path` 为空时列出 Session cwd 的一级目录内容；`path` 非空时读取该路径的"
                "完整文件内容，并透传 ETag、Content-Disposition 和 Content-Range 响应头。\n\n"
                "Manager 会把相对 path 拼到 Session cwd，但当前路径前缀**不是强安全边界**，不得把"
                "此接口直接开放给不可信终端用户。当前尚未暴露 Range、offset/limit 和递归深度。\n\n"
                "需要 `workspace:read` scope，Instance 必须为 ready。"
            ),
            tag="Workspace（工作区）",
            operation_id="read_manager_workspace",
            response_description="目录 JSON 或文件原始内容，取决于 path。",
        ),
    )
    async def read_workspace(
        subject_ref: str,
        session_id: str,
        path: str = "",
        caller: Principal = Depends(service_dependency),
    ) -> Response:
        return await workspace_proxy(
            method="GET",
            subject_ref=subject_ref,
            session_id=session_id,
            path=path,
            caller=caller,
        )

    @app.put(
        "/v1/instances/{subject_ref}/sessions/{session_id}/workspace/{path:path}",
        status_code=204,
        **api_doc(
            summary="条件保存纯文本文件",
            description=(
                "预览能力。用请求体原子替换 Session cwd 下的现有文本文件。必须发送 "
                "`Content-Type: text/plain; charset=utf-8` 和此前完整读取返回的强 `If-Match` "
                "ETag；缺少 ETag 返回 428，文件已变化返回 412。\n\n"
                "目标和新内容均不能超过 1 MiB，必须是 UTF-8 且不能包含 NUL；当前不支持新建文件、"
                "二进制上传或分块写入。需要 `workspace:write` scope。"
            ),
            tag="Workspace（工作区）",
            operation_id="update_manager_workspace_file",
            response_description="保存成功，无响应体；ETag 头包含新版本。",
        ),
    )
    async def update_workspace_file(
        subject_ref: str,
        session_id: str,
        path: str,
        content: Annotated[
            bytes,
            Body(
                description="完整的 UTF-8 文本文件内容，最大 1 MiB。",
                media_type="text/plain",
            ),
        ],
        content_type: Annotated[
            str | None,
            Header(
                alias="Content-Type",
                description="必须以 text/plain 开头，建议 text/plain; charset=utf-8。",
            ),
        ] = None,
        if_match: Annotated[
            str | None,
            Header(alias="If-Match", description="此前完整读取响应中的强 ETag；必填。"),
        ] = None,
        caller: Principal = Depends(service_dependency),
    ) -> Response:
        return await workspace_proxy(
            method="PUT",
            subject_ref=subject_ref,
            session_id=session_id,
            path=path,
            caller=caller,
            content=content,
            content_type=content_type,
            if_match=if_match,
        )

    @app.delete(
        "/v1/instances/{subject_ref}/sessions/{session_id}/workspace/{path:path}",
        status_code=204,
        **api_doc(
            summary="条件删除文件",
            description=(
                "预览能力。删除 Session cwd 下的现有常规文件。必须发送此前完整读取返回的强 "
                "`If-Match` ETag；缺少 ETag 返回 428，文件已变化返回 412。\n\n"
                "当前不支持递归删除目录。需要 `workspace:write` scope。"
            ),
            tag="Workspace（工作区）",
            operation_id="delete_manager_workspace_file",
            response_description="删除成功，无响应体。",
        ),
    )
    async def delete_workspace_file(
        subject_ref: str,
        session_id: str,
        path: str,
        if_match: Annotated[
            str | None,
            Header(alias="If-Match", description="此前完整读取响应中的强 ETag；必填。"),
        ] = None,
        caller: Principal = Depends(service_dependency),
    ) -> Response:
        return await workspace_proxy(
            method="DELETE",
            subject_ref=subject_ref,
            session_id=session_id,
            path=path,
            caller=caller,
            if_match=if_match,
        )

    async def command_proxy(
        *,
        method: str,
        subject_ref: str,
        session_id: str,
        command_id: str,
        caller: Principal,
        payload: dict[str, Any] | None = None,
    ) -> Response:
        caller.require("commands:execute")
        connection, binding = _session_connection(
            database, cipher, caller, subject_ref, session_id
        )
        path = "/commands" + (f"/{command_id}" if command_id else "")
        kwargs: dict[str, Any] = {}
        if method == "POST":
            body = dict(payload or {})
            body["cwd"] = binding.cwd
            body["background"] = True
            kwargs["json"] = body
        client = BridgeClient(settings, connection.bridge_url, connection.bridge_token)
        try:
            upstream = await asyncio.to_thread(
                client.passthrough, method, path, **kwargs
            )
        except UpstreamProblem as exc:
            raise as_manager_problem(exc) from exc
        finally:
            client.close()
        return Response(
            upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "application/json"),
        )

    @app.post(
        "/v1/instances/{subject_ref}/sessions/{session_id}/commands",
        status_code=202,
        **api_doc(
            summary="执行后台命令",
            description=(
                "预览能力。在 Session cwd 中通过 OpenSandbox Execd 执行 shell 命令，不进入 Pi "
                "对话上下文。Manager 会忽略请求中的 `cwd` 和 `background`，强制使用 Session cwd "
                "并以后台模式执行。\n\n"
                "命令进程拥有 Runner 容器的文件权限，workspace 前缀不是安全沙箱；调用方必须限制"
                "谁能提交命令。响应中的 command ID 用于查询或中止。需要 "
                "`commands:execute` scope。"
            ),
            tag="Command（后台命令）",
            operation_id="create_manager_command",
            response_description="OpenSandbox 接受后台命令后的 JSON。",
        ),
    )
    async def create_command(
        payload: CommandCreate,
        subject_ref: str,
        session_id: str,
        caller: Principal = Depends(service_dependency),
    ) -> Response:
        return await command_proxy(
            method="POST",
            subject_ref=subject_ref,
            session_id=session_id,
            command_id="",
            caller=caller,
            payload=payload.model_dump(exclude_none=True),
        )

    @app.get(
        "/v1/instances/{subject_ref}/sessions/{session_id}/commands/{command_id}",
        **api_doc(
            summary="查询后台命令状态",
            description=(
                "查询此前后台执行命令的状态和退出结果。command_id 来自执行命令响应。"
                "当前 Manager 尚未暴露独立日志游标接口；"
                "返回结构由 Bridge/OpenSandbox 协议决定。\n\n"
                "需要 `commands:execute` scope。"
            ),
            tag="Command（后台命令）",
            operation_id="get_manager_command",
            response_description="后台命令状态 JSON。",
        ),
    )
    async def get_command(
        subject_ref: str,
        session_id: str,
        command_id: str,
        caller: Principal = Depends(service_dependency),
    ) -> Response:
        return await command_proxy(
            method="GET",
            subject_ref=subject_ref,
            session_id=session_id,
            command_id=command_id,
            caller=caller,
        )

    @app.delete(
        "/v1/instances/{subject_ref}/sessions/{session_id}/commands/{command_id}",
        status_code=202,
        **api_doc(
            summary="中止后台命令",
            description=(
                "请求 OpenSandbox 中止指定后台命令。返回 202 只表示中止请求已接受；调用方应继续查询"
                "命令状态确认终态。command_id 来自执行命令响应。\n\n"
                "需要 `commands:execute` scope。"
            ),
            tag="Command（后台命令）",
            operation_id="cancel_manager_command",
            response_description="OpenSandbox 接受中止请求后的响应。",
        ),
    )
    async def cancel_command(
        subject_ref: str,
        session_id: str,
        command_id: str,
        caller: Principal = Depends(service_dependency),
    ) -> Response:
        return await command_proxy(
            method="DELETE",
            subject_ref=subject_ref,
            session_id=session_id,
            command_id=command_id,
            caller=caller,
        )


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
