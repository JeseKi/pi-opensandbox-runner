from __future__ import annotations

from pathlib import Path

import pytest

from pi_opensandbox_runner.model_catalog import ModelCatalog, ModelCatalogError


def catalog_document(*model_ids: str) -> dict[str, object]:
    return {
        "providers": {
            "litellm": {
                "baseUrl": "http://litellm:4000/v1",
                "api": "openai-completions",
                "apiKey": "$LITELLM_VIRTUAL_KEY",
                "authHeader": True,
                "models": [{"id": model_id} for model_id in model_ids],
            }
        }
    }


@pytest.mark.asyncio
async def test_replace_is_atomic_and_checks_key_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "models.json"
    path.write_text(__import__("json").dumps(catalog_document("coding-default")))
    catalog = ModelCatalog(path, litellm_api_base="http://litellm:4000/v1", virtual_key="test")

    async def authorized() -> set[str]:
        return {"coding-default", "gpt-5.6-terra"}

    monkeypatch.setattr(catalog, "_authorized_models", authorized)
    current = await catalog.read()
    replaced = await catalog.replace(
        catalog_document("coding-default", "gpt-5.6-terra"), if_match=current.fingerprint
    )
    assert replaced.models == ["coding-default", "gpt-5.6-terra"]
    assert (await catalog.read()).fingerprint == replaced.fingerprint

    with pytest.raises(ModelCatalogError, match="cannot access"):
        await catalog.replace(catalog_document("coding-default", "not-authorized"))
    assert (await catalog.read()).models == ["coding-default", "gpt-5.6-terra"]


@pytest.mark.asyncio
async def test_rejects_provider_escape(tmp_path: Path) -> None:
    catalog = ModelCatalog(
        tmp_path / "models.json", litellm_api_base="http://litellm", virtual_key="test"
    )
    unsafe = catalog_document("coding-default")
    unsafe["providers"]["litellm"]["baseUrl"] = "https://api.openai.com/v1"  # type: ignore[index]
    with pytest.raises(ModelCatalogError, match="endpoint"):
        await catalog.replace(unsafe)
