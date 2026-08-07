from __future__ import annotations

import json
from typing import Any

from fastapi import Depends, FastAPI
from sqlalchemy import select

from ..clients import BridgeClient, LiteLLMAdminClient, UpstreamProblem, as_manager_problem
from ..config import ManagerSettings
from ..crypto import CredentialCipher
from ..database import ManagerDatabase
from ..models import RunnerInstance, RunnerPolicy
from ..problems import ManagerProblem
from ..schemas import (
    McpMutationOut,
    McpReloadOut,
    McpServerCreateIn,
    McpServerOut,
    McpServerUpdateIn,
)
from ..security import Principal


def _out(value: dict[str, Any]) -> McpServerOut:
    credentials = value.get("credentials")
    auth_value = credentials.get("auth_value") if isinstance(credentials, dict) else None
    transport = "streamable_http" if value.get("transport") == "http" else "sse"
    return McpServerOut(
        server_id=str(value.get("server_id") or ""),
        label=str(value.get("description") or value.get("server_name") or ""),
        url=str(value["url"]) if value.get("url") else None,
        transport=transport,
        auth_type=value.get("auth_type"),
        allowed_tools=value.get("allowed_tools"),
        credential_configured=bool(auth_value) or value.get("auth_type") not in {None, "none"},
    )


def _used_by_active_instance(database: ManagerDatabase, server_id: str) -> bool:
    with database.session() as db:
        policies = list(db.scalars(select(RunnerPolicy)))
        policy_ids = {
            policy.id
            for policy in policies
            if server_id in json.loads(policy.mcp_server_ids_json)
        }
        if not policy_ids:
            return False
        instance = db.scalar(
            select(RunnerInstance.id).where(
                RunnerInstance.policy_id.in_(policy_ids),
                RunnerInstance.state.not_in(("destroyed", "failed")),
            )
        )
        return instance is not None


def _payload_create(value: McpServerCreateIn) -> dict[str, Any]:
    # LiteLLM accepts hyphens in server_id, but validates server_name and alias
    # more strictly. Keep the policy-facing ID stable and normalize only those
    # internal display identifiers.
    litellm_name = value.server_id.replace("-", "_")
    payload: dict[str, Any] = {
        "server_id": value.server_id,
        "server_name": litellm_name,
        "alias": litellm_name,
        "description": value.label,
        "url": value.url,
        "transport": "http" if value.transport == "streamable_http" else "sse",
        "auth_type": value.auth.type,
        "allowed_tools": value.allowed_tools,
        "allow_all_keys": False,
        "available_on_public_internet": False,
    }
    if value.auth.value is not None:
        payload["credentials"] = {"auth_value": value.auth.value}
    return payload


def _payload_update(server_id: str, value: McpServerUpdateIn) -> dict[str, Any]:
    payload: dict[str, Any] = {"server_id": server_id}
    if value.label is not None:
        payload["description"] = value.label
    if value.url is not None:
        payload["url"] = value.url
    if value.transport is not None:
        payload["transport"] = "http" if value.transport == "streamable_http" else "sse"
    if value.allowed_tools is not None:
        payload["allowed_tools"] = value.allowed_tools
    if value.auth is not None:
        payload["auth_type"] = value.auth.type
        if value.auth.value is not None:
            payload["credentials"] = {"auth_value": value.auth.value}
    return payload


def register_mcp_routes(
    app: FastAPI,
    database: ManagerDatabase,
    settings: ManagerSettings,
    cipher: CredentialCipher,
    admin_dependency: Any,
) -> None:
    def litellm() -> LiteLLMAdminClient:
        return LiteLLMAdminClient(settings)

    def reload(server_id: str) -> McpReloadOut:
        refreshed: list[str] = []
        failed: list[str] = []
        with database.session() as db:
            policies = list(db.scalars(select(RunnerPolicy)))
            policy_ids = {
                policy.id
                for policy in policies
                if server_id in json.loads(policy.mcp_server_ids_json)
            }
            instances = list(
                db.scalars(
                    select(RunnerInstance).where(
                        RunnerInstance.policy_id.in_(policy_ids),
                        RunnerInstance.state == "ready",
                    )
                )
            ) if policy_ids else []
        for instance in instances:
            if not instance.bridge_url or not instance.bridge_token_encrypted:
                failed.append(instance.id)
                continue
            try:
                BridgeClient(
                    settings,
                    instance.bridge_url,
                    cipher.decrypt(instance.bridge_token_encrypted),
                ).reload_mcp_gateway()
                refreshed.append(instance.id)
            except UpstreamProblem:
                failed.append(instance.id)
        return McpReloadOut(refreshed_instances=refreshed, failed_instances=failed)

    @app.get(
        "/admin/v1/mcp/servers",
        response_model=list[McpServerOut],
        tags=["Admin（内部管理）"],
        summary="列出 LiteLLM MCP Server",
        description="返回由 LiteLLM MCP Gateway 管理的 Server；认证原文不会出现在响应中。",
    )
    async def list_servers(_: Principal = Depends(admin_dependency)) -> list[McpServerOut]:
        client = litellm()
        try:
            return [_out(item) for item in client.list_mcp_servers()]
        except UpstreamProblem as exc:
            raise as_manager_problem(exc) from exc
        finally:
            client.close()

    @app.post(
        "/admin/v1/mcp/servers",
        response_model=McpMutationOut,
        status_code=201,
        tags=["Admin（内部管理）"],
        summary="创建 LiteLLM MCP Server",
        description="创建远程 MCP Server，并将认证值仅提交给 LiteLLM 保存。",
    )
    async def create_server(
        payload: McpServerCreateIn,
        _: Principal = Depends(admin_dependency),
    ) -> McpMutationOut:
        client = litellm()
        try:
            created = client.create_mcp_server(_payload_create(payload))
        except UpstreamProblem as exc:
            raise as_manager_problem(exc) from exc
        finally:
            client.close()
        return McpMutationOut(server=_out(created), reload=McpReloadOut())

    @app.put(
        "/admin/v1/mcp/servers/{server_id}",
        response_model=McpMutationOut,
        tags=["Admin（内部管理）"],
        summary="更新 LiteLLM MCP Server",
        description=(
            "更新后会请求所有受影响的 ready Instance 在下一条 Prompt 前刷新 MCP Gateway 配置。"
        ),
    )
    async def update_server(
        server_id: str,
        payload: McpServerUpdateIn,
        _: Principal = Depends(admin_dependency),
    ) -> McpMutationOut:
        client = litellm()
        try:
            updated = client.update_mcp_server(_payload_update(server_id, payload))
        except UpstreamProblem as exc:
            raise as_manager_problem(exc) from exc
        finally:
            client.close()
        return McpMutationOut(server=_out(updated), reload=reload(server_id))

    @app.delete(
        "/admin/v1/mcp/servers/{server_id}",
        response_model=McpReloadOut,
        tags=["Admin（内部管理）"],
        summary="删除 LiteLLM MCP Server",
        description="活动 Runner Instance 的 Policy 仍引用该 Server 时会返回 409。",
    )
    async def delete_server(
        server_id: str,
        _: Principal = Depends(admin_dependency),
    ) -> McpReloadOut:
        if _used_by_active_instance(database, server_id):
            raise ManagerProblem(
                409,
                "mcp_server_in_use",
                "MCP server is referenced by an active runner instance",
            )
        client = litellm()
        try:
            client.delete_mcp_server(server_id)
        except UpstreamProblem as exc:
            raise as_manager_problem(exc) from exc
        finally:
            client.close()
        return reload(server_id)
