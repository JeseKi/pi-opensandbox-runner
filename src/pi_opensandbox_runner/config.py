from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    api_token: str
    state_root: Path = Path("/root/.pi/bridge")
    pi_session_dir: Path = Path("/root/.pi/agent/sessions")
    workspace_root: Path = Path("/root/workspace")
    pi_executable: str = "pi"
    execd_url: str = "http://127.0.0.1:44772"
    default_model: str | None = "coding-default"
    model_catalog_path: Path = Path("/root/.pi/bridge/models.json")
    litellm_api_base: str = "http://litellm:4000/v1"
    litellm_virtual_key: str = ""
    max_active_sessions: int = 4
    idle_timeout_seconds: float = 300.0
    rpc_timeout_seconds: float = 30.0
    stop_grace_seconds: float = 10.0
    event_segment_bytes: int = 8 * 1024 * 1024
    event_segment_count: int = 8
    mcp_allow_insecure_http: bool = False

    @classmethod
    def from_env(cls) -> Settings:
        token = os.getenv("BRIDGE_API_TOKEN", "")
        if not token:
            raise RuntimeError("BRIDGE_API_TOKEN is required")
        return cls(
            api_token=token,
            state_root=Path(os.getenv("BRIDGE_STATE_ROOT", "/root/.pi/bridge")),
            pi_session_dir=Path(
                os.getenv("PI_CODING_AGENT_SESSION_DIR", "/root/.pi/agent/sessions")
            ),
            workspace_root=Path(os.getenv("PI_WORKSPACE_ROOT", "/root/workspace")),
            pi_executable=os.getenv("PI_EXECUTABLE", "pi"),
            execd_url=os.getenv("OPENSANDBOX_EXECD_URL", "http://127.0.0.1:44772"),
            default_model=os.getenv("PI_DEFAULT_MODEL", "coding-default"),
            model_catalog_path=Path(
                os.getenv("PI_MODEL_CATALOG_PATH", "/root/.pi/bridge/models.json")
            ),
            litellm_api_base=os.getenv("LITELLM_API_BASE", "http://litellm:4000/v1"),
            litellm_virtual_key=os.getenv("LITELLM_VIRTUAL_KEY", ""),
            max_active_sessions=int(os.getenv("PI_MAX_ACTIVE_SESSIONS", "4")),
            idle_timeout_seconds=float(os.getenv("PI_IDLE_TIMEOUT_SECONDS", "300")),
            rpc_timeout_seconds=float(os.getenv("PI_RPC_TIMEOUT_SECONDS", "30")),
            stop_grace_seconds=float(os.getenv("PI_STOP_GRACE_SECONDS", "10")),
            mcp_allow_insecure_http=os.getenv("MCP_ALLOW_INSECURE_HTTP", "").lower()
            in {"1", "true", "yes"},
        )

    def allowed_models(self) -> set[str]:
        try:
            document = json.loads(self.model_catalog_path.read_text())
            providers = document.get("providers", {})
            models = providers.get("litellm", {}).get("models", [])
            return {
                str(model["id"])
                for model in models
                if isinstance(model, dict) and "id" in model
            }
        except (OSError, ValueError, TypeError):
            return set()
