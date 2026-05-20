from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import subprocess
import shutil
from pathlib import Path

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


# RepoInfo moved to repo_provider.py for cross-provider sharing.
# Re-exported here so legacy imports (`from .github_client import RepoInfo`)
# keep working. New code should prefer `from .repo_provider import RepoInfo`.
from .repo_provider import RepoInfo  # noqa: F401


_GITHUB_URL_RE = re.compile(r"https?://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$")
_GITHUB_CREDENTIAL_URL_RE = re.compile(r"https://[^/@\s]+@github\.com")


def _gh_available() -> bool:
    """Check if gh CLI is installed and authenticated."""
    gh_path = shutil.which("gh")
    if gh_path is None:
        return False
    try:
        proc = subprocess.run(
            [gh_path, "auth", "status"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


async def _run_gh(*args: str) -> tuple[int, str, str]:
    """Run a gh CLI command and return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        "gh", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode().strip(), stderr.decode().strip()


def _git_auth_env(pat: str) -> dict[str, str]:
    """Inject a one-shot GitHub auth header via env, not argv."""
    env = os.environ.copy()
    try:
        count = int(env.get("GIT_CONFIG_COUNT", "0"))
    except ValueError:
        count = 0
    auth = base64.b64encode(f"x-access-token:{pat}".encode()).decode()
    env["GIT_CONFIG_COUNT"] = str(count + 1)
    env[f"GIT_CONFIG_KEY_{count}"] = "http.https://github.com/.extraHeader"
    env[f"GIT_CONFIG_VALUE_{count}"] = f"AUTHORIZATION: basic {auth}"
    return env


def _redact_secrets(text: str, *secrets: str) -> str:
    redacted = text
    for secret in secrets:
        if not secret:
            continue
        redacted = redacted.replace(secret, "[REDACTED]")
        encoded = base64.b64encode(f"x-access-token:{secret}".encode()).decode()
        redacted = redacted.replace(encoded, "[REDACTED]")
    return _GITHUB_CREDENTIAL_URL_RE.sub("https://[REDACTED]@github.com", redacted)


class GitHubClient:
    """GitHub client that uses gh CLI if available, falls back to PAT + httpx."""

    def __init__(self, pat: str = ""):
        self._pat = pat
        prefer_api = bool(pat) and httpx is not None
        self._use_gh = _gh_available() and not prefer_api
        self._client = None
        if not self._use_gh:
            if httpx is None:
                raise ImportError(
                    "Neither gh CLI nor httpx available. "
                    "Install gh CLI (https://cli.github.com) or run: pip install link-project-to-chat[create]"
                )
            if not pat:
                raise ValueError("GitHub PAT required when gh CLI is not available.")
            self._client = httpx.AsyncClient(
                base_url="https://api.github.com",
                headers={
                    "Authorization": f"Bearer {pat}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=30.0,
            )

    async def list_repos(self, page: int = 1, per_page: int = 5) -> tuple[list[RepoInfo], bool]:
        if self._use_gh:
            return await self._list_repos_gh(page, per_page)
        return await self._list_repos_api(page, per_page)

    async def _list_repos_gh(self, page: int, per_page: int) -> tuple[list[RepoInfo], bool]:
        # Use /user/repos so org-member repos are included (gh repo list is user-owned only).
        code, stdout, stderr = await _run_gh(
            "api", "--include",
            f"/user/repos?sort=updated&page={page}&per_page={per_page}",
        )
        if code != 0:
            raise Exception(f"gh api /user/repos failed: {stderr}")
        sep = "\r\n\r\n" if "\r\n\r\n" in stdout else "\n\n"
        headers_part, _, body_part = stdout.partition(sep)
        if not body_part:
            raise Exception("gh api returned no body")
        has_next = 'rel="next"' in headers_part
        repos = [
            RepoInfo(
                name=r["name"],
                full_name=r["full_name"],
                html_url=r["html_url"],
                clone_url=r["clone_url"],
                description=r.get("description") or "",
                private=r["private"],
            )
            for r in json.loads(body_part)
        ]
        return repos, has_next

    async def _list_repos_api(self, page: int, per_page: int) -> tuple[list[RepoInfo], bool]:
        resp = await self._client.get("/user/repos", params={"sort": "updated", "page": page, "per_page": per_page})
        if resp.status_code != 200:
            raise Exception(f"GitHub API error {resp.status_code}: {resp.json().get('message', '')}")
        repos = [
            RepoInfo(name=r["name"], full_name=r["full_name"], html_url=r["html_url"],
                     clone_url=r["clone_url"], description=r.get("description") or "", private=r["private"])
            for r in resp.json()
        ]
        has_next = 'rel="next"' in resp.headers.get("link", "")
        return repos, has_next

    async def validate_repo_url(self, url: str) -> RepoInfo | None:
        match = _GITHUB_URL_RE.match(url.strip())
        if not match:
            return None
        owner, repo = match.group(1), match.group(2)
        if self._use_gh:
            return await self._validate_gh(owner, repo)
        return await self._validate_api(owner, repo)

    async def _validate_gh(self, owner: str, repo: str) -> RepoInfo | None:
        code, stdout, stderr = await _run_gh(
            "repo", "view", f"{owner}/{repo}",
            "--json", "name,nameWithOwner,url,sshUrl,isPrivate,description",
        )
        if code != 0:
            return None
        r = json.loads(stdout)
        return RepoInfo(
            name=r["name"],
            full_name=r["nameWithOwner"],
            html_url=r["url"],
            clone_url=r["url"] + ".git",
            description=r.get("description") or "",
            private=r["isPrivate"],
        )

    async def _validate_api(self, owner: str, repo: str) -> RepoInfo | None:
        resp = await self._client.get(f"/repos/{owner}/{repo}")
        if resp.status_code != 200:
            return None
        r = resp.json()
        return RepoInfo(name=r["name"], full_name=r["full_name"], html_url=r["html_url"],
                        clone_url=r["clone_url"], description=r.get("description") or "", private=r["private"])

    async def clone_repo(self, repo: RepoInfo, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if self._use_gh:
            # gh repo clone handles auth automatically
            proc = await asyncio.create_subprocess_exec(
                "gh", "repo", "clone", repo.full_name, str(dest),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise Exception(f"gh repo clone failed: {stderr.decode().strip()}")
        else:
            clone_url = repo.clone_url
            env = None
            if self._pat:
                env = _git_auth_env(self._pat)
            proc = await asyncio.create_subprocess_exec(
                "git", "clone", clone_url, str(dest),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise Exception(
                    f"git clone failed: {_redact_secrets(stderr.decode().strip(), self._pat)}"
                )

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
