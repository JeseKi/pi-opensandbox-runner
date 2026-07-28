from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Header

from ..model_catalog import ModelCatalogError
from ..schemas import ModelCatalogConfigOut, ModelCatalogReplaceOut
from .context import BridgeContext
from .problems import ApiProblem


def register_model_routes(router: APIRouter, ctx: BridgeContext) -> None:
    @router.get("/models/config", response_model=ModelCatalogConfigOut, summary="读取 Pi 模型目录")
    async def get_model_config() -> ModelCatalogConfigOut:
        try:
            snapshot = await ctx.model_catalog.read()
        except ModelCatalogError as exc:
            raise ApiProblem(exc.status_code, exc.code, str(exc)) from exc
        return ModelCatalogConfigOut(
            config=snapshot.config, models=snapshot.models, fingerprint=snapshot.fingerprint
        )

    @router.put(
        "/models/config",
        response_model=ModelCatalogReplaceOut,
        summary="原子替换 Pi 模型目录",
        description="仅接受 LiteLLM 私网模型目录；会校验当前虚拟 Key 的模型权限。",
    )
    async def replace_model_config(
        payload: dict[str, Any],
        if_match: Annotated[str | None, Header()] = None,
    ) -> ModelCatalogReplaceOut:
        try:
            snapshot = await ctx.model_catalog.replace(payload, if_match=if_match)
        except ModelCatalogError as exc:
            raise ApiProblem(exc.status_code, exc.code, str(exc)) from exc
        migrated = await ctx.catalog.migrate_removed_models(
            set(snapshot.models), default_model="coding-default"
        )
        for session_id in migrated:
            await ctx.journal.append(
                session_id,
                "bridge",
                {"type": "model_migrated", "model": "coding-default"},
            )
        return ModelCatalogReplaceOut(
            config=snapshot.config,
            models=snapshot.models,
            fingerprint=snapshot.fingerprint,
            migrated_session_count=len(migrated),
        )
