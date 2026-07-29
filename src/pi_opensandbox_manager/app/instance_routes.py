from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, Path, status

from ..crypto import CredentialCipher
from ..database import ManagerDatabase
from ..models import ManagerOperation, RunnerPolicy
from ..openapi_docs import api_doc
from ..problems import ManagerProblem
from ..schemas import AcceptedOperation, InstanceEnsure, InstanceOut, OperationOut
from ..security import Principal
from ..service.short_transactions import enqueue_instance_operation, ensure_instance
from .helpers import _instance_operation, _instance_out, _operation_out, _owned_instance


def register_instance_routes(
    app: FastAPI,
    db_control: ManagerDatabase,
    cipher: CredentialCipher,
    service_principal: Any,
) -> None:

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

