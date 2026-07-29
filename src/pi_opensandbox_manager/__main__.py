from __future__ import annotations

import uvicorn

from .config import ManagerSettings


def main() -> None:
    settings = ManagerSettings.from_env()
    uvicorn.run(
        "pi_opensandbox_manager.app:create_manager_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        ws_max_size=65_536,
        ws_per_message_deflate=False,
    )


if __name__ == "__main__":
    main()
