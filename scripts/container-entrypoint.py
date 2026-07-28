#!/usr/bin/env python3
"""Prepare an ephemeral Pi configuration before starting the bridge."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def main() -> None:
    config_dir = Path(os.environ.setdefault("PI_CODING_AGENT_DIR", "/run/pi-agent"))
    config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    source = Path("/opt/pi-runner/pi-models.json")
    persistent = Path(
        os.environ.setdefault("PI_MODEL_CATALOG_PATH", "/root/.pi/bridge/models.json")
    )
    persistent.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not persistent.exists():
        shutil.copyfile(source, persistent)
        persistent.chmod(0o600)
    destination = config_dir / "models.json"
    destination.unlink(missing_ok=True)
    destination.symlink_to(persistent)
    os.execvp("python", ["python", "-m", "pi_opensandbox_runner", *sys.argv[1:]])


if __name__ == "__main__":
    main()
