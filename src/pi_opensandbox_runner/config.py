from __future__ import annotations

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
    default_provider: str | None = None
    default_model: str | None = None
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
            default_provider=os.getenv("PI_DEFAULT_PROVIDER") or None,
            default_model=os.getenv("PI_DEFAULT_MODEL") or None,
            max_active_sessions=int(os.getenv("PI_MAX_ACTIVE_SESSIONS", "4")),
            idle_timeout_seconds=float(os.getenv("PI_IDLE_TIMEOUT_SECONDS", "300")),
            rpc_timeout_seconds=float(os.getenv("PI_RPC_TIMEOUT_SECONDS", "30")),
            stop_grace_seconds=float(os.getenv("PI_STOP_GRACE_SECONDS", "10")),
            mcp_allow_insecure_http=os.getenv("MCP_ALLOW_INSECURE_HTTP", "").lower()
            in {"1", "true", "yes"},
        )
