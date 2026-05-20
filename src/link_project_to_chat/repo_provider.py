"""Provider-neutral repo abstractions.

`RepoInfo` carries the minimal fields the manager wizard needs to display,
validate, and clone a repo from any forge. `RepoProvider` is a structural
Protocol both `GitHubClient` and `GitLabClient` satisfy — the wizard only
talks to this surface so per-provider branching is confined to a single
factory helper in manager/bot.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass
class RepoInfo:
    name: str
    full_name: str
    html_url: str
    clone_url: str
    description: str
    private: bool


class RepoProvider(Protocol):
    async def list_repos(self, page: int, per_page: int) -> tuple[list[RepoInfo], bool]: ...
    async def validate_repo_url(self, url: str) -> RepoInfo | None: ...
    async def clone_repo(self, repo: RepoInfo, dest: Path) -> None: ...
    async def close(self) -> None: ...
