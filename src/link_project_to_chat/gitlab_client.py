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


def _glab_available(host: str = "gitlab.com") -> bool:
    """Check if glab CLI is installed AND its token is currently accepted by GitLab.

    `glab auth status` exits 0 even when the stored token has been revoked, so
    we hit `/api/user` instead — it returns a user object on success and a JSON
    error body (still exit 0) on rejection, which we can distinguish.
    """
    glab_path = shutil.which("glab")
    if glab_path is None:
        return False
    cmd = [glab_path, "api"]
    if host != "gitlab.com":
        cmd.extend(["--hostname", host])
    cmd.append("user")
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if proc.returncode != 0:
        return False
    # Even on exit 0, glab may have written an error JSON to stdout (e.g.
    # {"message": "401 ..."}). The /api/user endpoint returns an object with
    # "id" / "username" on success.
    try:
        body = proc.stdout.decode("utf-8", errors="replace").strip()
        if not body:
            return False
        parsed = json.loads(body)
        return isinstance(parsed, dict) and ("id" in parsed or "username" in parsed)
    except (ValueError, json.JSONDecodeError):
        return False


async def _run_glab(*args: str, env: dict[str, str] | None = None) -> tuple[int, str, str]:
    """Run a glab CLI command and return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "glab", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode().strip(), stderr.decode().strip()


def _repo_info_from_project(p: dict) -> RepoInfo:
    """Map a GitLab `/projects` payload entry to RepoInfo."""
    return RepoInfo(
        name=p["path"],
        full_name=p["path_with_namespace"],
        html_url=p["web_url"],
        clone_url=p["http_url_to_repo"],
        description=p.get("description") or "",
        private=p.get("visibility", "private") != "public",
    )


def _git_auth_env(pat: str, host: str) -> dict[str, str]:
    """Inject a one-shot GitLab auth header via env, not argv.

    Uses ``Authorization: Bearer {pat}`` (GitLab convention). The GitHub
    equivalent uses ``basic`` with a base64'd ``x-access-token:{pat}`` pair —
    these MUST differ.
    """
    env = os.environ.copy()
    try:
        count = int(env.get("GIT_CONFIG_COUNT", "0"))
    except ValueError:
        count = 0
    env["GIT_CONFIG_COUNT"] = str(count + 1)
    env[f"GIT_CONFIG_KEY_{count}"] = f"http.https://{host}/.extraHeader"
    env[f"GIT_CONFIG_VALUE_{count}"] = f"AUTHORIZATION: Bearer {pat}"
    return env


def _gitlab_url_re(host: str) -> "re.Pattern[str]":
    """Compile a per-host URL regex. Supports subgroups via the multi-segment capture."""
    return re.compile(rf"https?://{re.escape(host)}/((?:[^/\s]+/)+?[^/\s]+?)(?:\.git)?/?$")


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


class GitLabClient:
    """GitLab client that uses glab CLI if available, falls back to PAT + httpx."""

    def __init__(self, pat: str = "", host: str = "gitlab.com"):
        self._pat = pat or os.environ.get("GITLAB_TOKEN", "")
        self._host = host
        prefer_api = bool(pat) and httpx is not None
        self._use_glab = False if prefer_api else _glab_available(host=host)
        self._client = None
        if not self._use_glab:
            if httpx is None:
                raise ImportError(
                    "Neither glab CLI nor httpx available. "
                    "Install glab (https://gitlab.com/gitlab-org/cli) or run: "
                    "pip install link-project-to-chat[create]"
                )
            if not self._pat:
                raise ValueError("GitLab PAT required when glab CLI is not available.")
            self._client = httpx.AsyncClient(
                base_url=f"https://{host}/api/v4",
                headers={"PRIVATE-TOKEN": self._pat},
                timeout=30.0,
            )

    async def list_repos(self, page: int = 1, per_page: int = 5) -> tuple[list[RepoInfo], bool]:
        if self._use_glab:
            return await self._list_repos_glab(page, per_page)
        return await self._list_repos_api(page, per_page)

    async def _list_repos_api(self, page: int, per_page: int) -> tuple[list[RepoInfo], bool]:
        # Omit `order_by` entirely: GitLab.com /projects returns HTTP 500 for
        # ANY value of `order_by` when the membership set contains projects in
        # the `deletion_scheduled` state (account-state bug, observed 2026-05).
        # The REST default (created_at desc) is fine for the wizard's
        # paginated picker. `simple=true` still skips the joins
        # (statistics/permissions/links) we don't need — the RepoInfo mapping
        # only consumes path / path_with_namespace / web_url /
        # http_url_to_repo / description / visibility, all present in the
        # simple response.
        resp = await self._client.get(
            "/projects",
            params={
                "membership": "true",
                "simple": "true",
                "page": page,
                "per_page": per_page,
            },
        )
        if resp.status_code != 200:
            raise Exception(f"GitLab API error {resp.status_code}: {resp.json().get('message', '')}")
        repos = [_repo_info_from_project(p) for p in resp.json()]
        has_next = 'rel="next"' in resp.headers.get("link", "")
        return repos, has_next

    async def _list_repos_glab(self, page: int, per_page: int) -> tuple[list[RepoInfo], bool]:
        # `glab api --include` emits HTTP headers + body the same shape as `gh api --include`.
        args = ["api"]
        if self._host != "gitlab.com":
            args.extend(["--hostname", self._host])
        # See _list_repos_api: omit `order_by` entirely — GitLab.com /projects
        # returns HTTP 500 for ANY value of `order_by` when the membership set
        # contains `deletion_scheduled` projects (account-state bug). Default
        # sort (created_at desc) is fine for the wizard. `simple=true` stays
        # to skip joins we don't consume.
        args.extend([
            "--include",
            f"projects?membership=true&simple=true&page={page}&per_page={per_page}",
        ])
        code, stdout, stderr = await _run_glab(
            *args,
        )
        if code != 0:
            raise Exception(f"glab api projects failed: {stderr}")
        sep = "\r\n\r\n" if "\r\n\r\n" in stdout else "\n\n"
        headers_part, _, body_part = stdout.partition(sep)
        if not body_part:
            raise Exception("glab api returned no body")
        has_next = 'rel="next"' in headers_part
        parsed = json.loads(body_part)
        if not isinstance(parsed, list):
            msg = parsed.get("message") if isinstance(parsed, dict) else None
            raise Exception(
                f"glab api error: {msg or 'unexpected response shape'}. "
                "Token may be invalid — re-run `glab auth login`."
            )
        repos = [_repo_info_from_project(p) for p in parsed]
        return repos, has_next

    async def validate_repo_url(self, url: str) -> RepoInfo | None:
        match = _gitlab_url_re(self._host).match(url.strip())
        if not match:
            return None
        full_path = match.group(1)
        if self._use_glab:
            return await self._validate_glab(full_path)
        return await self._validate_api(full_path)

    async def _validate_api(self, full_path: str) -> RepoInfo | None:
        from urllib.parse import quote
        encoded = quote(full_path, safe="")
        resp = await self._client.get(f"/projects/{encoded}")
        if resp.status_code != 200:
            return None
        return _repo_info_from_project(resp.json())

    async def _validate_glab(self, full_path: str) -> RepoInfo | None:
        from urllib.parse import quote
        encoded = quote(full_path, safe="")
        args = ["api"]
        if self._host != "gitlab.com":
            args.extend(["--hostname", self._host])
        args.append(f"projects/{encoded}")
        code, stdout, stderr = await _run_glab(*args)
        if code != 0:
            return None
        parsed = json.loads(stdout)
        if not isinstance(parsed, dict) or "path" not in parsed:
            return None  # error response (e.g. {"message": "401"} or {"error": ...})
        return _repo_info_from_project(parsed)

    async def _clone_glab(self, repo: RepoInfo, dest: Path) -> None:
        env = None
        if self._host != "gitlab.com":
            env = os.environ.copy()
            env["GITLAB_HOST"] = self._host
        proc = await asyncio.create_subprocess_exec(
            "glab", "repo", "clone", repo.full_name, str(dest),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise Exception(
                "glab repo clone failed: "
                + _redact_secrets(stderr.decode().strip(), self._pat, host=self._host)
            )

    async def clone_repo(self, repo: RepoInfo, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if self._use_glab:
            await self._clone_glab(repo, dest)
            return
        env = _git_auth_env(self._pat, self._host) if self._pat else None
        proc = await asyncio.create_subprocess_exec(
            "git", "clone", repo.clone_url, str(dest),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise Exception(
                "git clone failed: "
                + _redact_secrets(stderr.decode().strip(), self._pat, host=self._host)
            )

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
