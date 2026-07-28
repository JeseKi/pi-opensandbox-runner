from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


class ModelCatalogError(RuntimeError):
    def __init__(self, status_code: int, code: str, message: str):
        self.status_code = status_code
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class CatalogSnapshot:
    config: dict[str, Any]
    models: list[str]
    fingerprint: str


class ModelCatalog:
    """Safe, atomic management of the Pi LiteLLM-only models.json file."""

    _provider_template = {
        "baseUrl": "http://litellm:4000/v1",
        "api": "openai-completions",
        "apiKey": "$LITELLM_VIRTUAL_KEY",
        "authHeader": True,
    }
    _provider_keys = frozenset({*_provider_template, "models"})
    _model_keys = frozenset(
        {"id", "name", "reasoning", "input", "contextWindow", "maxTokens", "cost", "compat"}
    )

    def __init__(self, path: Path, *, litellm_api_base: str, virtual_key: str):
        self.path = path
        self.litellm_api_base = litellm_api_base.rstrip("/")
        self.virtual_key = virtual_key
        self._lock = asyncio.Lock()

    @staticmethod
    def _fingerprint(payload: bytes) -> str:
        return hashlib.sha256(payload).hexdigest()

    @classmethod
    def _decode(cls, value: dict[str, Any]) -> CatalogSnapshot:
        cls._validate_structure(value)
        models = [str(model["id"]) for model in value["providers"]["litellm"]["models"]]
        payload = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        return CatalogSnapshot(
            config=value, models=sorted(models), fingerprint=cls._fingerprint(payload)
        )

    @classmethod
    def _validate_structure(cls, value: dict[str, Any]) -> None:
        if set(value) != {"providers"} or not isinstance(value["providers"], dict):
            raise ModelCatalogError(
                422, "invalid_model_catalog", "models.json must contain only providers"
            )
        providers = value["providers"]
        if set(providers) != {"litellm"} or not isinstance(providers["litellm"], dict):
            raise ModelCatalogError(
                422, "unsafe_model_catalog", "only the litellm provider may be configured"
            )
        provider = providers["litellm"]
        if set(provider) != cls._provider_keys or any(
            provider.get(key) != expected for key, expected in cls._provider_template.items()
        ):
            raise ModelCatalogError(
                422,
                "unsafe_model_catalog",
                "LiteLLM endpoint and authentication fields must match the sandbox template",
            )
        models = provider.get("models")
        if not isinstance(models, list) or not models:
            raise ModelCatalogError(422, "invalid_model_catalog", "at least one model is required")
        ids: set[str] = set()
        for model in models:
            if not isinstance(model, dict) or not set(model).issubset(cls._model_keys):
                raise ModelCatalogError(
                    422, "unsafe_model_catalog", "model entries contain unsupported fields"
                )
            model_id = model.get("id")
            if not isinstance(model_id, str) or not model_id.strip() or len(model_id) > 240:
                raise ModelCatalogError(
                    422, "invalid_model_catalog", "each model requires a valid id"
                )
            if model_id in ids:
                raise ModelCatalogError(422, "invalid_model_catalog", "model ids must be unique")
            ids.add(model_id)
        if "coding-default" not in ids:
            raise ModelCatalogError(
                422, "default_model_missing", "coding-default must remain in the model catalog"
            )

    async def read(self) -> CatalogSnapshot:
        try:
            content = await asyncio.to_thread(self.path.read_text, encoding="utf-8")
            value = json.loads(content)
        except (OSError, json.JSONDecodeError) as exc:
            raise ModelCatalogError(503, "model_catalog_unavailable", str(exc)) from exc
        if not isinstance(value, dict):
            raise ModelCatalogError(
                503, "model_catalog_unavailable", "models.json must be an object"
            )
        return self._decode(value)

    async def _authorized_models(self) -> set[str]:
        if not self.virtual_key:
            raise ModelCatalogError(
                503, "litellm_unavailable", "sandbox LiteLLM virtual key is missing"
            )
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(
                    f"{self.litellm_api_base}/models",
                    headers={"Authorization": f"Bearer {self.virtual_key}"},
                )
        except httpx.HTTPError as exc:
            raise ModelCatalogError(
                503, "litellm_unavailable", f"LiteLLM is unavailable: {exc}"
            ) from exc
        if not response.is_success:
            raise ModelCatalogError(
                503, "litellm_unavailable", f"LiteLLM returned HTTP {response.status_code}"
            )
        try:
            data = response.json().get("data", [])
            return {str(item["id"]) for item in data if isinstance(item, dict) and "id" in item}
        except (TypeError, ValueError) as exc:
            raise ModelCatalogError(
                503, "litellm_unavailable", "LiteLLM returned an invalid model list"
            ) from exc

    async def replace(
        self, value: dict[str, Any], *, if_match: str | None = None
    ) -> CatalogSnapshot:
        candidate = self._decode(value)
        async with self._lock:
            current = await self.read()
            if if_match is not None and if_match != current.fingerprint:
                raise ModelCatalogError(409, "model_catalog_conflict", "model catalog has changed")
            authorized = await self._authorized_models()
            missing = sorted(set(candidate.models) - authorized)
            if missing:
                raise ModelCatalogError(
                    422,
                    "model_not_authorized",
                    f"current LiteLLM virtual key cannot access: {', '.join(missing)}",
                )
            serialized = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
            await asyncio.to_thread(self._atomic_write, serialized)
            return candidate

    def _atomic_write(self, content: bytes) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".models.", dir=self.path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
