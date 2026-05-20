from pathlib import Path

from link_project_to_chat.backends.base import BackendCapabilities, BaseBackend
from link_project_to_chat.backends.claude import ClaudeBackend


class _DummyBackend(BaseBackend):
    name = "dummy"
    capabilities = BackendCapabilities(
        models=(),
        supports_thinking=False,
        supports_permissions=False,
        supports_resume=False,
        supports_compact=False,
        supports_allowed_tools=False,
        supports_usage_cap_detection=False,
    )
    _env_keep_patterns = ("OPENAI_*", "CODEX_*")
    _env_scrub_patterns = (
        "*_TOKEN", "*_KEY", "*_SECRET",
        "ANTHROPIC_*", "AWS_*", "GITHUB_*", "DATABASE_*", "PASSWORD*",
    )

    def __init__(self) -> None:
        self.project_path = Path("/tmp/project")
        self.model = None
        self.session_id = None


def test_keep_patterns_override_scrub_patterns(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.setenv("CODEX_SESSION_TOKEN", "codex-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "gh-secret")
    monkeypatch.setenv("GITLAB_TOKEN", "gl-secret")

    env = _DummyBackend()._prepare_env()

    assert env["OPENAI_API_KEY"] == "openai-secret"
    assert env["CODEX_SESSION_TOKEN"] == "codex-secret"
    assert "ANTHROPIC_API_KEY" not in env
    assert "GITHUB_TOKEN" not in env
    assert "GITLAB_TOKEN" not in env


def test_claude_backend_still_scrubs_openai_keys(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")

    env = ClaudeBackend(project_path=tmp_path)._prepare_env()

    assert "OPENAI_API_KEY" not in env
    assert "ANTHROPIC_API_KEY" not in env
