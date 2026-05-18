# Slack Transport Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement `SlackTransport` using `slack_bolt` with Socket Mode, wired to the existing project and manager bot surfaces via the shared primitives from spec #1 (structured mentions, `PromptSpec`, `BotPeerRef`/`RoomBinding`). This is the final transport in the additive track; if `PromptSpec` and structured mentions hold for Slack, the model is genuinely cross-platform.

**Architecture:** `SlackTransport` wraps a `slack_bolt.async_app.AsyncApp` with Socket Mode; `/lp2c` slash command maps to `CommandInvocation`; `PromptSpec(TEXT/SECRET)` opens Slack modals (Block Kit `input` views); `CHOICE/CONFIRM` sends `actions` Block Kit sections; `IncomingMessage.mentions` is populated from Slack mention entities (`<@U...>`) parsed from message text. Socket Mode means no public ingress needed.

**Tech Stack:** Python 3.11+, `slack-bolt>=1.18` with `slack_bolt.async_app.AsyncApp`, `slack_sdk>=3.19`, Socket Mode (`AsyncSocketModeHandler`)

**Prerequisite:** Plan `2026-04-21-web-transport.md` must be complete (all shared primitives in `transport/base.py`, `config.py`, `group_filters.py`, and `FakeTransport` extended).

**Scope:** v1.0 — transport-only. Per-project override schema lands in this plan (Task 1) so the v1.1 manager-integration plan can layer wizard + ProcessManager on top without re-opening config. The full manager-wizard + `ProcessManager.start_slack_subprocess` + autostart wiring is **deferred to v1.1** under a separate plan: [`2026-05-18-slack-manager-integration.md`](2026-05-18-slack-manager-integration.md). The v1.0 plan stops at "SlackTransport satisfies the parametrized contract test." v1.1 turns it into a first-class manager-supervised feature mirroring the Google Chat v1.2 shipping arc.

---

## File Map

| File | Change |
|------|--------|
| `src/link_project_to_chat/transport/slack.py` | **NEW**: `SlackTransport` full implementation |
| `src/link_project_to_chat/transport/__init__.py` | Export `SlackTransport` |
| `src/link_project_to_chat/slack/__init__.py` | **NEW**: namespace package for resolver/helpers (mirrors `google_chat/`) |
| `src/link_project_to_chat/slack/resolver.py` | **NEW**: `resolve_project_slack` per-project merge helper (mirrors `google_chat/resolver.py`) |
| `src/link_project_to_chat/config.py` | Add `SlackConfig`, `SlackProjectOverride`, parse/serialize helpers, `ProjectConfig.slack` field, `_maybe_migrate_top_level_slack` |
| `pyproject.toml` | Add `slack` optional dep group: `slack-bolt>=1.18` |
| `tests/transport/test_contract.py` | Add `SlackTransport` to `transport` fixture |
| `tests/transport/test_slack_transport.py` | **NEW**: Slack-specific unit tests (modal submit, mention parsing, command parsing) |
| `tests/slack/test_resolver.py` | **NEW**: unit tests for the per-project merge |
| `tests/test_config.py` | Round-trip + migration tests for `SlackProjectOverride` |

---

### Task 1: Add `slack_bolt` dependency, transport skeleton, and `SlackProjectOverride` config schema

This task lands four discrete sub-steps in this order so the v1.1 manager-integration plan can build directly on top without reopening config:

1. Add the `slack_bolt` optional dependency and `SlackTransport` skeleton (existing scope).
2. Add `SlackConfig` (top-level operational defaults) and `SlackProjectOverride` (per-project optionals) dataclasses, mirroring the Google Chat config layering.
3. Add `_parse_slack_override` / `_serialize_slack_override` round-trip helpers + `_maybe_migrate_top_level_slack` one-shot migration.
4. Add `resolve_project_slack` per-project merge resolver in a new `src/link_project_to_chat/slack/resolver.py` module.

Reference-mirror prior art (every helper has a Google Chat twin already shipped on `dev`):
- `# Mirror: src/link_project_to_chat/config.py GoogleChatProjectOverride pattern (lines ~372-407)`
- `# Mirror: src/link_project_to_chat/google_chat/resolver.py resolve_project_google_chat`
- `# Mirror: _maybe_migrate_top_level_google_chat in config.py (lines ~1065-1081)`

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/link_project_to_chat/config.py`
- Create: `src/link_project_to_chat/transport/slack.py`
- Create: `src/link_project_to_chat/slack/__init__.py`
- Create: `src/link_project_to_chat/slack/resolver.py`
- Create: `tests/transport/test_slack_transport.py` (initial skeleton)
- Create: `tests/slack/test_resolver.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Write the failing skeleton test**

```python
# tests/transport/test_slack_transport.py
def test_slack_transport_importable():
    from link_project_to_chat.transport.slack import SlackTransport  # noqa: F401


def test_slack_transport_id():
    from unittest.mock import MagicMock
    from link_project_to_chat.transport.slack import SlackTransport

    app = MagicMock()
    t = SlackTransport(app)
    assert t.TRANSPORT_ID == "slack"


def test_mention_regex_matches_U_W_B_user_ids():
    """Lesson from Google Chat v1.0: <@B...> bot mentions must be parsed too.

    Slack stable IDs come in three flavors — U (regular user), W (Enterprise
    Grid user), and B (bot user). A regex that only matches U... drops every
    bot-to-bot @mention, breaking team-routing in mixed-bot channels.
    """
    from link_project_to_chat.transport.slack import _MENTION_RE

    text = "<@U111> please ping <@W222|alice> and <@B333>"
    matches = _MENTION_RE.findall(text)
    assert matches == ["U111", "W222", "B333"]


def test_channel_mention_regex_matches_C_ids():
    from link_project_to_chat.transport.slack import _CHANNEL_MENTION_RE

    text = "see <#C100|general> and <#C200>"
    matches = _CHANNEL_MENTION_RE.findall(text)
    assert matches == ["C100", "C200"]
```

- [ ] **Step 2: Run to confirm failures**

```
pytest tests/transport/test_slack_transport.py::test_slack_transport_importable -v
```
Expected: `ModuleNotFoundError` — `slack` package not installed and module does not exist.

- [ ] **Step 3: Add `slack` optional dep to `pyproject.toml`**

```toml
[project.optional-dependencies]
slack = ["slack-bolt>=1.18"]
all = ["httpx>=0.27", "telethon>=1.36", "openai>=1.30", "fastapi[standard]>=0.111", "jinja2>=3.1", "aiosqlite>=0.19", "discord.py>=2.3", "slack-bolt>=1.18"]
```

Install it:
```
pip install -e ".[slack]"
```

- [ ] **Step 4: Create `src/link_project_to_chat/transport/slack.py` skeleton**

```python
"""SlackTransport — Transport Protocol implementation for Slack.

Uses slack_bolt AsyncApp with Socket Mode so no public ingress is needed.
/lp2c slash command maps to CommandInvocation. PromptSpec(TEXT/SECRET)
opens Slack modal views; CHOICE/CONFIRM sends Block Kit actions sections.
Mentions are parsed from <@U...>, <@W...> (Enterprise Grid users), and
<@B...> (bot user) entities to populate IncomingMessage.mentions; channel
mentions (<#C...>) are parsed separately for the prompt-renderer.
"""
from __future__ import annotations

import itertools
import re
from pathlib import Path
from typing import Any

from link_project_to_chat.transport.base import (
    ButtonClick,
    ButtonHandler,
    Buttons,
    ChatKind,
    ChatRef,
    CommandHandler,
    CommandInvocation,
    Identity,
    IncomingFile,
    IncomingMessage,
    MessageHandler,
    MessageRef,
    OnReadyCallback,
    PromptHandler,
    PromptRef,
    PromptSpec,
    PromptSubmission,
    TransportRetryAfter,
)

# Matches Slack mention tokens: <@U12345678>, <@W12345678|alice>, <@B12345678>.
# Slack stable IDs come in three flavors:
#   U... = regular workspace user
#   W... = Slack-Enterprise-Grid user
#   B... = bot user (peer bots, our own bot, and any third-party Slack apps)
# Lesson from Google Chat v1.0: the inbound parser must accept B-prefixed IDs
# so bot-to-bot @mentions (peer bots in the same channel) make it into
# IncomingMessage.mentions and the team-routing layer can match BotPeerRef.
_MENTION_RE = re.compile(r"<@([UWB][A-Z0-9]+)(?:\|[^>]*)?>")

# Matches Slack channel mention tokens: <#C12345678> or <#C12345678|general>.
# Populated separately from user mentions because Identity vs ChatRef are
# different shapes and the team-routing layer doesn't care about channel
# refs inside message text — but the linker / prompt-renderer does, so the
# transport exposes both lists.
_CHANNEL_MENTION_RE = re.compile(r"<#(C[A-Z0-9]+)(?:\|[^>]*)?>")


def _parse_mentions(text: str, client: Any) -> list[Identity]:
    """Extract structured Identity objects from Slack mention tokens in text."""
    ids = _MENTION_RE.findall(text)
    result: list[Identity] = []
    for uid in ids:
        result.append(Identity(
            transport_id="slack",
            native_id=uid,
            display_name="",
            handle=None,
            # is_bot can't be known from text alone; the dispatcher overrides
            # this from event.bot_profile when normalizing IncomingMessage.
            is_bot=uid.startswith("B"),
        ))
    return result


def _parse_channel_mentions(text: str) -> list[str]:
    """Extract channel IDs from <#C...> tokens. Returned bare (no ChatRef wrap)
    because the contract test exercises plain ID extraction; callers that
    need a ChatRef build it via ``_chat_ref_from_slack``."""
    return _CHANNEL_MENTION_RE.findall(text)


def _chat_ref_from_slack(channel_id: str, is_dm: bool) -> ChatRef:
    kind = ChatKind.DM if is_dm else ChatKind.ROOM
    return ChatRef(transport_id="slack", native_id=channel_id, kind=kind)


def _message_ref_from_slack(channel_id: str, ts: str, is_dm: bool) -> MessageRef:
    chat = _chat_ref_from_slack(channel_id, is_dm)
    return MessageRef(transport_id="slack", native_id=ts, chat=chat)


def _identity_from_slack_event(user_id: str, *, display_name: str = "", is_bot: bool = False) -> Identity:
    return Identity(
        transport_id="slack",
        native_id=user_id,
        display_name=display_name,
        handle=None,
        is_bot=is_bot,
    )


class SlackTransport:
    TRANSPORT_ID = "slack"

    def __init__(self, app: Any) -> None:
        self._app = app
        self._message_handlers: list[MessageHandler] = []
        self._command_handlers: dict[str, CommandHandler] = {}
        self._button_handlers: list[ButtonHandler] = []
        self._on_ready_callbacks: list[OnReadyCallback] = []
        self._prompt_handlers: list[PromptHandler] = []
        self._msg_counter = itertools.count(1)
        self._prompt_counter = itertools.count(1)
        self._prompt_specs: dict[str, PromptSpec] = {}
        self._bot_user_id: str | None = None

    @classmethod
    def build(cls, bot_token: str, app_token: str) -> "SlackTransport":
        from slack_bolt.async_app import AsyncApp
        app = AsyncApp(token=bot_token)
        t = cls(app)
        t._app_token = app_token
        t._bot_token = bot_token
        return t

    async def start(self) -> None:
        pass  # caller drives SocketModeHandler.start_async()

    async def stop(self) -> None:
        pass

    def on_message(self, handler: MessageHandler) -> None:
        self._message_handlers.append(handler)

    def on_command(self, name: str, handler: CommandHandler) -> None:
        self._command_handlers[name] = handler

    def on_button(self, handler: ButtonHandler) -> None:
        self._button_handlers.append(handler)

    def on_ready(self, callback: OnReadyCallback) -> None:
        self._on_ready_callbacks.append(callback)

    def on_prompt_submit(self, handler: PromptHandler) -> None:
        self._prompt_handlers.append(handler)

    async def send_text(self, chat: ChatRef, text: str, *, buttons: Buttons | None = None, html: bool = False, reply_to: MessageRef | None = None) -> MessageRef:
        raise NotImplementedError("implemented in Task 2")

    async def edit_text(self, msg: MessageRef, text: str, *, buttons: Buttons | None = None, html: bool = False) -> None:
        raise NotImplementedError("implemented in Task 2")

    async def send_file(self, chat: ChatRef, path: Path, *, caption: str | None = None, display_name: str | None = None) -> MessageRef:
        raise NotImplementedError("implemented in Task 2")

    async def send_voice(self, chat: ChatRef, path: Path, *, reply_to: MessageRef | None = None) -> MessageRef:
        raise NotImplementedError("implemented in Task 2")

    async def send_typing(self, chat: ChatRef) -> None:
        pass  # Slack typing indicators are not supported via Web API in a simple way

    async def open_prompt(self, chat: ChatRef, spec: PromptSpec, *, reply_to: MessageRef | None = None) -> PromptRef:
        raise NotImplementedError("implemented in Task 4")

    async def update_prompt(self, prompt: PromptRef, spec: PromptSpec) -> None:
        raise NotImplementedError("implemented in Task 4")

    async def close_prompt(self, prompt: PromptRef, *, final_text: str | None = None) -> None:
        raise NotImplementedError("implemented in Task 4")
```

- [ ] **Step 5: Run to confirm skeleton tests pass**

```
pytest tests/transport/test_slack_transport.py -k "importable or transport_id or mention_regex or channel_mention" -v
```
Expected: all four PASS.

- [ ] **Step 6: Commit the transport skeleton**

```bash
git add pyproject.toml src/link_project_to_chat/transport/slack.py tests/transport/test_slack_transport.py
git commit -m "feat: add SlackTransport skeleton, slack-bolt dep, and U/W/B mention regex"
```

#### Sub-step A — `SlackConfig` + `SlackProjectOverride` dataclasses

- [ ] **Step 7: Write failing dataclass tests**

Append to `tests/test_config.py`:

```python
def test_slack_config_defaults():
    from link_project_to_chat.config import SlackConfig

    cfg = SlackConfig()
    assert cfg.bot_token == ""
    assert cfg.app_token == ""
    assert cfg.workspace_id == ""
    assert cfg.default_channel_id == ""
    assert cfg.socket_mode_enabled is True


def test_slack_project_override_defaults_all_optional():
    from link_project_to_chat.config import SlackProjectOverride

    override = SlackProjectOverride()

    # Every field defaults to None so a project can opt in field-by-field.
    assert override.bot_token is None
    assert override.app_token is None
    assert override.workspace_id is None
    assert override.default_channel_id is None
    assert override.socket_mode_enabled is None


def test_slack_project_override_validate_requires_a_token():
    """A project-level override must carry at least one token; otherwise the
    spawn would have nothing to authenticate with. Mirrors the
    ``port``-is-required check on GoogleChatProjectOverride."""
    from link_project_to_chat.config import ConfigError, SlackProjectOverride

    with pytest.raises(ConfigError, match="token"):
        SlackProjectOverride().validate()
    # bot_token alone is fine (Socket Mode disabled / webhook delivery)
    SlackProjectOverride(bot_token="xoxb-1").validate()
    # app_token alone is fine (Socket Mode without an outbound bot token —
    # operator may scope it via top-level fallback)
    SlackProjectOverride(app_token="xapp-1").validate()


def test_slack_project_override_validate_accepts_both_tokens():
    from link_project_to_chat.config import SlackProjectOverride

    SlackProjectOverride(bot_token="xoxb-1", app_token="xapp-1").validate()
```

- [ ] **Step 8: Verify RED**

```
PYTHONPATH=src python3 -m pytest tests/test_config.py -k "slack_config_defaults or slack_project_override" -q
```
Expected: 4 failed with `ImportError: cannot import name 'SlackConfig'`.

- [ ] **Step 9: Add the dataclasses**

In `src/link_project_to_chat/config.py`, immediately after the existing
`GoogleChatProjectOverride` definition (around line 407), add:

```python
# Mirror: GoogleChatConfig (config.py:353) — top-level Slack operational
# defaults. Every project shares Socket Mode + workspace defaults from this
# block; per-project SlackProjectOverride fields win field-by-field.
@dataclass
class SlackConfig:
    bot_token: str = ""
    app_token: str = ""
    workspace_id: str = ""
    default_channel_id: str = ""
    socket_mode_enabled: bool = True


# Mirror: GoogleChatProjectOverride (config.py:372) — per-project override
# layered on top of SlackConfig. Every field is Optional so a project only
# needs to set the fields that differ from the operational-defaults block.
# At least one of bot_token / app_token is required at validate() time —
# Slack needs *something* to authenticate the spawn.
@dataclass
class SlackProjectOverride:
    bot_token: str | None = None
    app_token: str | None = None
    workspace_id: str | None = None
    default_channel_id: str | None = None
    socket_mode_enabled: bool | None = None

    def validate(self) -> None:
        if not self.bot_token and not self.app_token:
            raise ConfigError(
                "slack per-project override requires at least one of "
                "'bot_token' or 'app_token'"
            )
```

- [ ] **Step 10: Verify GREEN**

```
PYTHONPATH=src python3 -m pytest tests/test_config.py -k "slack_config_defaults or slack_project_override" -q
```
Expected: 4 passed.

- [ ] **Step 11: Commit**

```bash
git add src/link_project_to_chat/config.py tests/test_config.py
git commit -m "feat(config): add SlackConfig + SlackProjectOverride dataclasses"
```

#### Sub-step B — Parse/serialize helpers + `ProjectConfig.slack` field + migration

- [ ] **Step 12: Write failing round-trip + migration tests**

Append to `tests/test_config.py`:

```python
def test_parse_slack_override_minimal():
    from link_project_to_chat.config import _parse_slack_override

    override = _parse_slack_override({"bot_token": "xoxb-1"})
    assert override.bot_token == "xoxb-1"
    assert override.app_token is None


def test_parse_slack_override_full():
    from link_project_to_chat.config import _parse_slack_override

    raw = {
        "bot_token": "xoxb-1",
        "app_token": "xapp-1",
        "workspace_id": "T012",
        "default_channel_id": "C100",
        "socket_mode_enabled": False,
    }
    override = _parse_slack_override(raw)
    assert override.bot_token == "xoxb-1"
    assert override.app_token == "xapp-1"
    assert override.workspace_id == "T012"
    assert override.default_channel_id == "C100"
    assert override.socket_mode_enabled is False


def test_parse_slack_override_no_tokens_raises():
    from link_project_to_chat.config import ConfigError, _parse_slack_override

    with pytest.raises(ConfigError, match="token"):
        _parse_slack_override({"workspace_id": "T012"})


def test_serialize_slack_override_omits_none_fields():
    from link_project_to_chat.config import (
        SlackProjectOverride,
        _serialize_slack_override,
    )

    raw = _serialize_slack_override(SlackProjectOverride(bot_token="xoxb-1"))
    assert raw == {"bot_token": "xoxb-1"}
    assert "app_token" not in raw


def test_project_config_round_trips_slack_override(tmp_path):
    import json
    from link_project_to_chat.config import (
        SlackProjectOverride,
        load_config,
        save_config,
    )

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "projects": {
            "alpha": {
                "path": "/p",
                "telegram_bot_token": "",
                "slack": {
                    "bot_token": "xoxb-1",
                    "app_token": "xapp-1",
                    "workspace_id": "T012",
                },
            }
        }
    }))

    loaded = load_config(cfg_path)
    assert loaded.projects["alpha"].slack == SlackProjectOverride(
        bot_token="xoxb-1",
        app_token="xapp-1",
        workspace_id="T012",
    )

    save_config(loaded, cfg_path)
    raw = json.loads(cfg_path.read_text())
    assert raw["projects"]["alpha"]["slack"] == {
        "bot_token": "xoxb-1",
        "app_token": "xapp-1",
        "workspace_id": "T012",
    }


def test_slack_migration_auto_claims_for_single_project(tmp_path):
    """Mirrors _maybe_migrate_top_level_google_chat: when exactly one
    project exists and has no slack override but the top-level slack block
    is meaningful (a bot_token is set), synthesize an override."""
    import json
    from link_project_to_chat.config import load_config

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "projects": {"solo": {"path": "/p", "telegram_bot_token": ""}},
        "slack": {"bot_token": "xoxb-shared", "app_token": "xapp-shared"},
    }))

    loaded = load_config(cfg_path)
    assert loaded.projects["solo"].slack is not None
    assert loaded.projects["solo"].slack.bot_token == "xoxb-shared"


def test_slack_migration_skips_when_multiple_projects(tmp_path):
    """Ambiguous: don't guess which project gets the shared token."""
    import json
    from link_project_to_chat.config import load_config

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "projects": {
            "a": {"path": "/a", "telegram_bot_token": ""},
            "b": {"path": "/b", "telegram_bot_token": ""},
        },
        "slack": {"bot_token": "xoxb-shared"},
    }))

    loaded = load_config(cfg_path)
    assert loaded.projects["a"].slack is None
    assert loaded.projects["b"].slack is None
    # Top-level kept for the operator to claim via the v1.1 wizard.
    assert loaded.slack.bot_token == "xoxb-shared"
```

- [ ] **Step 13: Verify RED**

```
PYTHONPATH=src python3 -m pytest tests/test_config.py -k "parse_slack or serialize_slack or project_config_round_trips_slack or slack_migration" -q
```
Expected: 7 failed.

- [ ] **Step 14: Add parse/serialize helpers, `ProjectConfig.slack` field, migration**

In `src/link_project_to_chat/config.py`:

1. Add `slack` to `Config`:

```python
@dataclass
class Config:
    # ... existing fields ...
    slack: SlackConfig = field(default_factory=SlackConfig)
```

   …and wire its parse/serialize in `load_config` / `save_config` alongside
   `google_chat` (search for `_parse_google_chat(raw.get("google_chat", ...))`
   and add a sibling call to a new `_parse_slack(...)` helper that returns
   `SlackConfig(**raw)` with type-checked fields).

2. Add the per-project field on `ProjectConfig` (immediately after the new
   `google_chat: "GoogleChatProjectOverride | None" = None`):

```python
slack: "SlackProjectOverride | None" = None
```

3. Add the parse/serialize helpers (near `_parse_google_chat_override` /
   `_serialize_google_chat_override`, around line 980):

```python
def _parse_slack_override(raw: dict) -> "SlackProjectOverride":
    # Mirror: _parse_google_chat_override (config.py:979).
    def _opt_str(key: str) -> str | None:
        value = raw.get(key)
        return str(value) if isinstance(value, str) else None

    def _opt_bool(key: str) -> bool | None:
        value = raw.get(key)
        return bool(value) if isinstance(value, bool) else None

    override = SlackProjectOverride(
        bot_token=_opt_str("bot_token"),
        app_token=_opt_str("app_token"),
        workspace_id=_opt_str("workspace_id"),
        default_channel_id=_opt_str("default_channel_id"),
        socket_mode_enabled=_opt_bool("socket_mode_enabled"),
    )
    override.validate()
    return override


def _serialize_slack_override(override: "SlackProjectOverride") -> dict:
    # Mirror: _serialize_google_chat_override (config.py:1027).
    raw: dict[str, object] = {}
    for field_name in (
        "bot_token",
        "app_token",
        "workspace_id",
        "default_channel_id",
        "socket_mode_enabled",
    ):
        value = getattr(override, field_name)
        if value is not None:
            raw[field_name] = value
    return raw


def _slack_top_is_meaningful(top: "SlackConfig") -> bool:
    """True when the top-level slack block has at least one token set.
    Mirror: _google_chat_top_is_meaningful (config.py:1055)."""
    return bool(top.bot_token or top.app_token)


def _maybe_migrate_top_level_slack(cfg: "Config") -> None:
    """One-shot: when exactly one project exists and has no slack override
    but the top-level slack block carries a token, synthesize an override.
    No-op when zero, multiple, or already-overridden projects exist.
    Mirror: _maybe_migrate_top_level_google_chat (config.py:1065)."""
    top = cfg.slack
    if not _slack_top_is_meaningful(top):
        return
    projects_without_override = [
        name for name, pc in cfg.projects.items() if pc.slack is None
    ]
    if len(cfg.projects) != 1 or len(projects_without_override) != 1:
        return
    sole_name = projects_without_override[0]
    cfg.projects[sole_name].slack = SlackProjectOverride(
        bot_token=top.bot_token or None,
        app_token=top.app_token or None,
        workspace_id=top.workspace_id or None,
    )
```

4. Wire the override into project parse + serialize (mirror the
   `google_chat` branch in the same functions):

```python
raw_slack = raw.get("slack")
slack = _parse_slack_override(raw_slack) if isinstance(raw_slack, dict) else None
```

   …pass `slack=slack` to the `ProjectConfig(...)` constructor.

   For serialize:

```python
if project.slack is not None:
    proj["slack"] = _serialize_slack_override(project.slack)
```

5. Call the migration in `load_config` immediately after
   `_maybe_migrate_top_level_google_chat(config)`:

```python
_maybe_migrate_top_level_slack(config)
```

- [ ] **Step 15: Verify GREEN**

```
PYTHONPATH=src python3 -m pytest tests/test_config.py -k "slack" -q
```
Expected: all slack-related config tests PASS (11 total: 4 dataclass + 7 round-trip/migration).

- [ ] **Step 16: Commit**

```bash
git add src/link_project_to_chat/config.py tests/test_config.py
git commit -m "feat(config): parse/serialize SlackProjectOverride + one-shot migration"
```

#### Sub-step C — `resolve_project_slack` per-field merge resolver

- [ ] **Step 17: Write failing resolver tests**

Create `tests/slack/__init__.py` (empty) and `tests/slack/test_resolver.py`:

```python
"""Per-field merge tests for resolve_project_slack.

Mirror: tests/google_chat/test_resolver.py — same five scenarios,
re-typed for Slack tokens instead of Google Chat ports/SA files.
"""
from __future__ import annotations

import pytest

from link_project_to_chat.config import (
    Config,
    ProjectConfig,
    SlackConfig,
    SlackProjectOverride,
)
from link_project_to_chat.slack.resolver import resolve_project_slack


def _config(top_level: SlackConfig | None, projects: dict[str, ProjectConfig]) -> Config:
    return Config(
        projects=projects,
        slack=top_level if top_level is not None else SlackConfig(),
    )


def test_no_override_no_top_level_returns_none():
    config = _config(None, {"alpha": ProjectConfig(path="/p", telegram_bot_token="")})
    assert resolve_project_slack("alpha", config) is None


def test_top_level_only_returns_top_level():
    top = SlackConfig(bot_token="xoxb-shared", app_token="xapp-shared", workspace_id="T012")
    config = _config(top, {"alpha": ProjectConfig(path="/p", telegram_bot_token="")})
    resolved = resolve_project_slack("alpha", config)
    assert resolved is not None
    assert resolved.bot_token == "xoxb-shared"


def test_override_replaces_per_field():
    top = SlackConfig(
        bot_token="xoxb-shared",
        app_token="xapp-shared",
        workspace_id="T012",
        socket_mode_enabled=True,
    )
    config = _config(top, {
        "alpha": ProjectConfig(
            path="/p",
            telegram_bot_token="",
            slack=SlackProjectOverride(
                bot_token="xoxb-alpha",
                workspace_id="T999",
            ),
        )
    })

    resolved = resolve_project_slack("alpha", config)
    assert resolved is not None
    # Per-project wins:
    assert resolved.bot_token == "xoxb-alpha"
    assert resolved.workspace_id == "T999"
    # Operational defaults inherited from top-level:
    assert resolved.app_token == "xapp-shared"
    assert resolved.socket_mode_enabled is True


def test_override_alone_returns_merged_config():
    """Override with only bot_token set, no top-level — returns the override
    merged onto the empty defaults. Downstream validators decide whether
    the merge is complete enough to start."""
    config = _config(None, {
        "alpha": ProjectConfig(
            path="/p",
            telegram_bot_token="",
            slack=SlackProjectOverride(bot_token="xoxb-alpha"),
        )
    })
    resolved = resolve_project_slack("alpha", config)
    assert resolved is not None
    assert resolved.bot_token == "xoxb-alpha"
    assert resolved.app_token == ""  # default — caller's responsibility to handle


def test_unknown_project_returns_none():
    config = _config(SlackConfig(bot_token="xoxb-shared"), {})
    assert resolve_project_slack("does-not-exist", config) is None
```

- [ ] **Step 18: Verify RED**

```
PYTHONPATH=src python3 -m pytest tests/slack/test_resolver.py -q
```
Expected: 5 failed on `ImportError: cannot import name 'resolve_project_slack'`.

- [ ] **Step 19: Implement the resolver**

Create `src/link_project_to_chat/slack/__init__.py` (empty).

Create `src/link_project_to_chat/slack/resolver.py`:

```python
"""Merge per-project slack overrides onto the top-level block.

Mirror: src/link_project_to_chat/google_chat/resolver.py
        resolve_project_google_chat (the GChat shipping arc proved the
        per-field replace() pattern works cleanly across transports).
"""
from __future__ import annotations

from dataclasses import fields, replace

from link_project_to_chat.config import (
    Config,
    SlackConfig,
    SlackProjectOverride,
    _slack_top_is_meaningful,
)


def resolve_project_slack(project_name: str, config: Config) -> SlackConfig | None:
    """Return the effective SlackConfig for ``project_name``, or None.

    None means the project has no slack configured (neither override nor a
    non-empty top-level block). The returned config is the result of
    overlaying any per-project override field-by-field onto the top-level
    block. Downstream validators decide whether the merged result is
    complete enough to actually start a bot.

    "Non-empty top-level" means at least one of ``bot_token`` or ``app_token``
    is set — workspace_id alone is not enough to authenticate.
    """
    project = config.projects.get(project_name)
    if project is None:
        return None

    override = project.slack
    top_level = config.slack

    top_is_meaningful = top_level is not None and _slack_top_is_meaningful(top_level)
    if override is None and not top_is_meaningful:
        return None

    base = top_level if top_level is not None else SlackConfig()
    if override is None:
        return base

    # Per-field replace: every non-None override field wins.
    merged_kwargs = {}
    for f in fields(SlackProjectOverride):
        value = getattr(override, f.name)
        if value is not None:
            merged_kwargs[f.name] = value
    return replace(base, **merged_kwargs)
```

- [ ] **Step 20: Verify GREEN**

```
PYTHONPATH=src python3 -m pytest tests/slack/test_resolver.py -q
```
Expected: 5 passed.

- [ ] **Step 21: Commit**

```bash
git add src/link_project_to_chat/slack/__init__.py src/link_project_to_chat/slack/resolver.py tests/slack/test_resolver.py
git commit -m "feat(slack): resolve_project_slack per-field merge helper"
```

---

### Task 2: Implement outbound methods

**Files:**
- Modify: `src/link_project_to_chat/transport/slack.py`
- Modify: `tests/transport/test_slack_transport.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/transport/test_slack_transport.py`:

```python
from unittest.mock import AsyncMock, MagicMock
from types import SimpleNamespace
from link_project_to_chat.transport import ChatKind, ChatRef, MessageRef
from link_project_to_chat.transport.slack import SlackTransport


def _make_mock_transport() -> SlackTransport:
    """SlackTransport with a mocked slack_bolt AsyncApp."""
    client = MagicMock()
    client.chat_postMessage = AsyncMock(return_value={"ok": True, "ts": "1234.0001", "channel": "C100"})
    client.chat_update = AsyncMock(return_value={"ok": True})
    client.files_uploadV2 = AsyncMock(return_value={"ok": True, "file": {"permalink": "https://slack.com/f1"}})

    app = MagicMock()
    app.client = client

    t = SlackTransport(app)
    t._bot_user_id = "B001"
    return t


def _room() -> ChatRef:
    return ChatRef(transport_id="slack", native_id="C100", kind=ChatKind.ROOM)


async def test_send_text_returns_message_ref():
    t = _make_mock_transport()
    ref = await t.send_text(_room(), "hello slack")
    assert isinstance(ref, MessageRef)
    assert ref.transport_id == "slack"
    assert ref.chat == _room()


async def test_send_text_calls_chat_post_message():
    t = _make_mock_transport()
    await t.send_text(_room(), "hello slack")
    t._app.client.chat_postMessage.assert_called_once()
    call_kwargs = t._app.client.chat_postMessage.call_args.kwargs
    assert call_kwargs["channel"] == "C100"
    assert "hello slack" in call_kwargs.get("text", "") or "hello slack" in str(call_kwargs.get("blocks", ""))


async def test_edit_text_calls_chat_update():
    t = _make_mock_transport()
    ref = await t.send_text(_room(), "original")
    await t.edit_text(ref, "updated")
    t._app.client.chat_update.assert_called_once()


async def test_send_file_returns_message_ref(tmp_path):
    t = _make_mock_transport()
    f = tmp_path / "doc.txt"
    f.write_bytes(b"content")
    ref = await t.send_file(_room(), f)
    assert isinstance(ref, MessageRef)


async def test_send_voice_returns_message_ref(tmp_path):
    t = _make_mock_transport()
    f = tmp_path / "voice.opus"
    f.write_bytes(b"fake opus")
    ref = await t.send_voice(_room(), f)
    assert isinstance(ref, MessageRef)
```

- [ ] **Step 2: Run to confirm failures**

```
pytest tests/transport/test_slack_transport.py -k "send or edit" -v
```
Expected: all fail with `NotImplementedError`.

- [ ] **Step 3: Implement outbound methods in `slack.py`**

Replace the `raise NotImplementedError` stubs for outbound methods:

```python
async def send_text(
    self,
    chat: ChatRef,
    text: str,
    *,
    buttons: Buttons | None = None,
    html: bool = False,
    reply_to: MessageRef | None = None,
) -> MessageRef:
    clean_text = self._html_to_slack(text) if html else text
    kwargs: dict[str, Any] = {"channel": chat.native_id, "text": clean_text}
    if buttons:
        kwargs["blocks"] = self._buttons_to_blocks(buttons, clean_text)
        kwargs["text"] = clean_text  # fallback for notifications
    if reply_to:
        kwargs["thread_ts"] = reply_to.native_id
    resp = await self._app.client.chat_postMessage(**kwargs)
    ts = resp["ts"]
    self._ts_cache[ts] = {"channel": chat.native_id, "text": clean_text}
    return MessageRef(transport_id=self.TRANSPORT_ID, native_id=ts, chat=chat)

async def edit_text(
    self,
    msg: MessageRef,
    text: str,
    *,
    buttons: Buttons | None = None,
    html: bool = False,
) -> None:
    clean_text = self._html_to_slack(text) if html else text
    kwargs: dict[str, Any] = {
        "channel": msg.chat.native_id,
        "ts": msg.native_id,
        "text": clean_text,
    }
    if buttons:
        kwargs["blocks"] = self._buttons_to_blocks(buttons, clean_text)
    await self._app.client.chat_update(**kwargs)
    if msg.native_id in self._ts_cache:
        self._ts_cache[msg.native_id]["text"] = clean_text

async def send_file(
    self,
    chat: ChatRef,
    path: Path,
    *,
    caption: str | None = None,
    display_name: str | None = None,
) -> MessageRef:
    resp = await self._app.client.files_uploadV2(
        channel=chat.native_id,
        file=str(path),
        filename=display_name or path.name,
        initial_comment=caption or "",
    )
    ts = str(next(self._msg_counter))
    return MessageRef(transport_id=self.TRANSPORT_ID, native_id=ts, chat=chat)

async def send_voice(
    self,
    chat: ChatRef,
    path: Path,
    *,
    reply_to: MessageRef | None = None,
) -> MessageRef:
    kwargs: dict[str, Any] = {
        "channel": chat.native_id,
        "file": str(path),
        "filename": path.name,
    }
    await self._app.client.files_uploadV2(**kwargs)
    ts = str(next(self._msg_counter))
    return MessageRef(transport_id=self.TRANSPORT_ID, native_id=ts, chat=chat)
```

Add helpers and `__init__` additions:

In `__init__`, add:
```python
self._ts_cache: dict[str, dict[str, Any]] = {}
```

Add helpers:
```python
@staticmethod
def _html_to_slack(text: str) -> str:
    """Convert basic HTML to Slack mrkdwn. Falls back to plain text for unsupported tags."""
    import re
    text = re.sub(r"<b>(.*?)</b>", r"*\1*", text, flags=re.DOTALL)
    text = re.sub(r"<i>(.*?)</i>", r"_\1_", text, flags=re.DOTALL)
    text = re.sub(r"<code>(.*?)</code>", r"`\1`", text, flags=re.DOTALL)
    text = re.sub(r"<pre>(.*?)</pre>", r"```\1```", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", "", text)  # strip remaining tags
    return text

@staticmethod
def _buttons_to_blocks(buttons: Buttons, text: str) -> list[dict]:
    blocks: list[dict] = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
    for row in buttons.rows:
        elements = [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": btn.label},
                "value": btn.value,
                "action_id": f"btn_{btn.value}",
            }
            for btn in row
        ]
        blocks.append({"type": "actions", "elements": elements})
    return blocks
```

- [ ] **Step 4: Run to confirm pass**

```
pytest tests/transport/test_slack_transport.py -k "send or edit" -v
```
Expected: all 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/transport/slack.py tests/transport/test_slack_transport.py
git commit -m "feat: implement SlackTransport outbound methods (send_text, edit, files)"
```

---

### Task 3: Implement `/lp2c` command bridge

**Files:**
- Modify: `src/link_project_to_chat/transport/slack.py`
- Modify: `tests/transport/test_slack_transport.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/transport/test_slack_transport.py`:

```python
async def test_lp2c_command_dispatched():
    from link_project_to_chat.transport import CommandInvocation

    t = _make_mock_transport()
    seen: list[CommandInvocation] = []

    async def handler(ci: CommandInvocation) -> None:
        seen.append(ci)

    t.on_command("projects", handler)

    await t._handle_lp2c_command(
        command={"text": "projects", "user_id": "U001", "channel_id": "C100", "channel_name": "general"},
        ack=AsyncMock(),
        say=AsyncMock(),
    )

    assert len(seen) == 1
    assert seen[0].name == "projects"
    assert seen[0].raw_text == "/lp2c projects"


async def test_lp2c_command_with_args():
    from link_project_to_chat.transport import CommandInvocation

    t = _make_mock_transport()
    seen: list[CommandInvocation] = []

    async def handler(ci: CommandInvocation) -> None:
        seen.append(ci)

    t.on_command("model", handler)

    await t._handle_lp2c_command(
        command={"text": "model set sonnet", "user_id": "U001", "channel_id": "C100", "channel_name": "general"},
        ack=AsyncMock(),
        say=AsyncMock(),
    )

    assert seen[0].args == ["set", "sonnet"]
    assert seen[0].raw_text == "/lp2c model set sonnet"


async def test_unknown_command_calls_ack():
    t = _make_mock_transport()
    ack = AsyncMock()

    await t._handle_lp2c_command(
        command={"text": "unknown_cmd", "user_id": "U001", "channel_id": "C100", "channel_name": "general"},
        ack=ack,
        say=AsyncMock(),
    )

    ack.assert_called_once()
```

- [ ] **Step 2: Run to confirm failures**

```
pytest tests/transport/test_slack_transport.py -k "command" -v
```
Expected: `AttributeError` — `_handle_lp2c_command` does not exist.

- [ ] **Step 3: Implement `_handle_lp2c_command` and `attach_slack_routing`**

```python
async def _handle_lp2c_command(
    self,
    command: dict[str, Any],
    ack: Any,
    say: Any,
) -> None:
    """Parse /lp2c <name> [args...] and dispatch to registered handler."""
    await ack()
    text = (command.get("text") or "").strip()
    parts = text.split() if text else []
    name = parts[0] if parts else "help"
    extra_args = parts[1:] if len(parts) > 1 else []
    raw_text = f"/lp2c {text}".strip()

    channel_id = command.get("channel_id", "")
    user_id = command.get("user_id", "")
    is_dm = channel_id.startswith("D")

    chat = _chat_ref_from_slack(channel_id, is_dm)
    sender = _identity_from_slack_event(user_id)
    msg_ref = MessageRef(
        transport_id=self.TRANSPORT_ID,
        native_id=str(next(self._msg_counter)),
        chat=chat,
    )
    ci = CommandInvocation(
        chat=chat, sender=sender, name=name,
        args=extra_args, raw_text=raw_text, message=msg_ref,
    )
    handler = self._command_handlers.get(name)
    if handler:
        await handler(ci)
    else:
        await say(text=f"Unknown command: `{name}`. Try `/lp2c help`.")

def attach_slack_routing(self) -> None:
    """Register slack_bolt event handlers and slash command on the app."""
    transport = self

    @self._app.command("/lp2c")
    async def handle_lp2c(ack, command, say):
        await transport._handle_lp2c_command(command=command, ack=ack, say=say)

    @self._app.event("message")
    async def handle_message(event, say):
        await transport._dispatch_slack_message(event)

    @self._app.action(re.compile(r"^btn_.*"))
    async def handle_button_action(ack, action, body):
        await ack()
        await transport._dispatch_slack_button(action, body)

    @self._app.view(re.compile(r"^prompt_.*"))
    async def handle_modal_submit(ack, body, view):
        await ack()
        await transport._dispatch_slack_modal(body, view)
```

- [ ] **Step 4: Run to confirm pass**

```
pytest tests/transport/test_slack_transport.py -k "command" -v
```
Expected: all 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/transport/slack.py tests/transport/test_slack_transport.py
git commit -m "feat: add /lp2c command dispatch to SlackTransport"
```

---

### Task 4: Implement prompt mapping (Slack modals + Block Kit)

**Files:**
- Modify: `src/link_project_to_chat/transport/slack.py`
- Modify: `tests/transport/test_slack_transport.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/transport/test_slack_transport.py`:

```python
async def test_open_text_prompt_opens_modal():
    from link_project_to_chat.transport import PromptKind, PromptRef, PromptSpec

    t = _make_mock_transport()
    t._app.client.views_open = AsyncMock(return_value={"ok": True, "view": {"id": "V001"}})

    chat = _room()
    spec = PromptSpec(key="name", title="Your Name", body="Enter name", kind=PromptKind.TEXT)

    t._pending_trigger_id = "trigger_123"
    ref = await t.open_prompt(chat, spec)

    assert isinstance(ref, PromptRef)
    assert ref.key == "name"
    t._app.client.views_open.assert_called_once()
    call_kwargs = t._app.client.views_open.call_args.kwargs
    assert call_kwargs["trigger_id"] == "trigger_123"


async def test_open_choice_prompt_sends_block_message():
    from link_project_to_chat.transport import ButtonStyle, PromptKind, PromptOption, PromptSpec

    t = _make_mock_transport()
    spec = PromptSpec(
        key="model",
        title="Choose model",
        body="Select the model",
        kind=PromptKind.CHOICE,
        options=[PromptOption(value="sonnet", label="Sonnet"), PromptOption(value="opus", label="Opus")],
    )
    ref = await t.open_prompt(_room(), spec)
    assert isinstance(ref, PromptRef)
    t._app.client.chat_postMessage.assert_called_once()


async def test_modal_submit_fires_prompt_handler():
    from link_project_to_chat.transport import PromptKind, PromptSpec, PromptSubmission

    t = _make_mock_transport()
    t._app.client.views_open = AsyncMock(return_value={"ok": True, "view": {"id": "V001"}})
    t._pending_trigger_id = "t1"

    spec = PromptSpec(key="name", title="Name", body="Enter name", kind=PromptKind.TEXT)
    seen: list[PromptSubmission] = []

    async def on_submit(sub: PromptSubmission) -> None:
        seen.append(sub)

    t.on_prompt_submit(on_submit)
    ref = await t.open_prompt(_room(), spec)

    # Simulate Slack modal submission callback
    body = {
        "user": {"id": "U001"},
        "container": {"channel_id": "C100"},
        "view": {
            "callback_id": f"prompt_{ref.native_id}",
            "state": {"values": {ref.native_id: {"answer": {"value": "Alice"}}}},
        },
    }
    view = body["view"]
    await t._dispatch_slack_modal(body, view)

    assert len(seen) == 1
    assert seen[0].text == "Alice"
    assert seen[0].prompt == ref
```

- [ ] **Step 2: Run to confirm failures**

```
pytest tests/transport/test_slack_transport.py -k "prompt" -v
```
Expected: `NotImplementedError` from the stubs.

- [ ] **Step 3: Implement prompt methods in `slack.py`**

Replace `open_prompt`, `update_prompt`, `close_prompt` stubs:

```python
async def open_prompt(
    self,
    chat: ChatRef,
    spec: PromptSpec,
    *,
    reply_to: MessageRef | None = None,
) -> PromptRef:
    from link_project_to_chat.transport.base import PromptKind

    native_id = str(next(self._prompt_counter))
    ref = PromptRef(
        transport_id=self.TRANSPORT_ID,
        native_id=native_id,
        chat=chat,
        key=spec.key,
    )
    self._prompt_specs[native_id] = spec

    if spec.kind in (PromptKind.TEXT, PromptKind.SECRET):
        trigger_id = getattr(self, "_pending_trigger_id", None)
        if trigger_id:
            modal_view = self._build_modal_view(native_id, spec)
            await self._app.client.views_open(trigger_id=trigger_id, view=modal_view)
            self._pending_trigger_id = None
        else:
            # Fallback: send ephemeral message asking for text
            await self._app.client.chat_postMessage(
                channel=chat.native_id,
                text=f"*{spec.title}*\n{spec.body}",
            )
    else:
        # CHOICE / CONFIRM / DISPLAY: send Block Kit actions message
        blocks = self._build_choice_blocks(native_id, spec)
        resp = await self._app.client.chat_postMessage(
            channel=chat.native_id,
            text=spec.title,
            blocks=blocks,
        )
        self._prompt_ts_cache[native_id] = resp.get("ts", "")

    return ref

async def update_prompt(self, prompt: PromptRef, spec: PromptSpec) -> None:
    ts = self._prompt_ts_cache.get(prompt.native_id)
    if ts:
        blocks = self._build_choice_blocks(prompt.native_id, spec)
        await self._app.client.chat_update(
            channel=prompt.chat.native_id,
            ts=ts,
            text=spec.title,
            blocks=blocks,
        )

async def close_prompt(self, prompt: PromptRef, *, final_text: str | None = None) -> None:
    ts = self._prompt_ts_cache.pop(prompt.native_id, None)
    if ts and final_text:
        await self._app.client.chat_update(
            channel=prompt.chat.native_id,
            ts=ts,
            text=final_text,
            blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": final_text}}],
        )
    self._prompt_specs.pop(prompt.native_id, None)
```

Add private helpers:

```python
def _build_modal_view(self, native_id: str, spec: PromptSpec) -> dict[str, Any]:
    from link_project_to_chat.transport.base import PromptKind
    return {
        "type": "modal",
        "callback_id": f"prompt_{native_id}",
        "title": {"type": "plain_text", "text": spec.title[:24]},
        "submit": {"type": "plain_text", "text": spec.submit_label},
        "blocks": [
            {
                "type": "input",
                "block_id": native_id,
                "label": {"type": "plain_text", "text": spec.body or spec.title},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "answer",
                    "placeholder": {"type": "plain_text", "text": spec.placeholder or ""},
                    "multiline": False,
                },
            }
        ],
    }

def _build_choice_blocks(self, native_id: str, spec: PromptSpec) -> list[dict[str, Any]]:
    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{spec.title}*\n{spec.body}"}}
    ]
    elements = [
        {
            "type": "button",
            "text": {"type": "plain_text", "text": opt.label},
            "value": opt.value,
            "action_id": f"btn_{opt.value}_{native_id}",
        }
        for opt in spec.options
    ]
    if elements:
        blocks.append({"type": "actions", "elements": elements})
    return blocks

async def _dispatch_slack_modal(self, body: dict[str, Any], view: dict[str, Any]) -> None:
    callback_id: str = view.get("callback_id", "")
    if not callback_id.startswith("prompt_"):
        return
    native_id = callback_id[len("prompt_"):]
    spec = self._prompt_specs.get(native_id)
    ref = PromptRef(
        transport_id=self.TRANSPORT_ID,
        native_id=native_id,
        chat=_chat_ref_from_slack(
            body.get("container", {}).get("channel_id", ""),
            is_dm=False,
        ),
        key=spec.key if spec else "",
    )
    user_id = body.get("user", {}).get("id", "")
    sender = _identity_from_slack_event(user_id)
    values = view.get("state", {}).get("values", {})
    answer_value = values.get(native_id, {}).get("answer", {}).get("value", "")
    sub = PromptSubmission(
        chat=ref.chat, sender=sender, prompt=ref, text=answer_value
    )
    for h in self._prompt_handlers:
        await h(sub)

async def _dispatch_slack_button(self, action: dict[str, Any], body: dict[str, Any]) -> None:
    action_id: str = action.get("action_id", "")
    value: str = action.get("value", "")
    channel_id = body.get("container", {}).get("channel_id", "")
    user_id = body.get("user", {}).get("id", "")
    ts = action.get("block_id", "0")

    # Check if this is a prompt choice button (action_id starts with btn_ and ends with _<native_id>)
    if action_id.startswith("btn_"):
        parts = action_id.split("_")
        native_id = parts[-1] if len(parts) > 2 else ""
        spec = self._prompt_specs.get(native_id)
        if spec:
            ref = PromptRef(
                transport_id=self.TRANSPORT_ID,
                native_id=native_id,
                chat=_chat_ref_from_slack(channel_id, is_dm=False),
                key=spec.key,
            )
            sender = _identity_from_slack_event(user_id)
            sub = PromptSubmission(
                chat=ref.chat, sender=sender, prompt=ref, option=value
            )
            for h in self._prompt_handlers:
                await h(sub)
            return

    # Regular button click
    is_dm = channel_id.startswith("D")
    chat = _chat_ref_from_slack(channel_id, is_dm)
    msg_ref = MessageRef(transport_id=self.TRANSPORT_ID, native_id=ts, chat=chat)
    sender = _identity_from_slack_event(user_id)
    click = ButtonClick(chat=chat, message=msg_ref, sender=sender, value=value)
    for h in self._button_handlers:
        await h(click)
```

In `__init__`, add:
```python
self._prompt_ts_cache: dict[str, str] = {}
self._pending_trigger_id: str | None = None
```

- [ ] **Step 4: Run to confirm pass**

```
pytest tests/transport/test_slack_transport.py -k "prompt" -v
```
Expected: all 3 prompt tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/transport/slack.py tests/transport/test_slack_transport.py
git commit -m "feat: implement SlackTransport prompt mapping (modals + Block Kit actions)"
```

---

### Task 5: Implement inbound message dispatch with structured mentions

**Files:**
- Modify: `src/link_project_to_chat/transport/slack.py`
- Modify: `tests/transport/test_slack_transport.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/transport/test_slack_transport.py`:

```python
async def test_parse_mentions_from_slack_text():
    from link_project_to_chat.transport.slack import _parse_mentions

    mentions = _parse_mentions("<@U111> hello <@U222|alice>", client=None)
    assert len(mentions) == 2
    assert mentions[0].native_id == "U111"
    assert mentions[1].native_id == "U222"


async def test_dispatch_slack_message_populates_mentions():
    from link_project_to_chat.transport import IncomingMessage

    t = _make_mock_transport()
    received: list[IncomingMessage] = []

    async def handler(msg: IncomingMessage) -> None:
        received.append(msg)

    t.on_message(handler)
    t._bot_user_id = "B001"

    event = {
        "type": "message",
        "user": "U001",
        "text": "<@B001> can you help?",
        "channel": "C100",
        "ts": "1234.0001",
    }
    await t._dispatch_slack_message(event)

    assert len(received) == 1
    assert len(received[0].mentions) == 1
    assert received[0].mentions[0].native_id == "B001"


async def test_bot_own_messages_ignored():
    from link_project_to_chat.transport import IncomingMessage

    t = _make_mock_transport()
    received: list[IncomingMessage] = []
    t.on_message(lambda msg: received.append(msg))
    t._bot_user_id = "B001"

    # Message from the bot itself
    event = {
        "type": "message",
        "user": "B001",
        "text": "from myself",
        "channel": "C100",
        "ts": "1234.0001",
    }
    await t._dispatch_slack_message(event)
    assert received == []


async def test_own_bot_message_subtype_is_ignored():
    """Self-echoes carry subtype='bot_message' AND bot_id matching our
    bot user ID. The dispatcher must filter them out so we don't loop on
    our own posts."""
    from link_project_to_chat.transport import IncomingMessage

    t = _make_mock_transport()
    received: list[IncomingMessage] = []
    t.on_message(lambda msg: received.append(msg))
    t._bot_user_id = "B001"

    event = {
        "type": "message",
        "subtype": "bot_message",
        "bot_id": "B001",
        "bot_profile": {"id": "B001", "name": "self"},
        "text": "echo of our own post",
        "channel": "C100",
        "ts": "1234.0009",
    }
    await t._dispatch_slack_message(event)
    assert received == []


async def test_other_bot_message_subtype_is_dispatched():
    """Lesson from Google Chat v1.0: a blanket subtype=='bot_message' skip
    silently drops every peer bot's messages. The narrow rule is:
    skip only when bot_id (or user) matches our OWN bot user ID. Messages
    from OTHER bots must dispatch, with sender.is_bot=True."""
    from link_project_to_chat.transport import IncomingMessage

    t = _make_mock_transport()
    received: list[IncomingMessage] = []
    t.on_message(lambda msg: received.append(msg))
    t._bot_user_id = "B001"

    event = {
        "type": "message",
        "subtype": "bot_message",
        "bot_id": "B999",  # different bot
        "bot_profile": {"id": "B999", "name": "peer-bot"},
        "text": "hello from peer bot",
        "channel": "C100",
        "ts": "1234.0010",
    }
    await t._dispatch_slack_message(event)
    assert len(received) == 1
    assert received[0].sender.is_bot is True
    assert received[0].sender.native_id == "B999"


async def test_dispatch_slack_message_dm():
    from link_project_to_chat.transport import IncomingMessage, ChatKind

    t = _make_mock_transport()
    received: list[IncomingMessage] = []
    t.on_message(lambda msg: received.append(msg))
    t._bot_user_id = "B001"

    event = {
        "type": "message",
        "user": "U005",
        "text": "hello in dm",
        "channel": "D100",  # DM channel starts with D
        "ts": "1234.0002",
    }
    await t._dispatch_slack_message(event)

    assert len(received) == 1
    assert received[0].chat.kind == ChatKind.DM
```

- [ ] **Step 2: Run to confirm failures**

```
pytest tests/transport/test_slack_transport.py -k "mention or dispatch or dm" -v
```
Expected: `AttributeError` — `_dispatch_slack_message` does not exist.

- [ ] **Step 3: Implement `_dispatch_slack_message` and inject helpers**

> **Lesson from the Google Chat v1.0 → v1.2 shipping arc:** a blanket
> `subtype == "bot_message"` skip silently drops every peer-bot message in
> the same channel — including other team bots. Slack tags every Slack-app
> message with that subtype, so the filter has to be narrower: skip only the
> bot's *own* echoes (matched via `bot_id == self._bot_user_id` or the user
> matches our own bot user ID). Bot-to-bot messages from other bots still
> dispatch, with `sender.is_bot=True` set from `bot_profile` presence.

```python
async def _dispatch_slack_message(self, event: dict[str, Any]) -> None:
    """Normalize a Slack message event into IncomingMessage and dispatch.

    Subtype filtering: ``message_changed`` / ``message_deleted`` are
    structural edits, drop them. ``bot_message`` is NOT a blanket drop —
    only self-echoes are ignored. Bot-to-bot routing depends on being
    able to see other bots' messages.
    """
    if event.get("subtype") in ("message_changed", "message_deleted"):
        return

    # Detect bot-sourced messages via bot_profile (Slack always sets this on
    # bot-authored events). Subtype/user-ID prefix alone is unreliable:
    # Slack apps post as their bot user ID (B...) but also as other shapes
    # depending on how the app was registered.
    event_user = event.get("user", "")
    bot_profile = event.get("bot_profile")
    is_bot = bool(bot_profile) or event_user.startswith("B")

    # Skip ONLY if this is a self-message (echo of our own bot's posts).
    # Compare both bot_id (set on bot_message subtype) and user (set on
    # plain message events authored by the bot user).
    if is_bot and (
        event.get("bot_id") == self._bot_user_id
        or event_user == self._bot_user_id
    ):
        return  # ignore own messages

    channel_id = event.get("channel", "")
    is_dm = channel_id.startswith("D")
    text = event.get("text", "")
    mentions = _parse_mentions(text, self._app.client)

    chat = _chat_ref_from_slack(channel_id, is_dm)
    sender = _identity_from_slack_event(
        event_user or event.get("bot_id", ""),
        is_bot=is_bot,
    )
    incoming = IncomingMessage(
        chat=chat,
        sender=sender,
        text=text,
        files=[],
        reply_to=None,
        mentions=mentions,
        is_relayed_bot_to_bot=False,
    )
    for h in self._message_handlers:
        await h(incoming)

# ── Test injection helpers ─────────────────────────────────────────────
async def inject_message(
    self,
    chat: ChatRef,
    sender: Identity,
    text: str,
    *,
    files: list[IncomingFile] | None = None,
    reply_to: MessageRef | None = None,
    mentions: list[Identity] | None = None,
) -> None:
    msg = IncomingMessage(
        chat=chat, sender=sender, text=text,
        files=files or [], reply_to=reply_to, mentions=mentions or [],
    )
    for h in self._message_handlers:
        await h(msg)

async def inject_command(
    self,
    chat: ChatRef,
    sender: Identity,
    name: str,
    *,
    args: list[str],
    raw_text: str,
) -> None:
    msg_ref = MessageRef(
        transport_id=self.TRANSPORT_ID,
        native_id=str(next(self._msg_counter)),
        chat=chat,
    )
    ci = CommandInvocation(
        chat=chat, sender=sender, name=name,
        args=args, raw_text=raw_text, message=msg_ref,
    )
    handler = self._command_handlers.get(name)
    if handler:
        await handler(ci)

async def inject_button_click(self, message: MessageRef, sender: Identity, *, value: str) -> None:
    click = ButtonClick(chat=message.chat, message=message, sender=sender, value=value)
    for h in self._button_handlers:
        await h(click)

async def inject_prompt_submit(
    self,
    prompt: PromptRef,
    sender: Identity,
    *,
    text: str | None = None,
    option: str | None = None,
) -> None:
    sub = PromptSubmission(
        chat=prompt.chat, sender=sender, prompt=prompt, text=text, option=option
    )
    for h in self._prompt_handlers:
        await h(sub)
```

- [ ] **Step 4: Run to confirm pass**

```
pytest tests/transport/test_slack_transport.py -k "mention or dispatch or dm or parse" -v
```
Expected: all 4 tests PASS.

- [ ] **Step 5: Run full Slack test suite**

```
pytest tests/transport/test_slack_transport.py -v
```
Expected: all tests PASS.

- [ ] **Step 6: Commit**

```bash
git add src/link_project_to_chat/transport/slack.py tests/transport/test_slack_transport.py
git commit -m "feat: SlackTransport inbound dispatch with structured mention parsing + inject helpers"
```

---

### Task 6: Add `SlackTransport` to contract tests and export

**Files:**
- Modify: `src/link_project_to_chat/transport/__init__.py`
- Modify: `tests/transport/test_contract.py`

- [ ] **Step 1: Export `SlackTransport`**

In `transport/__init__.py`, add:

```python
from .slack import SlackTransport

__all__ = [
    # ... existing ...
    "SlackTransport",
]
```

- [ ] **Step 2: Add `SlackTransport` to the contract test fixture**

In `tests/transport/test_contract.py`, add a factory:

```python
from link_project_to_chat.transport.slack import SlackTransport


def _make_slack_transport_with_inject() -> SlackTransport:
    """SlackTransport with mocked slack_bolt app for contract testing."""
    from unittest.mock import AsyncMock, MagicMock

    client = MagicMock()
    client.chat_postMessage = AsyncMock(
        return_value={"ok": True, "ts": "1000.0001", "channel": "C1"}
    )
    client.chat_update = AsyncMock(return_value={"ok": True})
    client.files_uploadV2 = AsyncMock(return_value={"ok": True})
    client.views_open = AsyncMock(return_value={"ok": True, "view": {"id": "V1"}})

    app = MagicMock()
    app.client = client

    t = SlackTransport(app)
    t._bot_user_id = "B_TEST"
    return t
```

Update the fixture to match the actually-shipped transports. The original
plan listed Discord, but Discord (#2) was designed and never implemented;
Google Chat (#4) shipped on `dev` first and now owns the slot Discord was
holding. The fixture should reflect what's on disk, not the historical
spec ordering:

```python
# Discord (#2) is designed but not shipped; add it back to this fixture
# list when its transport lands. Google Chat (#4) shipped first and
# therefore takes Discord's slot in the parametrize tuple.
@pytest.fixture(params=["fake", "telegram", "web", "google_chat", "slack"])
async def transport(request, tmp_path):
    if request.param == "fake":
        yield FakeTransport()
    elif request.param == "telegram":
        yield _make_telegram_transport_with_inject()
    elif request.param == "web":
        from link_project_to_chat.transport import Identity
        from link_project_to_chat.web.transport import WebTransport
        db_path = tmp_path / "contract.db"
        bot = Identity(transport_id="web", native_id="bot1", display_name="Bot", handle=None, is_bot=True)
        t = WebTransport(db_path=db_path, bot_identity=bot, port=18181)
        await t.start()
        yield t
        await t.stop()
    elif request.param == "google_chat":
        yield _make_google_chat_transport_with_inject()
    elif request.param == "slack":
        yield _make_slack_transport_with_inject()
    else:
        pytest.fail(f"Unknown param: {request.param}")
```

- [ ] **Step 3: Run all contract tests**

```
pytest tests/transport/test_contract.py -v
```
Expected: all contract tests PASS for all five transports (prompt tests skip for telegram where not applicable).

- [ ] **Step 4: Run the full test suite**

```
pytest -v
```
Expected: all tests PASS with no regressions.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/transport/__init__.py tests/transport/test_contract.py
git commit -m "test: add SlackTransport to transport contract test suite"
```

---

### Task 7: Self-review — spec coverage check

Run this as a checklist before declaring the plan complete.

- [ ] **Outbound**: `send_text`, `edit_text`, `send_file`, `send_voice`, `send_typing` — all implemented and tested.
- [ ] **Inbound**: `on_message`, `on_command` (`/lp2c`), `on_button` — all dispatch to handlers.
- [ ] **Prompt**: `open_prompt` (modal for TEXT/SECRET, Block Kit for CHOICE/CONFIRM), `update_prompt`, `close_prompt`, `on_prompt_submit` — all implemented.
- [ ] **Mentions**: `IncomingMessage.mentions` populated from `<@U...>` tokens — tested.
- [ ] **Identity source of truth**: stable Slack IDs (`U...`, `B...`, `C...`) used for `ChatRef.native_id`, `Identity.native_id`, `PromptRef` — verified.
- [ ] **Bot-to-bot**: messages from other bots have `sender.is_bot=True` from `bot_profile` presence; `is_relayed_bot_to_bot=False` — covered in `_dispatch_slack_message`.
- [ ] **Room config**: `RoomBinding.transport_id == "slack"` and peer routing via `BotPeerRef.native_id` — covered by shared config + group_filters from spec #1.
- [ ] **Contract tests**: `SlackTransport` passes `test_contract.py` for text, edit, voice, command, button, mentions, and prompts.
- [ ] **No Telegram leaks**: `SlackTransport` has zero imports from `python-telegram-bot`; verify with `grep -r "telegram" src/link_project_to_chat/transport/slack.py` → should return nothing.
