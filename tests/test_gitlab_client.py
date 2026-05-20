"""GitLab client — module-level helpers."""
from __future__ import annotations

import asyncio
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def test_glab_available_when_not_on_path():
    from link_project_to_chat import gitlab_client
    with patch("link_project_to_chat.gitlab_client.shutil.which", return_value=None):
        assert gitlab_client._glab_available() is False


def test_glab_available_when_not_authenticated():
    from link_project_to_chat import gitlab_client
    fake_proc = MagicMock(returncode=1)
    with patch("link_project_to_chat.gitlab_client.shutil.which", return_value="/usr/bin/glab"), \
         patch("link_project_to_chat.gitlab_client.subprocess.run", return_value=fake_proc):
        assert gitlab_client._glab_available() is False


def test_glab_available_when_ready():
    from link_project_to_chat import gitlab_client
    fake_proc = MagicMock(returncode=0)
    with patch("link_project_to_chat.gitlab_client.shutil.which", return_value="/usr/bin/glab"), \
         patch("link_project_to_chat.gitlab_client.subprocess.run", return_value=fake_proc):
        assert gitlab_client._glab_available() is True


def test_redact_secrets_redacts_raw_pat():
    from link_project_to_chat.gitlab_client import _redact_secrets
    out = _redact_secrets("error: glpat-abc123", "glpat-abc123", host="gitlab.com")
    assert "glpat-abc123" not in out
    assert "[REDACTED]" in out


def test_redact_secrets_redacts_base64_form():
    """Defense-in-depth: redact base64(x-access-token:PAT) even though GitLab uses Bearer."""
    import base64
    from link_project_to_chat.gitlab_client import _redact_secrets
    encoded = base64.b64encode(b"x-access-token:glpat-abc").decode()
    out = _redact_secrets(f"leak: {encoded}", "glpat-abc", host="gitlab.com")
    assert encoded not in out


def test_redact_secrets_redacts_credential_url_default_host():
    from link_project_to_chat.gitlab_client import _redact_secrets
    out = _redact_secrets(
        "fatal: https://glpat-x@gitlab.com/g/p.git", "glpat-x", host="gitlab.com"
    )
    assert "glpat-x" not in out
    assert "[REDACTED]@gitlab.com" in out


def test_redact_secrets_redacts_credential_url_self_hosted():
    from link_project_to_chat.gitlab_client import _redact_secrets
    out = _redact_secrets(
        "fatal: https://glpat-x@gitlab.example.com/g/p.git",
        "glpat-x",
        host="gitlab.example.com",
    )
    assert "glpat-x" not in out
    assert "[REDACTED]@gitlab.example.com" in out
