from __future__ import annotations

import uvicorn


def main() -> None:
    uvicorn.run(
        "pi_opensandbox_runner.app:create_app",
        factory=True,
        host="0.0.0.0",
        port=8765,
        proxy_headers=True,
    )


if __name__ == "__main__":
    main()
