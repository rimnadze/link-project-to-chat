"""GitLab client — module-level helpers."""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def test_glab_available_when_not_on_path():
    from link_project_to_chat import gitlab_client
    with patch("link_project_to_chat.gitlab_client.shutil.which", return_value=None):
        assert gitlab_client._glab_available() is False


def test_glab_available_when_not_authenticated():
    from link_project_to_chat import gitlab_client
    fake_proc = MagicMock(returncode=1, stdout=b"", stderr=b"not logged in")
    with patch("link_project_to_chat.gitlab_client.shutil.which", return_value="/usr/bin/glab"), \
         patch("link_project_to_chat.gitlab_client.subprocess.run", return_value=fake_proc):
        assert gitlab_client._glab_available() is False


def test_glab_available_when_ready():
    from link_project_to_chat import gitlab_client
    fake_proc = MagicMock(returncode=0, stdout=b'{"id": 1, "username": "u"}', stderr=b"")
    with patch("link_project_to_chat.gitlab_client.shutil.which", return_value="/usr/bin/glab"), \
         patch("link_project_to_chat.gitlab_client.subprocess.run", return_value=fake_proc):
        assert gitlab_client._glab_available() is True


def test_glab_available_checks_specific_host():
    from link_project_to_chat import gitlab_client

    fake_proc = MagicMock(returncode=0, stdout=b'{"id": 1, "username": "u"}', stderr=b"")
    with patch("link_project_to_chat.gitlab_client.shutil.which", return_value="/usr/bin/glab"), \
         patch("link_project_to_chat.gitlab_client.subprocess.run", return_value=fake_proc) as mock_run:
        assert gitlab_client._glab_available(host="gitlab.example.com") is True

    assert mock_run.call_args.args[0] == [
        "/usr/bin/glab", "api", "--hostname", "gitlab.example.com", "user",
    ]


def test_glab_available_returns_false_when_api_user_returns_dict_message():
    """Invalid token: `glab api user` exits 0 but body is {'message': '401 ...'}.
    Must return False so the httpx fallback kicks in."""
    from link_project_to_chat import gitlab_client
    fake_proc = MagicMock(returncode=0, stdout=b'{"message": "401 Unauthorized"}', stderr=b"")
    with patch("link_project_to_chat.gitlab_client.shutil.which", return_value="/usr/bin/glab"), \
         patch("link_project_to_chat.gitlab_client.subprocess.run", return_value=fake_proc):
        assert gitlab_client._glab_available() is False


def test_glab_available_returns_true_when_api_user_returns_user_object():
    """Valid token: `glab api user` returns {'id': 123, 'username': '...'}. Must return True."""
    from link_project_to_chat import gitlab_client
    fake_proc = MagicMock(returncode=0, stdout=b'{"id": 42, "username": "alice"}', stderr=b"")
    with patch("link_project_to_chat.gitlab_client.shutil.which", return_value="/usr/bin/glab"), \
         patch("link_project_to_chat.gitlab_client.subprocess.run", return_value=fake_proc):
        assert gitlab_client._glab_available() is True


def test_glab_available_returns_false_when_api_user_empty_stdout():
    from link_project_to_chat import gitlab_client
    fake_proc = MagicMock(returncode=0, stdout=b"", stderr=b"")
    with patch("link_project_to_chat.gitlab_client.shutil.which", return_value="/usr/bin/glab"), \
         patch("link_project_to_chat.gitlab_client.subprocess.run", return_value=fake_proc):
        assert gitlab_client._glab_available() is False


def test_glab_available_returns_false_on_nonzero_exit():
    """No regression on the obvious case."""
    from link_project_to_chat import gitlab_client
    fake_proc = MagicMock(returncode=1, stdout=b"", stderr=b"not logged in")
    with patch("link_project_to_chat.gitlab_client.shutil.which", return_value="/usr/bin/glab"), \
         patch("link_project_to_chat.gitlab_client.subprocess.run", return_value=fake_proc):
        assert gitlab_client._glab_available() is False


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


def test_init_uses_gitlab_token_env_when_no_pat_and_no_glab(monkeypatch):
    from link_project_to_chat import gitlab_client

    monkeypatch.setenv("GITLAB_TOKEN", "glpat-env")
    with patch("link_project_to_chat.gitlab_client._glab_available", return_value=False):
        client = gitlab_client.GitLabClient(pat="")

    assert client._pat == "glpat-env"
    assert client._client.headers["PRIVATE-TOKEN"] == "glpat-env"


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


@pytest.fixture
def gl_api_client(monkeypatch):
    """Force httpx (API) mode."""
    from link_project_to_chat import gitlab_client
    monkeypatch.setattr(gitlab_client, "_glab_available", lambda: False)
    return gitlab_client.GitLabClient(pat="glpat-test123")


def _mock_resp(status_code, json_data, headers=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.headers = headers or {}
    return resp


async def test_list_repos_httpx_maps_fields(gl_api_client):
    projects = [
        {
            "path": "my-app",
            "path_with_namespace": "acme/my-app",
            "web_url": "https://gitlab.com/acme/my-app",
            "http_url_to_repo": "https://gitlab.com/acme/my-app.git",
            "description": "An app",
            "visibility": "private",
        },
        {
            "path": "lib",
            "path_with_namespace": "acme/team/lib",
            "web_url": "https://gitlab.com/acme/team/lib",
            "http_url_to_repo": "https://gitlab.com/acme/team/lib.git",
            "description": None,
            "visibility": "internal",
        },
        {
            "path": "open",
            "path_with_namespace": "acme/open",
            "web_url": "https://gitlab.com/acme/open",
            "http_url_to_repo": "https://gitlab.com/acme/open.git",
            "description": "",
            "visibility": "public",
        },
    ]
    with patch.object(gl_api_client, "_client") as mock_client:
        mock_client.get = AsyncMock(return_value=_mock_resp(200, projects, {"link": ""}))
        repos, has_next = await gl_api_client.list_repos(page=1, per_page=5)
    assert [r.full_name for r in repos] == ["acme/my-app", "acme/team/lib", "acme/open"]
    assert [r.name for r in repos] == ["my-app", "lib", "open"]
    assert [r.private for r in repos] == [True, True, False]  # internal counts as non-public
    assert repos[1].description == ""  # None coerced to ""
    assert has_next is False
    # Workaround: omit order_by entirely + keep simple=true to dodge a
    # GitLab.com /projects 500 bug triggered by deletion-pending projects in
    # the membership set (any value of order_by trips it; default sort is fine).
    params = mock_client.get.await_args.kwargs["params"]
    assert "order_by" not in params
    assert params["simple"] == "true"


async def test_list_repos_httpx_detects_next_page(gl_api_client):
    with patch.object(gl_api_client, "_client") as mock_client:
        mock_client.get = AsyncMock(return_value=_mock_resp(
            200, [],
            {"link": '<https://gitlab.com/api/v4/projects?page=2>; rel="next"'},
        ))
        _, has_next = await gl_api_client.list_repos(page=1, per_page=5)
    assert has_next is True


async def test_list_repos_httpx_auth_failure(gl_api_client):
    with patch.object(gl_api_client, "_client") as mock_client:
        mock_client.get = AsyncMock(return_value=_mock_resp(401, {"message": "Unauthorized"}))
        with pytest.raises(Exception, match="GitLab API error 401"):
            await gl_api_client.list_repos()


@pytest.fixture
def gl_glab_client(monkeypatch):
    """Force glab CLI mode (no PAT)."""
    from link_project_to_chat import gitlab_client
    monkeypatch.setattr(gitlab_client, "_glab_available", lambda host="gitlab.com": True)
    return gitlab_client.GitLabClient(pat="")


async def test_list_repos_glab_parses_link_header_and_body(gl_glab_client):
    import json
    body = json.dumps([
        {"path": "p1", "path_with_namespace": "u/p1", "web_url": "https://gitlab.com/u/p1",
         "http_url_to_repo": "https://gitlab.com/u/p1.git", "description": "", "visibility": "private"},
    ])
    headers = (
        'HTTP/2.0 200 OK\r\n'
        'Link: <https://gitlab.com/api/v4/projects?page=2>; rel="next"\r\n'
    )
    stdout = headers + "\r\n" + body
    with patch("link_project_to_chat.gitlab_client._run_glab",
               AsyncMock(return_value=(0, stdout, ""))) as mock_run:
        repos, has_next = await gl_glab_client.list_repos(page=1, per_page=5)
    assert [r.full_name for r in repos] == ["u/p1"]
    assert has_next is True
    args = mock_run.await_args.args
    assert args[0] == "api"
    assert "--include" in args
    assert any("projects" in a and "membership=true" in a for a in args)
    # Workaround: omit order_by entirely + keep simple=true to dodge a
    # GitLab.com /projects 500 bug triggered by deletion-pending projects in
    # the membership set (any value of order_by trips it; default sort is fine).
    assert all("order_by" not in a for a in args)
    assert any("simple=true" in a for a in args)


async def test_list_repos_omits_order_by_to_avoid_gitlab_bug(gl_api_client, gl_glab_client):
    """GitLab.com /projects returns HTTP 500 for ANY value of `order_by` when
    the membership set contains projects in `deletion_scheduled` state
    (account-state bug, observed 2026-05). We omit `order_by` entirely — the
    REST default (created_at desc) is fine for the wizard's paginated picker.

    This test pins the workaround so a future refactor doesn't silently
    reintroduce the parameter.
    """
    import json as _json

    # httpx path: params dict must NOT carry order_by.
    with patch.object(gl_api_client, "_client") as mock_client:
        mock_client.get = AsyncMock(return_value=_mock_resp(200, [], {"link": ""}))
        await gl_api_client.list_repos(page=1, per_page=5)
    params = mock_client.get.await_args.kwargs["params"]
    assert "order_by" not in params

    # glab path: URL string must NOT contain order_by.
    body = _json.dumps([])
    stdout = "HTTP/2.0 200 OK\r\n\r\n" + body
    with patch(
        "link_project_to_chat.gitlab_client._run_glab",
        AsyncMock(return_value=(0, stdout, "")),
    ) as mock_run:
        await gl_glab_client.list_repos(page=1, per_page=5)
    assert all("order_by" not in a for a in mock_run.await_args.args)


async def test_list_repos_glab_self_hosted_uses_hostname(monkeypatch):
    from link_project_to_chat import gitlab_client

    monkeypatch.setattr(gitlab_client, "_glab_available", lambda host="gitlab.com": True)
    client = gitlab_client.GitLabClient(pat="", host="gitlab.example.com")
    body = "[]"
    stdout = "HTTP/2.0 200 OK\r\n\r\n" + body
    with patch(
        "link_project_to_chat.gitlab_client._run_glab",
        AsyncMock(return_value=(0, stdout, "")),
    ) as mock_run:
        await client.list_repos(page=1, per_page=5)

    args = mock_run.await_args.args
    assert "--hostname" in args
    assert "gitlab.example.com" in args


async def test_list_repos_glab_no_next_page(gl_glab_client):
    import json
    body = json.dumps([])
    stdout = "HTTP/2.0 200 OK\r\n\r\n" + body
    with patch("link_project_to_chat.gitlab_client._run_glab",
               AsyncMock(return_value=(0, stdout, ""))):
        repos, has_next = await gl_glab_client.list_repos(page=1, per_page=5)
    assert repos == []
    assert has_next is False


async def test_list_repos_glab_failure_raises(gl_glab_client):
    with patch("link_project_to_chat.gitlab_client._run_glab",
               AsyncMock(return_value=(1, "", "auth error"))):
        with pytest.raises(Exception, match="glab api .* failed"):
            await gl_glab_client.list_repos()


async def test_list_repos_glab_raises_on_dict_response_with_message(gl_glab_client):
    """When glab returns a JSON error object (token invalid, etc.), raise a domain error
    instead of iterating dict keys."""
    import json
    body = json.dumps({"message": "401 Unauthorized"})
    stdout = "HTTP/2.0 401 Unauthorized\r\n\r\n" + body
    with patch("link_project_to_chat.gitlab_client._run_glab",
               AsyncMock(return_value=(0, stdout, ""))):
        with pytest.raises(Exception, match="(?i)glab api error.*401"):
            await gl_glab_client.list_repos()


async def test_validate_repo_url_gitlab_com(gl_api_client):
    project = {
        "path": "myproj",
        "path_with_namespace": "owner/myproj",
        "web_url": "https://gitlab.com/owner/myproj",
        "http_url_to_repo": "https://gitlab.com/owner/myproj.git",
        "description": "ok",
        "visibility": "private",
    }
    with patch.object(gl_api_client, "_client") as mock_client:
        mock_client.get = AsyncMock(return_value=_mock_resp(200, project))
        info = await gl_api_client.validate_repo_url("https://gitlab.com/owner/myproj")
    assert info is not None
    assert info.full_name == "owner/myproj"
    call = mock_client.get.await_args
    assert "projects/owner%2Fmyproj" in call.args[0]


async def test_validate_repo_url_subgroup(gl_api_client):
    project = {
        "path": "leaf",
        "path_with_namespace": "group/sub1/sub2/leaf",
        "web_url": "https://gitlab.com/group/sub1/sub2/leaf",
        "http_url_to_repo": "https://gitlab.com/group/sub1/sub2/leaf.git",
        "description": "",
        "visibility": "internal",
    }
    with patch.object(gl_api_client, "_client") as mock_client:
        mock_client.get = AsyncMock(return_value=_mock_resp(200, project))
        info = await gl_api_client.validate_repo_url(
            "https://gitlab.com/group/sub1/sub2/leaf"
        )
    assert info is not None
    assert info.full_name == "group/sub1/sub2/leaf"
    call = mock_client.get.await_args
    assert "projects/group%2Fsub1%2Fsub2%2Fleaf" in call.args[0]


async def test_validate_repo_url_self_hosted(monkeypatch):
    from link_project_to_chat import gitlab_client
    monkeypatch.setattr(gitlab_client, "_glab_available", lambda: False)
    client = gitlab_client.GitLabClient(pat="glpat-x", host="gitlab.example.com")
    project = {
        "path": "p",
        "path_with_namespace": "g/p",
        "web_url": "https://gitlab.example.com/g/p",
        "http_url_to_repo": "https://gitlab.example.com/g/p.git",
        "description": "",
        "visibility": "private",
    }
    with patch.object(client, "_client") as mock_client:
        mock_client.get = AsyncMock(return_value=_mock_resp(200, project))
        info = await client.validate_repo_url("https://gitlab.example.com/g/p")
    assert info is not None
    assert info.html_url == "https://gitlab.example.com/g/p"


async def test_validate_repo_url_glab_self_hosted_uses_hostname(monkeypatch):
    from link_project_to_chat import gitlab_client

    monkeypatch.setattr(gitlab_client, "_glab_available", lambda host="gitlab.com": True)
    client = gitlab_client.GitLabClient(pat="", host="gitlab.example.com")
    project = {
        "path": "p",
        "path_with_namespace": "g/p",
        "web_url": "https://gitlab.example.com/g/p",
        "http_url_to_repo": "https://gitlab.example.com/g/p.git",
        "description": "",
        "visibility": "private",
    }
    with patch(
        "link_project_to_chat.gitlab_client._run_glab",
        AsyncMock(return_value=(0, __import__("json").dumps(project), "")),
    ) as mock_run:
        info = await client.validate_repo_url("https://gitlab.example.com/g/p")

    assert info is not None
    args = mock_run.await_args.args
    assert "--hostname" in args
    assert "gitlab.example.com" in args


async def test_validate_repo_url_rejects_github(gl_api_client):
    info = await gl_api_client.validate_repo_url("https://github.com/owner/repo")
    assert info is None


async def test_validate_repo_url_rejects_wrong_host(monkeypatch):
    from link_project_to_chat import gitlab_client
    monkeypatch.setattr(gitlab_client, "_glab_available", lambda: False)
    client = gitlab_client.GitLabClient(pat="glpat-x", host="gitlab.example.com")
    info = await client.validate_repo_url("https://gitlab.com/owner/repo")
    assert info is None


async def test_validate_repo_url_rejects_bare_path(gl_api_client):
    info = await gl_api_client.validate_repo_url("not-a-url")
    assert info is None


async def test_validate_repo_url_glab_returns_none_on_dict_message_response(gl_glab_client):
    """When glab returns {'message': '404 ...'}, validate should return None (not raise TypeError)."""
    import json
    body = json.dumps({"message": "404 Not Found"})
    with patch("link_project_to_chat.gitlab_client._run_glab",
               AsyncMock(return_value=(0, body, ""))):
        info = await gl_glab_client.validate_repo_url("https://gitlab.com/u/repo")
    assert info is None


async def test_validate_repo_url_not_found(gl_api_client):
    with patch.object(gl_api_client, "_client") as mock_client:
        mock_client.get = AsyncMock(return_value=_mock_resp(404, {"message": "Not Found"}))
        info = await gl_api_client.validate_repo_url("https://gitlab.com/x/y")
    assert info is None


class _FakeProc:
    def __init__(self, returncode: int, stderr: bytes = b"", stdout: bytes = b""):
        self.returncode = returncode
        self._stderr = stderr
        self._stdout = stdout

    async def communicate(self):
        return self._stdout, self._stderr


async def test_clone_repo_api_mode_injects_bearer_via_git_config(
    gl_api_client, tmp_path: Path,
):
    from link_project_to_chat.repo_provider import RepoInfo

    repo = RepoInfo(
        name="p", full_name="u/p",
        html_url="https://gitlab.com/u/p",
        clone_url="https://gitlab.com/u/p.git",
        description="", private=True,
    )
    with patch(
        "link_project_to_chat.gitlab_client.asyncio.create_subprocess_exec",
        AsyncMock(return_value=_FakeProc(0)),
    ) as mock_exec:
        await gl_api_client.clone_repo(repo, tmp_path / "p")

    args = mock_exec.await_args.args
    kwargs = mock_exec.await_args.kwargs
    assert args[:3] == ("git", "clone", "https://gitlab.com/u/p.git")
    assert all("glpat-test123" not in str(a) for a in args)
    env = kwargs["env"]
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "http.https://gitlab.com/.extraHeader"
    assert env["GIT_CONFIG_VALUE_0"] == "AUTHORIZATION: Bearer glpat-test123"


async def test_clone_repo_api_mode_redacts_pat_in_errors(gl_api_client, tmp_path: Path):
    from link_project_to_chat.repo_provider import RepoInfo

    repo = RepoInfo(
        name="p", full_name="u/p",
        html_url="https://gitlab.com/u/p",
        clone_url="https://gitlab.com/u/p.git",
        description="", private=True,
    )
    stderr = b"fatal: https://glpat-test123@gitlab.com/u/p.git access denied"
    with patch(
        "link_project_to_chat.gitlab_client.asyncio.create_subprocess_exec",
        AsyncMock(return_value=_FakeProc(1, stderr=stderr)),
    ):
        with pytest.raises(Exception, match="git clone failed") as exc:
            await gl_api_client.clone_repo(repo, tmp_path / "p")
    assert "glpat-test123" not in str(exc.value)
    assert "[REDACTED]" in str(exc.value)


async def test_clone_repo_api_mode_uses_self_hosted_host_in_git_config(
    monkeypatch, tmp_path: Path,
):
    from link_project_to_chat import gitlab_client
    from link_project_to_chat.repo_provider import RepoInfo

    monkeypatch.setattr(gitlab_client, "_glab_available", lambda: False)
    client = gitlab_client.GitLabClient(pat="glpat-x", host="gitlab.example.com")
    repo = RepoInfo(
        name="p", full_name="u/p",
        html_url="https://gitlab.example.com/u/p",
        clone_url="https://gitlab.example.com/u/p.git",
        description="", private=True,
    )
    with patch(
        "link_project_to_chat.gitlab_client.asyncio.create_subprocess_exec",
        AsyncMock(return_value=_FakeProc(0)),
    ) as mock_exec:
        await client.clone_repo(repo, tmp_path / "p")

    env = mock_exec.await_args.kwargs["env"]
    assert env["GIT_CONFIG_KEY_0"] == "http.https://gitlab.example.com/.extraHeader"


async def test_clone_repo_glab_mode_uses_full_name(gl_glab_client, tmp_path: Path):
    from link_project_to_chat.repo_provider import RepoInfo

    repo = RepoInfo(
        name="p", full_name="group/sub/p",
        html_url="https://gitlab.com/group/sub/p",
        clone_url="https://gitlab.com/group/sub/p.git",
        description="", private=True,
    )
    with patch(
        "link_project_to_chat.gitlab_client.asyncio.create_subprocess_exec",
        AsyncMock(return_value=_FakeProc(0)),
    ) as mock_exec:
        await gl_glab_client.clone_repo(repo, tmp_path / "p")

    args = mock_exec.await_args.args
    assert args[0] == "glab"
    assert args[1:3] == ("repo", "clone")
    assert args[3] == "group/sub/p"
    assert args[4] == str(tmp_path / "p")


async def test_clone_repo_glab_self_hosted_sets_gitlab_host(monkeypatch, tmp_path: Path):
    from link_project_to_chat import gitlab_client
    from link_project_to_chat.repo_provider import RepoInfo

    monkeypatch.setattr(gitlab_client, "_glab_available", lambda host="gitlab.com": True)
    client = gitlab_client.GitLabClient(pat="", host="gitlab.example.com")
    repo = RepoInfo(
        name="p", full_name="group/p",
        html_url="https://gitlab.example.com/group/p",
        clone_url="https://gitlab.example.com/group/p.git",
        description="", private=True,
    )
    with patch(
        "link_project_to_chat.gitlab_client.asyncio.create_subprocess_exec",
        AsyncMock(return_value=_FakeProc(0)),
    ) as mock_exec:
        await client.clone_repo(repo, tmp_path / "p")

    assert mock_exec.await_args.kwargs["env"]["GITLAB_HOST"] == "gitlab.example.com"


async def test_clone_repo_glab_mode_failure_raises(gl_glab_client, tmp_path: Path):
    from link_project_to_chat.repo_provider import RepoInfo

    repo = RepoInfo(
        name="p", full_name="u/p",
        html_url="https://gitlab.com/u/p",
        clone_url="https://gitlab.com/u/p.git",
        description="", private=True,
    )
    with patch(
        "link_project_to_chat.gitlab_client.asyncio.create_subprocess_exec",
        AsyncMock(return_value=_FakeProc(1, stderr=b"glab: permission denied")),
    ):
        with pytest.raises(Exception, match="glab repo clone failed"):
            await gl_glab_client.clone_repo(repo, tmp_path / "p")
