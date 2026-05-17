"""Merge per-project google_chat overrides onto the top-level block."""
from __future__ import annotations

from dataclasses import fields, replace

from link_project_to_chat.config import (
    Config,
    GoogleChatConfig,
    GoogleChatProjectOverride,
    ProjectConfig,
)


def resolve_project_google_chat(project_name: str, config: Config) -> GoogleChatConfig | None:
    """Return the effective GoogleChatConfig for ``project_name``, or None.

    None means the project has no google_chat configured (neither override nor
    a non-empty top-level block). The returned config is the result of
    overlaying any per-project override field-by-field onto the top-level
    block. Downstream validators decide whether the merged result is complete
    enough to actually start a bot.
    """
    project = config.projects.get(project_name)
    if project is None:
        return None

    override = project.google_chat
    top_level = config.google_chat

    # A "non-empty" top-level is one with at least service_account_file or port
    # explicitly set. Pure-default GoogleChatConfig() means "nothing configured".
    top_is_meaningful = (
        top_level is not None
        and (top_level.service_account_file or top_level.public_url or top_level.root_command_id)
    )
    if override is None and not top_is_meaningful:
        return None

    base = top_level if top_level is not None else GoogleChatConfig()
    if override is None:
        return base

    # Build the merge dict: every override field that's not None wins.
    merged_kwargs = {}
    for f in fields(GoogleChatProjectOverride):
        value = getattr(override, f.name)
        if value is not None:
            merged_kwargs[f.name] = value
    return replace(base, **merged_kwargs)
