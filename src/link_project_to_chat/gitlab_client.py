"""GitLab client — mirrors github_client.py.

Prefers the `glab` CLI when installed + authenticated; falls back to
`httpx + GITLAB_TOKEN`. Used by the manager wizard's create-project /
create-team flows when the operator picks GitLab in the provider picker.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]

from .repo_provider import RepoInfo

logger = logging.getLogger(__name__)


def _glab_available() -> bool:
    """Check if glab CLI is installed and authenticated."""
    glab_path = shutil.which("glab")
    if glab_path is None:
        return False
    try:
        proc = subprocess.run(
            [glab_path, "auth", "status"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


async def _run_glab(*args: str) -> tuple[int, str, str]:
    """Run a glab CLI command and return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "glab", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode().strip(), stderr.decode().strip()


def _redact_secrets(text: str, *secrets: str, host: str) -> str:
    """Strip raw PATs, their base64 forms, and credential-URL forms from text."""
    redacted = text
    for secret in secrets:
        if not secret:
            continue
        redacted = redacted.replace(secret, "[REDACTED]")
        encoded = base64.b64encode(f"x-access-token:{secret}".encode()).decode()
        redacted = redacted.replace(encoded, "[REDACTED]")
    cred_url_re = re.compile(rf"https://[^/@\s]+@{re.escape(host)}")
    redacted = cred_url_re.sub(f"https://[REDACTED]@{host}", redacted)
    return redacted
