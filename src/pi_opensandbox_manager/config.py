from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ManagerSettings:
    database_url: str = "sqlite:///./data/runner-manager.db"
    opensandbox_base_url: str = "http://opensandbox:8080"
    opensandbox_api_key: str = ""
    litellm_base_url: str = "http://litellm:4000"
    litellm_master_key: str = ""
    credential_encryption_key: str = ""
    runner_image: str = "pi-opensandbox-runner:local"
    bootstrap_consumer_slug: str = "agent-runner"
    bootstrap_service_token: str = ""
    bootstrap_admin_token: str = ""
    catalog_path: Path = Path("config/runner-catalog.toml")
    catalog_reload_seconds: float = 5.0
    operation_poll_seconds: float = 0.5
    http_timeout_seconds: float = 30.0
    provision_timeout_seconds: float = 180.0
    sandbox_healthcheck_seconds: float = 60.0
    terminal_public_ws_url: str = "ws://127.0.0.1:8090/v1/terminal-connections"
    terminal_allowed_origins: str = ""
    terminal_ticket_ttl_seconds: int = 60
    terminal_detached_ttl_seconds: int = 900
    terminal_max_lifetime_seconds: int = 28_800
    max_terminals_per_instance: int = 4
    terminal_cleanup_seconds: float = 60.0
    docs_site_dir: Path = Path("site")
    host: str = "0.0.0.0"
    port: int = 8090

    @classmethod
    def from_env(cls) -> ManagerSettings:
        return cls(
            database_url=os.getenv(
                "RUNNER_MANAGER_DATABASE_URL",
                "sqlite:///./data/runner-manager.db",
            ),
            opensandbox_base_url=os.getenv(
                "OPENSANDBOX_BASE_URL", "http://opensandbox:8080"
            ).rstrip("/"),
            opensandbox_api_key=os.getenv("OPENSANDBOX_API_KEY", ""),
            litellm_base_url=os.getenv("LITELLM_BASE_URL", "http://litellm:4000").rstrip("/"),
            litellm_master_key=os.getenv("LITELLM_MASTER_KEY", ""),
            credential_encryption_key=os.getenv("RUNNER_MANAGER_CREDENTIAL_ENCRYPTION_KEY", ""),
            runner_image=os.getenv("PI_RUNNER_IMAGE", "pi-opensandbox-runner:local"),
            bootstrap_consumer_slug=os.getenv("RUNNER_MANAGER_BOOTSTRAP_CONSUMER", "agent-runner"),
            bootstrap_service_token=os.getenv("RUNNER_MANAGER_BOOTSTRAP_SERVICE_TOKEN", ""),
            bootstrap_admin_token=os.getenv("RUNNER_MANAGER_BOOTSTRAP_ADMIN_TOKEN", ""),
            catalog_path=Path(
                os.getenv("RUNNER_MANAGER_CATALOG_PATH", "config/runner-catalog.toml")
            ),
            catalog_reload_seconds=float(
                os.getenv("RUNNER_MANAGER_CATALOG_RELOAD_SECONDS", "5")
            ),
            operation_poll_seconds=float(os.getenv("RUNNER_MANAGER_OPERATION_POLL_SECONDS", "0.5")),
            http_timeout_seconds=float(os.getenv("RUNNER_MANAGER_HTTP_TIMEOUT_SECONDS", "30")),
            provision_timeout_seconds=float(
                os.getenv("RUNNER_MANAGER_PROVISION_TIMEOUT_SECONDS", "180")
            ),
            sandbox_healthcheck_seconds=float(
                os.getenv("RUNNER_MANAGER_SANDBOX_HEALTHCHECK_SECONDS", "60")
            ),
            terminal_public_ws_url=os.getenv(
                "RUNNER_MANAGER_TERMINAL_PUBLIC_WS_URL",
                "ws://127.0.0.1:8090/v1/terminal-connections",
            ),
            terminal_allowed_origins=os.getenv("RUNNER_MANAGER_TERMINAL_ALLOWED_ORIGINS", ""),
            terminal_ticket_ttl_seconds=int(
                os.getenv("RUNNER_MANAGER_TERMINAL_TICKET_TTL_SECONDS", "60")
            ),
            terminal_detached_ttl_seconds=int(
                os.getenv("RUNNER_MANAGER_TERMINAL_DETACHED_TTL_SECONDS", "900")
            ),
            terminal_max_lifetime_seconds=int(
                os.getenv("RUNNER_MANAGER_TERMINAL_MAX_LIFETIME_SECONDS", "28800")
            ),
            max_terminals_per_instance=int(
                os.getenv("RUNNER_MANAGER_MAX_TERMINALS_PER_INSTANCE", "4")
            ),
            terminal_cleanup_seconds=float(
                os.getenv("RUNNER_MANAGER_TERMINAL_CLEANUP_SECONDS", "60")
            ),
            docs_site_dir=Path(os.getenv("RUNNER_MANAGER_DOCS_SITE_DIR", "site")),
            host=os.getenv("RUNNER_MANAGER_HOST", "0.0.0.0"),
            port=int(os.getenv("RUNNER_MANAGER_PORT", "8090")),
        )

    @property
    def sqlite_path(self) -> Path | None:
        prefix = "sqlite:///"
        if not self.database_url.startswith(prefix):
            return None
        return Path(self.database_url.removeprefix(prefix))

    @property
    def allowed_terminal_origins(self) -> frozenset[str]:
        return frozenset(
            item.strip().rstrip("/")
            for item in self.terminal_allowed_origins.split(",")
            if item.strip()
        )
