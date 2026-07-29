from __future__ import annotations

import uvicorn

from .config import ManagerSettings


def main() -> None:
    settings = ManagerSettings.from_env()
    uvicorn.run(
        "pi_opensandbox_runner.manager.app:create_manager_app",
        factory=True,
        host=settings.host,
        port=settings.port,
    )


if __name__ == "__main__":
    main()
