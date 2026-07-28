from __future__ import annotations

import uvicorn


def main() -> None:
    uvicorn.run(
        "pi_opensandbox_runner.app:create_app",
        factory=True,
        host="0.0.0.0",
        port=8765,
        # Source-address access control is part of the bridge boundary.  Do not
        # replace the TCP peer with a caller-controlled forwarded header.
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
