from __future__ import annotations

from link_project_to_chat.config import (
    Config,
    GoogleChatConfig,
    GoogleChatProjectOverride,
    ProjectConfig,
)
from link_project_to_chat.google_chat.resolver import resolve_project_google_chat


def _config(top_level: GoogleChatConfig | None, projects: dict[str, ProjectConfig]) -> Config:
    return Config(
        projects=projects,
        google_chat=top_level if top_level is not None else GoogleChatConfig(),
    )


def test_no_override_no_top_level_returns_none():
    config = _config(None, {"alpha": ProjectConfig(path="/p", telegram_bot_token="")})
    assert resolve_project_google_chat("alpha", config) is None


def test_top_level_only_returns_top_level():
    top = GoogleChatConfig(
        service_account_file="/keys/shared.json",
        public_url="https://shared.example",
        port=8090,
        root_command_id=1,
    )
    config = _config(top, {"alpha": ProjectConfig(path="/p", telegram_bot_token="")})
    resolved = resolve_project_google_chat("alpha", config)
    assert resolved is not None
    assert resolved.service_account_file == "/keys/shared.json"
    assert resolved.port == 8090


def test_override_replaces_per_field():
    top = GoogleChatConfig(
        service_account_file="/keys/shared.json",
        public_url="https://shared.example",
        port=8090,
        root_command_id=1,
        host="0.0.0.0",  # operational default stays
    )
    config = _config(top, {
        "alpha": ProjectConfig(
            path="/p",
            telegram_bot_token="",
            google_chat=GoogleChatProjectOverride(
                port=8091,
                service_account_file="/keys/alpha.json",
                public_url="https://alpha.example",
                root_command_id=3,
            ),
        )
    })

    resolved = resolve_project_google_chat("alpha", config)
    assert resolved is not None
    # Per-project wins:
    assert resolved.port == 8091
    assert resolved.service_account_file == "/keys/alpha.json"
    assert resolved.public_url == "https://alpha.example"
    assert resolved.root_command_id == 3
    # Operational default inherited from top-level:
    assert resolved.host == "0.0.0.0"


def test_override_alone_returns_merged_with_empty_defaults():
    """Override with only port set, no top-level block to fill service_account_file."""
    config = _config(None, {
        "alpha": ProjectConfig(
            path="/p",
            telegram_bot_token="",
            google_chat=GoogleChatProjectOverride(port=8091),
        )
    })
    # service_account_file is empty after merge → resolved.service_account_file == ""
    # validators downstream will reject this; resolver returns the merged dict
    # so callers can let validators emit the precise error.
    resolved = resolve_project_google_chat("alpha", config)
    assert resolved is not None
    assert resolved.service_account_file == ""
    assert resolved.port == 8091


def test_unknown_project_returns_none():
    config = _config(GoogleChatConfig(port=8090), {})
    assert resolve_project_google_chat("does-not-exist", config) is None
