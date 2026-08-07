from __future__ import annotations

import json
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from pi_opensandbox_manager.catalog_config import CatalogConfigError, load_catalog, sync_catalog
from pi_opensandbox_manager.config import ManagerSettings
from pi_opensandbox_manager.database import ManagerDatabase
from pi_opensandbox_manager.models import ModelDeployment, RunnerPolicy


def write_catalog(
    path: Path,
    *,
    label: str = "Coding",
    input: list[str] | None = None,
    policies: list[dict] | None = None,
) -> None:
    document = {
        "version": 1,
        "models": [
            {
                "slug": "coding-default",
                "label": label,
                "provider_model": "provider/coding",
                "api": "openai-completions",
                "context_window": 1000,
                "max_tokens": 100,
                "reasoning": True,
                "input": input if input is not None else ["text"],
            }
        ],
        "policies": policies
        if policies is not None
        else [
            {
                "slug": "default",
                "label": "Default",
                "model_slugs": ["coding-default"],
                "default_model_slug": "coding-default",
                "cpu": "1",
                "memory": "1Gi",
                "max_active_sessions": 1,
                "max_budget": 1,
                "budget_duration": "24h",
                "rpm_limit": 1,
                "tpm_limit": 100,
                "max_parallel_requests": 1,
                "egress_domains": ["pypi.org"],
            }
        ],
    }
    lines = ["version = 1", ""]
    for table_name in ("models", "policies"):
        for item in document[table_name]:
            lines.append(f"[[{table_name}]]")
            lines.extend(f"{key} = {json.dumps(value)}" for key, value in item.items())
            lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def test_catalog_sync_versions_models_and_policies(tmp_path: Path) -> None:
    path = tmp_path / "catalog.toml"
    write_catalog(path)
    database = ManagerDatabase(
        ManagerSettings(
            database_url=f"sqlite:///{tmp_path / 'manager.db'}",
            credential_encryption_key=Fernet.generate_key().decode(),
        )
    )
    database.initialize()
    with database.session() as db:
        assert sync_catalog(db, load_catalog(path))
        assert not sync_catalog(db, load_catalog(path))
        model = db.query(ModelDeployment).filter_by(slug="coding-default", revision=1).one()
        policy = db.query(RunnerPolicy).filter_by(slug="default", revision=1).one()
        assert model.state == "published"
        assert json.loads(model.input_json) == ["text"]
        assert policy.state == "published"

    write_catalog(path, label="Coding v2")
    with database.session() as db:
        assert sync_catalog(db, load_catalog(path))
        model_one = db.query(ModelDeployment).filter_by(slug="coding-default", revision=1).one()
        model_two = db.query(ModelDeployment).filter_by(slug="coding-default", revision=2).one()
        policy_one = db.query(RunnerPolicy).filter_by(slug="default", revision=1).one()
        policy_two = db.query(RunnerPolicy).filter_by(slug="default", revision=2).one()
        assert (model_one.state, model_two.state) == ("superseded", "published")
        assert (policy_one.state, policy_two.state) == ("superseded", "published")
        assert json.loads(policy_two.model_revisions_json) == {"coding-default": 2}
    database.dispose()


def test_catalog_input_capability_creates_model_revision(tmp_path: Path) -> None:
    path = tmp_path / "catalog.toml"
    write_catalog(path)
    database = ManagerDatabase(
        ManagerSettings(
            database_url=f"sqlite:///{tmp_path / 'manager.db'}",
            credential_encryption_key=Fernet.generate_key().decode(),
        )
    )
    database.initialize()
    with database.session() as db:
        assert sync_catalog(db, load_catalog(path))
    write_catalog(path, input=["text", "image"])
    with database.session() as db:
        assert sync_catalog(db, load_catalog(path))
        model = db.query(ModelDeployment).filter_by(slug="coding-default", revision=2).one()
        assert json.loads(model.input_json) == ["text", "image"]
    database.dispose()


@pytest.mark.parametrize("input", [[], ["image"], ["text", "text"], ["audio"]])
def test_catalog_rejects_invalid_input_modalities(tmp_path: Path, input: list[str]) -> None:
    path = tmp_path / "catalog.toml"
    write_catalog(path, input=input)
    with pytest.raises(CatalogConfigError):
        load_catalog(path)


def test_catalog_sync_retires_removed_policy(tmp_path: Path) -> None:
    path = tmp_path / "catalog.toml"
    write_catalog(path)
    database = ManagerDatabase(
        ManagerSettings(
            database_url=f"sqlite:///{tmp_path / 'manager.db'}",
            credential_encryption_key=Fernet.generate_key().decode(),
        )
    )
    database.initialize()
    with database.session() as db:
        sync_catalog(db, load_catalog(path))
    write_catalog(path, policies=[])
    with database.session() as db:
        assert sync_catalog(db, load_catalog(path))
        assert db.query(RunnerPolicy).filter_by(slug="default").one().state == "retired"
    database.dispose()


def test_catalog_rejects_invalid_egress_domain(tmp_path: Path) -> None:
    path = tmp_path / "catalog.toml"
    write_catalog(path)
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            'egress_domains = ["pypi.org"]',
            'egress_domains = ["https://pypi.org"]',
        ),
        encoding="utf-8",
    )
    with pytest.raises(CatalogConfigError, match="invalid egress domain"):
        load_catalog(path)
