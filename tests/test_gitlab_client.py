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


def test_init_prefers_api_mode_when_pat_set():
    """With a PAT and httpx available, prefer the API path even if glab is auth'd."""
    from link_project_to_chat import gitlab_client
    with patch("link_project_to_chat.gitlab_client._glab_available", return_value=True):
        client = gitlab_client.GitLabClient(pat="glpat-test")
    assert client._use_glab is False
    assert client._client is not None


def test_init_uses_glab_when_no_pat_and_glab_available():
    from link_project_to_chat import gitlab_client
    with patch("link_project_to_chat.gitlab_client._glab_available", return_value=True):
        client = gitlab_client.GitLabClient(pat="")
    assert client._use_glab is True
    assert client._client is None


def test_init_raises_when_no_pat_and_no_glab():
    from link_project_to_chat import gitlab_client
    with patch("link_project_to_chat.gitlab_client._glab_available", return_value=False):
        with pytest.raises(ValueError, match="GitLab PAT required"):
            gitlab_client.GitLabClient(pat="")


def test_init_raises_when_no_httpx_and_no_glab(monkeypatch):
    from link_project_to_chat import gitlab_client
    monkeypatch.setattr(gitlab_client, "httpx", None)
    with patch("link_project_to_chat.gitlab_client._glab_available", return_value=False):
        with pytest.raises(ImportError, match="Neither glab CLI nor httpx"):
            gitlab_client.GitLabClient(pat="glpat-test")


def test_init_custom_host_used_for_api_base_url():
    from link_project_to_chat import gitlab_client
    with patch("link_project_to_chat.gitlab_client._glab_available", return_value=False):
        client = gitlab_client.GitLabClient(pat="glpat-test", host="gitlab.example.com")
    assert str(client._client.base_url).rstrip("/") == "https://gitlab.example.com/api/v4"


async def test_close_is_idempotent_when_glab_mode():
    from link_project_to_chat import gitlab_client
    with patch("link_project_to_chat.gitlab_client._glab_available", return_value=True):
        client = gitlab_client.GitLabClient(pat="")
    await client.close()  # must not raise
