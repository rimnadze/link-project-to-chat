"""RepoProvider Protocol + RepoInfo dataclass."""
from __future__ import annotations

from pathlib import Path

import pytest

from link_project_to_chat.repo_provider import RepoInfo, RepoProvider


def test_repoinfo_fields():
    info = RepoInfo(
        name="my-app",
        full_name="acme/my-app",
        html_url="https://example.com/acme/my-app",
        clone_url="https://example.com/acme/my-app.git",
        description="An app",
        private=True,
    )
    assert info.name == "my-app"
    assert info.full_name == "acme/my-app"
    assert info.private is True


def test_github_client_satisfies_protocol():
    """Existing GitHubClient must structurally implement RepoProvider."""
    from link_project_to_chat.github_client import GitHubClient

    # Structural check: every method on the Protocol exists on the class.
    for name in ("list_repos", "validate_repo_url", "clone_repo", "close"):
        assert callable(getattr(GitHubClient, name)), f"GitHubClient.{name} missing"


def test_protocol_has_expected_methods():
    """Pin the Protocol surface so a future rename is caught by tests."""
    for name in ("list_repos", "validate_repo_url", "clone_repo", "close"):
        assert hasattr(RepoProvider, name), f"RepoProvider.{name} missing"
