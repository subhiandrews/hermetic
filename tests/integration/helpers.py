"""What the Docker tests share."""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest


def docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    done = subprocess.run(["docker", "info"], capture_output=True, check=False, timeout=30)
    return done.returncode == 0


def require_docker() -> None:
    """Skip without Docker on a developer machine; fail in CI, where a green skip tests nothing."""
    if docker_ready():
        return
    if os.environ.get("CI"):
        pytest.fail("Docker is not available on this CI runner")
    pytest.skip("Docker is not running")
