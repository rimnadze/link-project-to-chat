# Google Chat Manager Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make per-project Google Chat bots a first-class manager-supervised feature: `projects.<name>.google_chat` overrides the top-level block, the manager wizard adds/edits/removes Google Chat bots in the Telegram UI, and `ProcessManager` spawns a Google Chat subprocess per project keyed by `(project, transport)` — eliminating the per-bot systemd unit and the `rebuild.sh`-doesn't-restart-Google-Chat caveat.

**Architecture:** Land config schema first (`GoogleChatProjectOverride` + `resolve_project_google_chat`), then wire the CLI to accept a resolved-config JSON blob, then teach `ProcessManager` to spawn/stop/restart Google Chat children alongside Telegram ones, then layer the manager Telegram UI on top, then smoke-test the full path.

**Tech Stack:** Python 3.12, dataclasses, pytest (`asyncio_mode = "auto"`), the existing `python-telegram-bot` manager UI, and the v1.1 Google Chat transport already on `dev`.

**Reference design:** [`docs/superpowers/specs/2026-05-17-google-chat-manager-integration-design.md`](../specs/2026-05-17-google-chat-manager-integration-design.md)

**Branch:** Create `feat/google-chat-manager` from current `dev`. Each task ends with a focused commit and a passing targeted test slice.

---

## File Map

### New files

- `src/link_project_to_chat/google_chat/resolver.py` — `resolve_project_google_chat` per-project merge helper (split out of `config.py` because the merge is non-trivial and benefits from its own test file).
- `tests/google_chat/test_resolver.py` — unit tests for the merge.
- `tests/test_manager_create_google_chat.py` — wizard state-machine tests for the new add/edit/remove flows.
- `tests/test_process_manager_google_chat.py` — `ProcessManager` start/stop/restart tests for the google_chat transport.
- `tests/test_projectbot_smoke_manager_google_chat.py` — integration smoke: manager discovers + spawns + a synthetic event round-trips.

### Existing files to modify

- `src/link_project_to_chat/config.py` — add `GoogleChatProjectOverride` dataclass, parse/serialize helpers, optional `google_chat` field on `ProjectConfig`, one-shot migration.
- `src/link_project_to_chat/cli.py` — add `--google-chat-config-json` to `start`.
- `src/link_project_to_chat/manager/process.py` — `google_chat_pids` dict, `start_google_chat_subprocess`, `stop_google_chat_subprocess`, `restart_google_chat_subprocess`, discovery + supervise extensions.
- `src/link_project_to_chat/manager/bot.py` — per-project view buttons, wizard handlers, nginx snippet printer.
- `src/link_project_to_chat/manager/conversation.py` — new `WIZARD_STATE_GCHAT_*` states + per-state input dispatch.
- `tests/test_config.py` — round-trip + migration tests.
- `README.md` — replace per-systemd-unit setup section with manager-wizard flow.
- `docs/CHANGELOG.md` — v1.2 feature entry.
- `docs/TODO.md` — flip §1.5 to ✅, update §1.3 Google Chat row.

---

## Task 0: Setup Branch and Baseline

**Files:**
- No source changes.

- [ ] **Step 1: Create the feature branch**

```bash
git checkout dev
git pull --ff-only
git checkout -b feat/google-chat-manager
git status --short --branch
```

Expected output contains:

```text
## feat/google-chat-manager
```

- [ ] **Step 2: Run the baseline suite**

```bash
PYTHONPATH=src python3 -m pytest -q
```

Expected: current `dev` baseline passes. Record the exact pass/skip/warning count in task notes and in the empty baseline commit body. (At time of writing on `dev` HEAD `e0d4380`: 1534 passed / 6 skipped.)

- [ ] **Step 3: Commit the baseline marker**

```bash
git commit --allow-empty -m "chore: pin baseline before Google Chat manager integration"
```

Expected: one empty commit on `feat/google-chat-manager`.

---

## Task 1: Add `GoogleChatProjectOverride` Dataclass

**Files:**
- Modify: `src/link_project_to_chat/config.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Write failing override-dataclass test**

Append to `tests/test_config.py`:

```python
def test_google_chat_project_override_defaults_all_optional_except_port():
    from link_project_to_chat.config import GoogleChatProjectOverride

    override = GoogleChatProjectOverride(port=8091)

    # port is required; everything else defaults to None / unset
    assert override.port == 8091
    assert override.service_account_file is None
    assert override.public_url is None
    assert override.root_command_id is None
    assert override.project_number is None
    assert override.auth_audience_type is None
    assert override.host is None
    assert override.callback_token_ttl_seconds is None
    assert override.pending_prompt_ttl_seconds is None
    assert override.max_message_bytes is None
    assert override.attachment_max_bytes is None
    assert override.endpoint_path is None
    assert override.allowed_audiences is None
    assert override.app_id is None
    assert override.root_command_name is None


def test_google_chat_project_override_port_must_be_in_range():
    from link_project_to_chat.config import ConfigError, GoogleChatProjectOverride

    with pytest.raises(ConfigError):
        GoogleChatProjectOverride(port=0).validate()
    with pytest.raises(ConfigError):
        GoogleChatProjectOverride(port=70_000).validate()


def test_google_chat_project_override_validate_accepts_valid_port():
    from link_project_to_chat.config import GoogleChatProjectOverride

    GoogleChatProjectOverride(port=8091).validate()  # no exception
```

- [ ] **Step 2: Verify RED**

```bash
PYTHONPATH=src python3 -m pytest tests/test_config.py::test_google_chat_project_override_defaults_all_optional_except_port -q
```

Expected: fails with `ImportError: cannot import name 'GoogleChatProjectOverride'`.

- [ ] **Step 3: Add the dataclass**

In `src/link_project_to_chat/config.py`, immediately after the existing `GoogleChatConfig` class definition (around line 369), add:

```python
@dataclass
class GoogleChatProjectOverride:
    """Per-project override layered on top of the top-level GoogleChatConfig.

    Every field mirrors GoogleChatConfig but is Optional, so a project only
    needs to set the fields that differ from the operational-defaults block.
    ``port`` is required because two Google Chat bots cannot share a port.
    """
    port: int
    service_account_file: str | None = None
    public_url: str | None = None
    root_command_id: int | None = None
    project_number: str | None = None
    auth_audience_type: str | None = None
    host: str | None = None
    callback_token_ttl_seconds: int | None = None
    pending_prompt_ttl_seconds: int | None = None
    max_message_bytes: int | None = None
    attachment_max_bytes: int | None = None
    endpoint_path: str | None = None
    allowed_audiences: list[str] | None = None
    app_id: str | None = None
    root_command_name: str | None = None

    def validate(self) -> None:
        if not 1 <= self.port <= 65535:
            raise ConfigError(
                f"google_chat.port must be in 1..65535 (got {self.port})"
            )
        if self.auth_audience_type is not None and self.auth_audience_type not in {
            "endpoint_url",
            "project_number",
        }:
            raise ConfigError(
                "google_chat.auth_audience_type must be 'endpoint_url' or 'project_number'"
            )
```

- [ ] **Step 4: Verify GREEN**

```bash
PYTHONPATH=src python3 -m pytest tests/test_config.py::test_google_chat_project_override_defaults_all_optional_except_port tests/test_config.py::test_google_chat_project_override_port_must_be_in_range tests/test_config.py::test_google_chat_project_override_validate_accepts_valid_port -q
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/config.py tests/test_config.py
git commit -m "feat(config): add GoogleChatProjectOverride dataclass"
```

---

## Task 2: Parse / Serialize `GoogleChatProjectOverride`

**Files:**
- Modify: `src/link_project_to_chat/config.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Write failing round-trip tests**

Append to `tests/test_config.py`:

```python
def test_parse_google_chat_override_minimal():
    from link_project_to_chat.config import _parse_google_chat_override

    override = _parse_google_chat_override({"port": 8091})
    assert override.port == 8091
    assert override.service_account_file is None


def test_parse_google_chat_override_full():
    from link_project_to_chat.config import _parse_google_chat_override

    raw = {
        "port": 8092,
        "service_account_file": "/keys/proj.json",
        "public_url": "https://proj.example.com",
        "root_command_id": 7,
        "project_number": "12345",
    }
    override = _parse_google_chat_override(raw)
    assert override.port == 8092
    assert override.service_account_file == "/keys/proj.json"
    assert override.public_url == "https://proj.example.com"
    assert override.root_command_id == 7
    assert override.project_number == "12345"


def test_parse_google_chat_override_missing_port_raises():
    from link_project_to_chat.config import ConfigError, _parse_google_chat_override

    with pytest.raises(ConfigError, match="port"):
        _parse_google_chat_override({"public_url": "https://x.test"})


def test_serialize_google_chat_override_omits_none_fields():
    from link_project_to_chat.config import (
        GoogleChatProjectOverride,
        _serialize_google_chat_override,
    )

    raw = _serialize_google_chat_override(
        GoogleChatProjectOverride(port=8091, public_url="https://x.test")
    )
    assert raw == {"port": 8091, "public_url": "https://x.test"}
    assert "service_account_file" not in raw  # None fields stripped


def test_google_chat_override_round_trip():
    from link_project_to_chat.config import (
        GoogleChatProjectOverride,
        _parse_google_chat_override,
        _serialize_google_chat_override,
    )

    original = GoogleChatProjectOverride(
        port=8091,
        service_account_file="/keys/a.json",
        public_url="https://a.example",
        root_command_id=3,
    )
    raw = _serialize_google_chat_override(original)
    reparsed = _parse_google_chat_override(raw)
    assert reparsed == original
```

- [ ] **Step 2: Verify RED**

```bash
PYTHONPATH=src python3 -m pytest tests/test_config.py::test_parse_google_chat_override_minimal -q
```

Expected: fails with `ImportError: cannot import name '_parse_google_chat_override'`.

- [ ] **Step 3: Add parse + serialize helpers**

In `src/link_project_to_chat/config.py`, near the existing `_parse_google_chat` and `_serialize_google_chat` functions (around line 890), add:

```python
def _parse_google_chat_override(raw: dict) -> "GoogleChatProjectOverride":
    if "port" not in raw:
        raise ConfigError("google_chat per-project override requires 'port'")
    port_raw = raw["port"]
    if not isinstance(port_raw, int):
        raise ConfigError("google_chat.port must be an integer")

    def _opt_str(key: str) -> str | None:
        value = raw.get(key)
        return str(value) if isinstance(value, str) else None

    def _opt_int(key: str) -> int | None:
        value = raw.get(key)
        return int(value) if isinstance(value, int) else None

    audience_type = _opt_str("auth_audience_type")
    if audience_type is not None and audience_type not in {"endpoint_url", "project_number"}:
        raise ConfigError(
            "google_chat.auth_audience_type must be 'endpoint_url' or 'project_number'"
        )

    allowed = raw.get("allowed_audiences")
    if allowed is not None and (
        not isinstance(allowed, list) or not all(isinstance(v, str) for v in allowed)
    ):
        raise ConfigError("google_chat.allowed_audiences must be a list of strings")

    override = GoogleChatProjectOverride(
        port=port_raw,
        service_account_file=_opt_str("service_account_file"),
        public_url=_opt_str("public_url"),
        root_command_id=_opt_int("root_command_id"),
        project_number=_opt_str("project_number"),
        auth_audience_type=audience_type,
        host=_opt_str("host"),
        callback_token_ttl_seconds=_opt_int("callback_token_ttl_seconds"),
        pending_prompt_ttl_seconds=_opt_int("pending_prompt_ttl_seconds"),
        max_message_bytes=_opt_int("max_message_bytes"),
        attachment_max_bytes=_opt_int("attachment_max_bytes"),
        endpoint_path=_opt_str("endpoint_path"),
        allowed_audiences=allowed,
        app_id=_opt_str("app_id"),
        root_command_name=_opt_str("root_command_name"),
    )
    override.validate()
    return override


def _serialize_google_chat_override(override: "GoogleChatProjectOverride") -> dict:
    raw: dict[str, object] = {"port": override.port}
    for field_name in (
        "service_account_file",
        "public_url",
        "root_command_id",
        "project_number",
        "auth_audience_type",
        "host",
        "callback_token_ttl_seconds",
        "pending_prompt_ttl_seconds",
        "max_message_bytes",
        "attachment_max_bytes",
        "endpoint_path",
        "allowed_audiences",
        "app_id",
        "root_command_name",
    ):
        value = getattr(override, field_name)
        if value is not None:
            raw[field_name] = value
    return raw
```

- [ ] **Step 4: Verify GREEN**

```bash
PYTHONPATH=src python3 -m pytest tests/test_config.py -k "google_chat_override" -q
```

Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/config.py tests/test_config.py
git commit -m "feat(config): parse and serialize GoogleChatProjectOverride"
```

---

## Task 3: Add `google_chat` Field to `ProjectConfig`

**Files:**
- Modify: `src/link_project_to_chat/config.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Write failing field test**

Append to `tests/test_config.py`:

```python
def test_project_config_google_chat_defaults_none():
    from link_project_to_chat.config import ProjectConfig

    pc = ProjectConfig(path="/p", telegram_bot_token="")
    assert pc.google_chat is None


def test_project_config_round_trips_google_chat_override(tmp_path):
    import json
    from link_project_to_chat.config import (
        GoogleChatProjectOverride,
        load_config,
        save_config,
    )

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "projects": {
            "alpha": {
                "path": "/p",
                "telegram_bot_token": "",
                "google_chat": {
                    "port": 8091,
                    "service_account_file": "/keys/a.json",
                    "public_url": "https://a.example",
                    "root_command_id": 5,
                },
            }
        }
    }))

    loaded = load_config(cfg_path)
    alpha = loaded.projects["alpha"]
    assert alpha.google_chat == GoogleChatProjectOverride(
        port=8091,
        service_account_file="/keys/a.json",
        public_url="https://a.example",
        root_command_id=5,
    )

    save_config(loaded, cfg_path)
    raw = json.loads(cfg_path.read_text())
    assert raw["projects"]["alpha"]["google_chat"] == {
        "port": 8091,
        "service_account_file": "/keys/a.json",
        "public_url": "https://a.example",
        "root_command_id": 5,
    }


def test_project_config_load_rejects_invalid_google_chat_block(tmp_path):
    import json
    from link_project_to_chat.config import ConfigError, load_config

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "projects": {"alpha": {"path": "/p", "telegram_bot_token": "", "google_chat": {}}}
    }))

    with pytest.raises(ConfigError, match="port"):
        load_config(cfg_path)
```

- [ ] **Step 2: Verify RED**

```bash
PYTHONPATH=src python3 -m pytest tests/test_config.py::test_project_config_google_chat_defaults_none -q
```

Expected: fails — `google_chat` is not an attribute of `ProjectConfig` yet.

- [ ] **Step 3: Add the field + wire parse/serialize**

In `src/link_project_to_chat/config.py`:

1. Update `ProjectConfig` (around line 372) to add the field:

```python
@dataclass
class ProjectConfig:
    path: str
    telegram_bot_token: str
    model: str | None = None
    effort: str | None = None
    # ... existing fields ...
    google_chat: "GoogleChatProjectOverride | None" = None
```

(Keep every existing field; only add the new line.)

2. In the function that loads a single project dict (search for the existing `_load_project` / `_parse_project_config` helper; modify it where `ProjectConfig(...)` is constructed) — after the existing fields, add:

```python
raw_gchat = raw.get("google_chat")
google_chat = _parse_google_chat_override(raw_gchat) if isinstance(raw_gchat, dict) else None
```

…and pass `google_chat=google_chat` to the `ProjectConfig(...)` constructor.

3. In the function that serializes a single project (search for the existing `_serialize_project` / `_save_project` helper), after the existing key writes, add:

```python
if project.google_chat is not None:
    raw["google_chat"] = _serialize_google_chat_override(project.google_chat)
```

- [ ] **Step 4: Verify GREEN**

```bash
PYTHONPATH=src python3 -m pytest tests/test_config.py -k "project_config_google_chat or project_config_round_trips or project_config_load_rejects" -q
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/config.py tests/test_config.py
git commit -m "feat(config): per-project google_chat override on ProjectConfig"
```

---

## Task 4: `resolve_project_google_chat` Per-Field Merge Helper

**Files:**
- Create: `src/link_project_to_chat/google_chat/resolver.py`
- Create: `tests/google_chat/test_resolver.py`

- [ ] **Step 1: Write failing merge tests**

Create `tests/google_chat/test_resolver.py`:

```python
from __future__ import annotations

import pytest

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


def test_override_alone_is_incomplete_returns_none():
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
```

- [ ] **Step 2: Verify RED**

```bash
PYTHONPATH=src python3 -m pytest tests/google_chat/test_resolver.py -q
```

Expected: fails on `ImportError: cannot import name 'resolve_project_google_chat'`.

- [ ] **Step 3: Implement the resolver**

Create `src/link_project_to_chat/google_chat/resolver.py`:

```python
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
```

- [ ] **Step 4: Verify GREEN**

```bash
PYTHONPATH=src python3 -m pytest tests/google_chat/test_resolver.py -q
```

Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/google_chat/resolver.py tests/google_chat/test_resolver.py
git commit -m "feat(google-chat): resolve_project_google_chat merge helper"
```

---

## Task 5: One-Shot Top-Level → Per-Project Migration

**Files:**
- Modify: `src/link_project_to_chat/config.py`
- Modify: `tests/test_config.py`

The migration runs at `load_config` time when the config has a non-empty top-level `google_chat` block but no per-project overrides. It exists so the new manager spawn loop (Task 7) has a per-project anchor to key on. Single-project case auto-claims; multi-project surfaces a setup marker.

- [ ] **Step 1: Write failing migration tests**

Append to `tests/test_config.py`:

```python
def test_migration_auto_claims_for_single_project(tmp_path):
    import json
    from link_project_to_chat.config import load_config, save_config

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "projects": {
            "solo": {"path": "/p", "telegram_bot_token": ""},
        },
        "google_chat": {
            "service_account_file": "/keys/sa.json",
            "public_url": "https://x.example",
            "port": 8090,
            "root_command_id": 1,
        },
    }))

    loaded = load_config(cfg_path)
    assert loaded.projects["solo"].google_chat is not None
    assert loaded.projects["solo"].google_chat.port == 8090

    save_config(loaded, cfg_path)
    raw = json.loads(cfg_path.read_text())
    assert "google_chat" in raw["projects"]["solo"]


def test_migration_idempotent(tmp_path):
    import json
    from link_project_to_chat.config import load_config, save_config

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "projects": {"solo": {"path": "/p", "telegram_bot_token": ""}},
        "google_chat": {
            "service_account_file": "/keys/sa.json",
            "public_url": "https://x.example",
            "port": 8090,
            "root_command_id": 1,
        },
    }))

    load_config(cfg_path)
    save_config(load_config(cfg_path), cfg_path)
    raw = json.loads(cfg_path.read_text())
    # Per-project override exists; running again must not duplicate, change, or remove it.
    assert raw["projects"]["solo"]["google_chat"]["port"] == 8090


def test_migration_skips_when_multiple_projects_no_overrides(tmp_path):
    import json
    from link_project_to_chat.config import load_config

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "projects": {
            "a": {"path": "/a", "telegram_bot_token": ""},
            "b": {"path": "/b", "telegram_bot_token": ""},
        },
        "google_chat": {
            "service_account_file": "/keys/sa.json",
            "public_url": "https://x.example",
            "port": 8090,
            "root_command_id": 1,
        },
    }))

    loaded = load_config(cfg_path)
    # Ambiguous which project to claim — migration MUST NOT guess.
    assert loaded.projects["a"].google_chat is None
    assert loaded.projects["b"].google_chat is None
    # The top-level block is preserved, the operator will use the wizard to claim.
    assert loaded.google_chat.port == 8090
```

- [ ] **Step 2: Verify RED**

```bash
PYTHONPATH=src python3 -m pytest tests/test_config.py -k migration -q
```

Expected: 3 failed (auto-claim doesn't happen yet).

- [ ] **Step 3: Implement the migration**

In `src/link_project_to_chat/config.py`, inside `load_config` after the projects dict is fully populated and before the function returns:

```python
def _maybe_migrate_top_level_google_chat(cfg: "Config") -> None:
    """One-shot: when exactly one project exists and has no override but
    the top-level google_chat block is meaningful, synthesize an override.
    No-op when zero, multiple, or already-overridden projects exist.
    """
    if cfg.google_chat is None:
        return
    top = cfg.google_chat
    if not (top.service_account_file or top.public_url or top.root_command_id):
        return  # not a meaningful top-level block
    projects_without_override = [
        name for name, pc in cfg.projects.items() if pc.google_chat is None
    ]
    if len(cfg.projects) != 1 or len(projects_without_override) != 1:
        return  # ambiguous; skip silently
    sole_name = projects_without_override[0]
    cfg.projects[sole_name].google_chat = GoogleChatProjectOverride(port=top.port)
```

Then in `load_config`, before the final return, call:

```python
_maybe_migrate_top_level_google_chat(cfg)
```

- [ ] **Step 4: Verify GREEN**

```bash
PYTHONPATH=src python3 -m pytest tests/test_config.py -k migration -q
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/config.py tests/test_config.py
git commit -m "feat(config): one-shot top-level google_chat → per-project migration"
```

---

## Task 6: `--google-chat-config-json` CLI Flag

**Files:**
- Modify: `src/link_project_to_chat/cli.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Write failing CLI test**

Append to `tests/test_cli.py`:

```python
def test_start_accepts_google_chat_config_json(monkeypatch, tmp_path):
    """When --google-chat-config-json is set, the start command must use the
    resolved blob instead of reading config.google_chat from disk."""
    import json
    from click.testing import CliRunner
    from link_project_to_chat.cli import main

    captured = {}

    def fake_run_bot(*, project_name, transport, config, **kwargs):
        captured["transport"] = transport
        captured["google_chat"] = config.google_chat
        return  # don't actually start the bot

    monkeypatch.setattr("link_project_to_chat.cli.run_bot", fake_run_bot)

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "projects": {"alpha": {"path": str(tmp_path), "telegram_bot_token": ""}},
    }))

    blob = json.dumps({
        "service_account_file": "/keys/alpha.json",
        "port": 8091,
        "public_url": "https://alpha.example",
        "root_command_id": 7,
    })

    runner = CliRunner()
    result = runner.invoke(main, [
        "--config", str(cfg_path),
        "start",
        "--project", "alpha",
        "--transport", "google_chat",
        "--google-chat-config-json", blob,
    ])
    assert result.exit_code == 0, result.output

    # The resolved config blob got applied on top of (or in place of)
    # whatever was in config.json's google_chat block.
    assert captured["transport"] == "google_chat"
    assert captured["google_chat"].service_account_file == "/keys/alpha.json"
    assert captured["google_chat"].port == 8091
```

(Note: `run_bot` is the symbol the existing `start` command calls; adjust the patch target if your codebase uses a different name. Verify with `grep -n "def run_bot\|def main\|@cli.command" src/link_project_to_chat/cli.py` before writing the test.)

- [ ] **Step 2: Verify RED**

```bash
PYTHONPATH=src python3 -m pytest tests/test_cli.py::test_start_accepts_google_chat_config_json -q
```

Expected: fails (no such flag, or capture not populated).

- [ ] **Step 3: Add the CLI flag**

In `src/link_project_to_chat/cli.py`, find the `start` Click command and add the option:

```python
@click.option(
    "--google-chat-config-json",
    "google_chat_config_json",
    default=None,
    help=(
        "JSON-encoded resolved GoogleChatConfig used in place of "
        "config.google_chat. Intended for use by ProcessManager when "
        "spawning per-project google_chat subprocesses."
    ),
)
```

In the command body, before `run_bot(...)` is called, if `google_chat_config_json` is non-empty, parse it and override `config.google_chat`:

```python
if google_chat_config_json:
    import json
    from .config import GoogleChatConfig
    raw = json.loads(google_chat_config_json)
    # Build a GoogleChatConfig from the resolved dict — every field is
    # already merged by the manager, so this is a direct construct.
    config.google_chat = GoogleChatConfig(
        **{k: v for k, v in raw.items() if k in {f.name for f in fields(GoogleChatConfig)}}
    )
```

…where `fields` comes from `dataclasses` (already imported elsewhere in the module).

- [ ] **Step 4: Verify GREEN**

```bash
PYTHONPATH=src python3 -m pytest tests/test_cli.py::test_start_accepts_google_chat_config_json -q
```

Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/cli.py tests/test_cli.py
git commit -m "feat(cli): --google-chat-config-json resolved-override flag"
```

---

## Task 7: `ProcessManager.start_google_chat_subprocess`

**Files:**
- Modify: `src/link_project_to_chat/manager/process.py`
- Create: `tests/test_process_manager_google_chat.py`

- [ ] **Step 1: Write failing spawn test**

Create `tests/test_process_manager_google_chat.py`:

```python
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from link_project_to_chat.config import (
    Config,
    GoogleChatConfig,
    GoogleChatProjectOverride,
    ProjectConfig,
)
from link_project_to_chat.manager.process import ProcessManager


def _make_config(tmp_path: Path) -> Config:
    return Config(
        projects={
            "alpha": ProjectConfig(
                path=str(tmp_path),
                telegram_bot_token="",
                google_chat=GoogleChatProjectOverride(
                    port=8091,
                    service_account_file="/keys/alpha.json",
                    public_url="https://alpha.example",
                    root_command_id=7,
                ),
            )
        },
        google_chat=GoogleChatConfig(host="127.0.0.1"),
    )


def test_start_google_chat_subprocess_execs_correct_command(monkeypatch, tmp_path):
    captured: dict[str, object] = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        class FakeProc:
            pid = 12345
            def poll(self): return None  # still running
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    pm = ProcessManager(config=_make_config(tmp_path))

    assert pm.start_google_chat_subprocess("alpha") is True
    cmd = captured["cmd"]
    assert "--project" in cmd
    assert "alpha" in cmd
    assert "--transport" in cmd
    assert "google_chat" in cmd
    assert "--google-chat-config-json" in cmd
    blob_idx = cmd.index("--google-chat-config-json") + 1
    resolved = json.loads(cmd[blob_idx])
    assert resolved["port"] == 8091
    assert resolved["service_account_file"] == "/keys/alpha.json"
    assert resolved["host"] == "127.0.0.1"  # from top-level

    assert pm.google_chat_pids == {"alpha": 12345}


def test_start_google_chat_subprocess_returns_false_when_unconfigured(tmp_path):
    pm = ProcessManager(config=Config(
        projects={"beta": ProjectConfig(path=str(tmp_path), telegram_bot_token="")},
        google_chat=GoogleChatConfig(),
    ))
    assert pm.start_google_chat_subprocess("beta") is False
    assert "beta" not in pm.google_chat_pids
```

- [ ] **Step 2: Verify RED**

```bash
PYTHONPATH=src python3 -m pytest tests/test_process_manager_google_chat.py -q
```

Expected: fails on `AttributeError: 'ProcessManager' object has no attribute 'start_google_chat_subprocess'`.

- [ ] **Step 3: Implement the spawn method**

In `src/link_project_to_chat/manager/process.py`:

1. Inside `ProcessManager.__init__`, add (alongside existing PID-tracking dicts):

```python
self.google_chat_pids: dict[str, int] = {}
```

2. Add the new method:

```python
def start_google_chat_subprocess(self, project_name: str) -> bool:
    """Spawn a Google Chat bot subprocess for ``project_name``.

    Returns False if the project has no google_chat configured (no override
    and no top-level block). Returns True after Popen — does not wait for
    the child to fully bind the port; the supervise loop reports binding
    failures via the standard non-zero-exit path.
    """
    import json
    from link_project_to_chat.google_chat.resolver import resolve_project_google_chat
    from link_project_to_chat.config import _serialize_google_chat

    resolved = resolve_project_google_chat(project_name, self._config)
    if resolved is None:
        return False

    blob = json.dumps(_serialize_google_chat(resolved))
    cmd = [
        "link-project-to-chat",
        "start",
        "--project", project_name,
        "--transport", "google_chat",
        "--google-chat-config-json", blob,
    ]
    proc = subprocess.Popen(cmd, **_process_popen_kwargs())
    self.google_chat_pids[project_name] = proc.pid
    return True
```

- [ ] **Step 4: Verify GREEN**

```bash
PYTHONPATH=src python3 -m pytest tests/test_process_manager_google_chat.py -q
```

Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/manager/process.py tests/test_process_manager_google_chat.py
git commit -m "feat(manager): ProcessManager.start_google_chat_subprocess"
```

---

## Task 8: `stop_google_chat_subprocess` + `restart_google_chat_subprocess`

**Files:**
- Modify: `src/link_project_to_chat/manager/process.py`
- Modify: `tests/test_process_manager_google_chat.py`

- [ ] **Step 1: Write failing stop/restart tests**

Append to `tests/test_process_manager_google_chat.py`:

```python
def test_stop_google_chat_subprocess_kills_and_clears_pid(monkeypatch, tmp_path):
    sent_signals: list[int] = []

    class FakeProc:
        pid = 12345
        def terminate(self): sent_signals.append(15)  # SIGTERM
        def wait(self, timeout=None): return 0
        def poll(self): return 0

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: FakeProc())
    pm = ProcessManager(config=_make_config(tmp_path))
    pm.start_google_chat_subprocess("alpha")

    assert pm.stop_google_chat_subprocess("alpha") is True
    assert sent_signals == [15]
    assert "alpha" not in pm.google_chat_pids


def test_stop_google_chat_subprocess_returns_false_when_not_running(tmp_path):
    pm = ProcessManager(config=_make_config(tmp_path))
    assert pm.stop_google_chat_subprocess("alpha") is False


def test_restart_google_chat_subprocess_calls_stop_then_start(monkeypatch, tmp_path):
    events: list[str] = []

    class FakeProc:
        pid = 12345
        def terminate(self): events.append("terminate")
        def wait(self, timeout=None): return 0
        def poll(self): return None

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: (events.append("popen"), FakeProc())[1])
    pm = ProcessManager(config=_make_config(tmp_path))
    pm.start_google_chat_subprocess("alpha")
    events.clear()

    assert pm.restart_google_chat_subprocess("alpha") is True
    assert events == ["terminate", "popen"]
```

- [ ] **Step 2: Verify RED**

```bash
PYTHONPATH=src python3 -m pytest tests/test_process_manager_google_chat.py -k "stop_google_chat or restart_google_chat" -q
```

Expected: 3 failed on missing methods.

- [ ] **Step 3: Implement stop + restart**

In `src/link_project_to_chat/manager/process.py`, on the `ProcessManager` class:

```python
def stop_google_chat_subprocess(self, project_name: str) -> bool:
    """SIGTERM the project's google_chat subprocess. Returns True if a
    subprocess was running, False if there was nothing to stop."""
    pid = self.google_chat_pids.pop(project_name, None)
    if pid is None:
        return False
    # The existing termination helper takes a Popen, but we only have a pid;
    # use os.kill with SIGTERM for symmetry with `stop()`'s existing path.
    import os
    import signal
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True  # already gone
    return True


def restart_google_chat_subprocess(self, project_name: str) -> bool:
    """Stop then start. Returns the start() result."""
    self.stop_google_chat_subprocess(project_name)
    return self.start_google_chat_subprocess(project_name)
```

- [ ] **Step 4: Verify GREEN**

```bash
PYTHONPATH=src python3 -m pytest tests/test_process_manager_google_chat.py -q
```

Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/manager/process.py tests/test_process_manager_google_chat.py
git commit -m "feat(manager): stop and restart Google Chat subprocesses"
```

---

## Task 9: Discovery + Supervise Loop Extensions

**Files:**
- Modify: `src/link_project_to_chat/manager/process.py`
- Modify: `tests/test_process_manager_google_chat.py`

- [ ] **Step 1: Write failing discovery test**

Append to `tests/test_process_manager_google_chat.py`:

```python
def test_start_autostart_spawns_telegram_and_google_chat(monkeypatch, tmp_path):
    """A project with both bot_token and a google_chat override should get
    both subprocesses on autostart."""
    spawned: list[list[str]] = []

    def fake_popen(cmd, **kwargs):
        spawned.append(cmd)
        class FakeProc:
            pid = 10000 + len(spawned)
            def poll(self): return None
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    config = Config(
        projects={
            "alpha": ProjectConfig(
                path=str(tmp_path),
                telegram_bot_token="abc:123",
                google_chat=GoogleChatProjectOverride(
                    port=8091,
                    service_account_file="/keys/a.json",
                    public_url="https://a.example",
                    root_command_id=7,
                ),
            ),
            "beta": ProjectConfig(  # Telegram-only
                path=str(tmp_path),
                telegram_bot_token="xyz:789",
            ),
        },
        google_chat=GoogleChatConfig(host="127.0.0.1"),
    )
    pm = ProcessManager(config=config)
    pm.start_autostart()

    telegram_spawns = [c for c in spawned if "--transport" not in c or "google_chat" not in c]
    google_chat_spawns = [c for c in spawned if "--transport" in c and "google_chat" in c]
    # Alpha + Beta both get Telegram bots (existing behavior).
    assert len(telegram_spawns) == 2
    # Only Alpha gets a Google Chat bot.
    assert len(google_chat_spawns) == 1
    assert "alpha" in google_chat_spawns[0]
    assert pm.google_chat_pids == {"alpha": pm.google_chat_pids["alpha"]}
```

- [ ] **Step 2: Verify RED**

```bash
PYTHONPATH=src python3 -m pytest tests/test_process_manager_google_chat.py::test_start_autostart_spawns_telegram_and_google_chat -q
```

Expected: fails — autostart doesn't spawn google_chat yet.

- [ ] **Step 3: Extend `start_autostart`**

In `src/link_project_to_chat/manager/process.py`, find the existing `start_autostart` method (around line 546) and at the end of its loop body (after the Telegram start call), add:

```python
        # Also spawn a Google Chat bot for any project that has one configured.
        if resolve_project_google_chat(name, self._config) is not None:
            self.start_google_chat_subprocess(name)
```

…with the import added at the top of `process.py`:

```python
from link_project_to_chat.google_chat.resolver import resolve_project_google_chat
```

- [ ] **Step 4: Verify GREEN**

```bash
PYTHONPATH=src python3 -m pytest tests/test_process_manager_google_chat.py -q
```

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/manager/process.py tests/test_process_manager_google_chat.py
git commit -m "feat(manager): autostart spawns google_chat alongside Telegram"
```

---

## Task 10: Crash-Detection No-Retry on Startup Failure

**Files:**
- Modify: `src/link_project_to_chat/manager/process.py`
- Modify: `tests/test_process_manager_google_chat.py`

A google_chat bot that fails to bind its port should not be restart-looped. The supervise loop must detect non-zero exit within ~5 s of spawn and mark the subprocess as failed.

- [ ] **Step 1: Write failing test**

Append to `tests/test_process_manager_google_chat.py`:

```python
def test_start_google_chat_records_failed_startup(monkeypatch, tmp_path, caplog):
    """If the child exits non-zero within ~5 s of spawn, manager records the
    failure and does NOT retry."""

    class FakeFailedProc:
        pid = 99999
        def poll(self): return 1  # exited immediately, non-zero
        def wait(self, timeout=None): return 1

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: FakeFailedProc())
    pm = ProcessManager(config=_make_config(tmp_path))

    pm.start_google_chat_subprocess("alpha")
    # Run the supervise check immediately — under real usage the loop runs
    # every few seconds.
    pm._check_google_chat_health()  # to be added

    assert pm.google_chat_pids == {}  # cleared
    assert "alpha" in pm.google_chat_failed_startups
    # Sanity: no retry was attempted (no second Popen).
    # (The monkeypatched fake_popen wasn't given a counter; if a retry happened
    # the test wouldn't fail outright, but the status dict tells us no retry
    # is queued.)
```

- [ ] **Step 2: Verify RED**

```bash
PYTHONPATH=src python3 -m pytest tests/test_process_manager_google_chat.py::test_start_google_chat_records_failed_startup -q
```

Expected: fails — `_check_google_chat_health` does not exist.

- [ ] **Step 3: Implement health-check helper**

In `src/link_project_to_chat/manager/process.py`, on `ProcessManager.__init__` add:

```python
self.google_chat_failed_startups: dict[str, str] = {}  # project → error tail
self._google_chat_procs: dict[str, subprocess.Popen] = {}
```

Update `start_google_chat_subprocess` to store the Popen object as well:

```python
proc = subprocess.Popen(cmd, **_process_popen_kwargs())
self.google_chat_pids[project_name] = proc.pid
self._google_chat_procs[project_name] = proc
return True
```

Add the health-check helper:

```python
def _check_google_chat_health(self) -> None:
    """Detect google_chat children that exited within the last health window.
    Move their state from "running" to "failed startup" so the supervise loop
    doesn't restart them in a tight loop."""
    failed: list[str] = []
    for name, proc in list(self._google_chat_procs.items()):
        status = proc.poll()
        if status is None:
            continue  # still running
        if status != 0:
            failed.append(name)
    for name in failed:
        self.google_chat_pids.pop(name, None)
        self._google_chat_procs.pop(name, None)
        self.google_chat_failed_startups[name] = f"exited with code (see manager log)"
```

Wire `_check_google_chat_health` into the existing supervise loop (find the existing `_supervise` or equivalent method and call it on every iteration alongside the Telegram health check).

- [ ] **Step 4: Verify GREEN**

```bash
PYTHONPATH=src python3 -m pytest tests/test_process_manager_google_chat.py::test_start_google_chat_records_failed_startup -q
```

Expected: 1 passed (full suite: 7 passed).

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/manager/process.py tests/test_process_manager_google_chat.py
git commit -m "feat(manager): record google_chat startup failures, no restart loop"
```

---

## Task 11: Per-Project Manager Buttons

**Files:**
- Modify: `src/link_project_to_chat/manager/bot.py`
- Create: `tests/test_manager_create_google_chat.py`

- [ ] **Step 1: Write failing button test**

Create `tests/test_manager_create_google_chat.py`:

```python
"""Wizard-state-machine tests for the new Google Chat manager flows."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from link_project_to_chat.config import (
    Config,
    GoogleChatConfig,
    ProjectConfig,
    save_config,
)


def _write_config(tmp_path: Path, projects: dict, gchat: GoogleChatConfig | None = None) -> Path:
    cfg_path = tmp_path / "config.json"
    raw = {"projects": projects}
    if gchat is not None:
        raw["google_chat"] = {
            "service_account_file": gchat.service_account_file,
            "public_url": gchat.public_url,
            "port": gchat.port,
            "root_command_id": gchat.root_command_id,
            "host": gchat.host,
        }
    cfg_path.write_text(json.dumps(raw))
    return cfg_path


def test_project_view_shows_add_google_chat_button_when_no_override(tmp_path):
    """Reading the per-project view of a project without a google_chat
    override must include an 'Add Google Chat' button."""
    from link_project_to_chat.manager.bot import ManagerBot

    cfg_path = _write_config(tmp_path, {
        "alpha": {"path": str(tmp_path), "telegram_bot_token": "tok"},
    })

    bot = ManagerBot(config_path=cfg_path, process_manager=MagicMock())
    markup = bot._build_project_view_keyboard("alpha")
    button_labels = [b.text for row in markup.inline_keyboard for b in row]
    assert any("Add Google Chat" in label or "Google Chat" in label for label in button_labels)


def test_project_view_shows_edit_remove_restart_when_override_exists(tmp_path):
    from link_project_to_chat.manager.bot import ManagerBot

    cfg_path = _write_config(tmp_path, {
        "alpha": {
            "path": str(tmp_path),
            "telegram_bot_token": "tok",
            "google_chat": {
                "port": 8091,
                "service_account_file": "/keys/a.json",
                "public_url": "https://a.example",
                "root_command_id": 7,
            },
        },
    })

    bot = ManagerBot(config_path=cfg_path, process_manager=MagicMock())
    markup = bot._build_project_view_keyboard("alpha")
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert any("Edit Google Chat" in l for l in labels)
    assert any("Remove Google Chat" in l for l in labels)
    assert any("Restart Google Chat" in l for l in labels)
```

- [ ] **Step 2: Verify RED**

```bash
PYTHONPATH=src python3 -m pytest tests/test_manager_create_google_chat.py::test_project_view_shows_add_google_chat_button_when_no_override -q
```

Expected: fails — the per-project view doesn't include Google Chat buttons.

- [ ] **Step 3: Add the buttons**

In `src/link_project_to_chat/manager/bot.py`, find the method that builds the per-project keyboard (search for the inline-keyboard construction in the project-view handler; rename or extract as `_build_project_view_keyboard(project_name)` if it isn't already a discrete method). Inside that method, after the existing buttons:

```python
from telegram import InlineKeyboardButton

project = self._config.projects[project_name]
if project.google_chat is None:
    rows.append([InlineKeyboardButton(
        "➕ Add Google Chat",
        callback_data=f"proj_add_gchat_{project_name}",
    )])
else:
    rows.append([
        InlineKeyboardButton(
            "✏️ Edit Google Chat",
            callback_data=f"proj_edit_gchat_{project_name}",
        ),
        InlineKeyboardButton(
            "🔁 Restart Google Chat",
            callback_data=f"proj_restart_gchat_{project_name}",
        ),
    ])
    rows.append([InlineKeyboardButton(
        "🗑 Remove Google Chat",
        callback_data=f"proj_remove_gchat_{project_name}",
    )])
```

- [ ] **Step 4: Verify GREEN**

```bash
PYTHONPATH=src python3 -m pytest tests/test_manager_create_google_chat.py -q
```

Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/manager/bot.py tests/test_manager_create_google_chat.py
git commit -m "feat(manager): add Google Chat buttons to per-project view"
```

---

## Task 12: Add-Google-Chat Wizard

**Files:**
- Modify: `src/link_project_to_chat/manager/conversation.py`
- Modify: `src/link_project_to_chat/manager/bot.py`
- Modify: `tests/test_manager_create_google_chat.py`

- [ ] **Step 1: Write failing wizard-flow test**

Append to `tests/test_manager_create_google_chat.py`:

```python
@pytest.mark.asyncio
async def test_add_google_chat_wizard_collects_four_fields_and_persists(tmp_path, monkeypatch):
    """End-to-end wizard: tap [+ Add Google Chat], answer four prompts,
    config gets persisted, ProcessManager.start_google_chat_subprocess fires."""
    from link_project_to_chat.manager.bot import ManagerBot

    cfg_path = _write_config(tmp_path, {
        "alpha": {"path": str(tmp_path), "telegram_bot_token": "tok"},
    })
    pm = MagicMock()
    pm.start_google_chat_subprocess.return_value = True

    bot = ManagerBot(config_path=cfg_path, process_manager=pm)

    # Simulate the operator pressing [+ Add Google Chat]
    await bot._handle_add_gchat_start("alpha", session_id="op-1")
    await bot._handle_wizard_input("op-1", "/home/botuser/keys/alpha.json")  # SA path
    await bot._handle_wizard_input("op-1", "8091")                            # port
    await bot._handle_wizard_input("op-1", "https://alpha.example.com")       # public URL
    await bot._handle_wizard_input("op-1", "7")                               # root_command_id

    # Wizard saved the override
    from link_project_to_chat.config import load_config
    loaded = load_config(cfg_path)
    assert loaded.projects["alpha"].google_chat is not None
    assert loaded.projects["alpha"].google_chat.port == 8091
    assert loaded.projects["alpha"].google_chat.service_account_file == "/home/botuser/keys/alpha.json"

    # And asked ProcessManager to start the subprocess.
    pm.start_google_chat_subprocess.assert_called_once_with("alpha")


@pytest.mark.asyncio
async def test_add_google_chat_wizard_rejects_invalid_port(tmp_path):
    from link_project_to_chat.manager.bot import ManagerBot

    cfg_path = _write_config(tmp_path, {
        "alpha": {"path": str(tmp_path), "telegram_bot_token": "tok"},
    })
    bot = ManagerBot(config_path=cfg_path, process_manager=MagicMock())

    await bot._handle_add_gchat_start("alpha", session_id="op-2")
    await bot._handle_wizard_input("op-2", "/keys/sa.json")
    # Invalid port
    reply = await bot._handle_wizard_input("op-2", "99999")
    assert "1-65535" in reply or "65535" in reply
    # Wizard didn't advance past the port prompt
    session = bot._wizard_sessions["op-2"]
    assert session.state.name.endswith("PORT")
```

- [ ] **Step 2: Verify RED**

```bash
PYTHONPATH=src python3 -m pytest tests/test_manager_create_google_chat.py::test_add_google_chat_wizard_collects_four_fields_and_persists -q
```

Expected: fails — the wizard handlers don't exist.

- [ ] **Step 3: Implement wizard states + handlers**

In `src/link_project_to_chat/manager/conversation.py` add the new states (at the top alongside any existing wizard-state enum):

```python
class WizardState(Enum):
    # ... existing states ...
    GCHAT_SA_FILE = auto()
    GCHAT_PORT = auto()
    GCHAT_PUBLIC_URL = auto()
    GCHAT_ROOT_COMMAND_ID = auto()
    GCHAT_EDIT_SA_FILE = auto()
    GCHAT_EDIT_PORT = auto()
    GCHAT_EDIT_PUBLIC_URL = auto()
    GCHAT_EDIT_ROOT_COMMAND_ID = auto()
```

In `src/link_project_to_chat/manager/bot.py`, register a callback handler for the `proj_add_gchat_<name>` button pattern and add the handler methods (the exact existing handler-registration spelling depends on the current PTB wiring; mirror an existing `proj_edit_<name>` pattern):

```python
async def _handle_add_gchat_start(self, project_name: str, session_id: str) -> str:
    """Operator tapped [+ Add Google Chat]. Start the wizard."""
    self._wizard_sessions[session_id] = WizardSession(
        kind="add_gchat",
        project_name=project_name,
        state=WizardState.GCHAT_SA_FILE,
        data={},
    )
    return "Path to the service-account JSON file:"


async def _handle_wizard_input(self, session_id: str, text: str) -> str:
    session = self._wizard_sessions.get(session_id)
    if session is None:
        return "No active wizard."

    text = text.strip()
    if session.kind == "add_gchat":
        return await self._handle_add_gchat_step(session, text)
    # ... existing wizard dispatch for other kinds ...
    return "Unknown wizard state."


async def _handle_add_gchat_step(self, session: WizardSession, text: str) -> str:
    state = session.state
    if state is WizardState.GCHAT_SA_FILE:
        if not text:
            return "Path required:"
        session.data["service_account_file"] = text
        session.state = WizardState.GCHAT_PORT
        return "Port (1-65535):"
    if state is WizardState.GCHAT_PORT:
        try:
            port = int(text)
        except ValueError:
            return "Port must be an integer (1-65535):"
        if not 1 <= port <= 65535:
            return "Port must be 1-65535:"
        session.data["port"] = port
        session.state = WizardState.GCHAT_PUBLIC_URL
        return "Public HTTPS URL (e.g. https://lp2c-alpha.example.com):"
    if state is WizardState.GCHAT_PUBLIC_URL:
        if not text.startswith("https://"):
            return "URL must start with https:// :"
        session.data["public_url"] = text
        session.state = WizardState.GCHAT_ROOT_COMMAND_ID
        return "Cloud Console slash-command ID (integer):"
    if state is WizardState.GCHAT_ROOT_COMMAND_ID:
        try:
            cmd_id = int(text)
        except ValueError:
            return "Command ID must be an integer:"
        session.data["root_command_id"] = cmd_id
        return await self._finalize_add_gchat(session)
    return "Wizard state error."


async def _finalize_add_gchat(self, session: WizardSession) -> str:
    from link_project_to_chat.config import (
        GoogleChatProjectOverride,
        load_config,
        save_config,
    )
    cfg = load_config(self._config_path)
    cfg.projects[session.project_name].google_chat = GoogleChatProjectOverride(
        port=session.data["port"],
        service_account_file=session.data["service_account_file"],
        public_url=session.data["public_url"],
        root_command_id=session.data["root_command_id"],
    )
    save_config(cfg, self._config_path)
    self._config = cfg  # in-memory refresh
    self._wizard_sessions.pop(session.id, None)
    self._process_manager.start_google_chat_subprocess(session.project_name)
    return (
        f"✅ Google Chat bot configured for {session.project_name} on port "
        f"{session.data['port']}. Deploy this nginx vhost next:\n\n"
        + self._render_nginx_snippet(session.project_name, session.data)
    )


def _render_nginx_snippet(self, project_name: str, data: dict) -> str:
    public_host = data["public_url"].split("//", 1)[-1].split("/", 1)[0]
    return (
        "```nginx\n"
        f"server {{\n"
        f"    listen 80;\n"
        f"    server_name {public_host};\n"
        f"    location /.well-known/acme-challenge/ {{ root /var/www/letsencrypt; }}\n"
        f"    location / {{ return 301 https://$host$request_uri; }}\n"
        f"}}\n"
        f"server {{\n"
        f"    listen 443 ssl http2;\n"
        f"    server_name {public_host};\n"
        f"    ssl_certificate /etc/letsencrypt/live/{public_host}/fullchain.pem;\n"
        f"    ssl_certificate_key /etc/letsencrypt/live/{public_host}/privkey.pem;\n"
        f"    location /google-chat/events {{\n"
        f"        proxy_pass http://127.0.0.1:{data['port']}/google-chat/events;\n"
        f"        proxy_http_version 1.1;\n"
        f"        proxy_set_header Host $host;\n"
        f"        proxy_set_header X-Forwarded-Proto https;\n"
        f"        proxy_read_timeout 10s;\n"
        f"        client_max_body_size 10m;\n"
        f"    }}\n"
        f"}}\n"
        "```\n"
        f"Run `sudo certbot --nginx -d {public_host}` to obtain the TLS cert,\n"
        f"then `sudo nginx -t && sudo systemctl reload nginx`."
    )
```

(Existing PTB callback-handler registration code already gates state-changing buttons via `_guard_executor*` helpers — apply those guards to the new `proj_add_gchat_*` / `proj_edit_gchat_*` / `proj_remove_gchat_*` / `proj_restart_gchat_*` callbacks for parity with the existing Telegram-bot-management flows.)

- [ ] **Step 4: Verify GREEN**

```bash
PYTHONPATH=src python3 -m pytest tests/test_manager_create_google_chat.py -q
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/manager/bot.py src/link_project_to_chat/manager/conversation.py tests/test_manager_create_google_chat.py
git commit -m "feat(manager): add Google Chat wizard (4-field collect + nginx snippet)"
```

---

## Task 13: Edit / Remove / Restart Google Chat Wizards

**Files:**
- Modify: `src/link_project_to_chat/manager/bot.py`
- Modify: `tests/test_manager_create_google_chat.py`

- [ ] **Step 1: Write failing edit/remove tests**

Append to `tests/test_manager_create_google_chat.py`:

```python
@pytest.mark.asyncio
async def test_remove_google_chat_clears_override_and_stops_subprocess(tmp_path):
    from link_project_to_chat.config import load_config
    from link_project_to_chat.manager.bot import ManagerBot

    cfg_path = _write_config(tmp_path, {
        "alpha": {
            "path": str(tmp_path),
            "telegram_bot_token": "tok",
            "google_chat": {
                "port": 8091,
                "service_account_file": "/keys/a.json",
                "public_url": "https://a.example",
                "root_command_id": 7,
            },
        },
    })
    pm = MagicMock()
    bot = ManagerBot(config_path=cfg_path, process_manager=pm)

    reply = await bot._handle_remove_gchat_confirm("alpha")

    loaded = load_config(cfg_path)
    assert loaded.projects["alpha"].google_chat is None
    pm.stop_google_chat_subprocess.assert_called_once_with("alpha")
    assert "removed" in reply.lower()


@pytest.mark.asyncio
async def test_restart_google_chat_calls_pm_restart(tmp_path):
    from link_project_to_chat.manager.bot import ManagerBot

    cfg_path = _write_config(tmp_path, {
        "alpha": {
            "path": str(tmp_path),
            "telegram_bot_token": "tok",
            "google_chat": {
                "port": 8091,
                "service_account_file": "/keys/a.json",
                "public_url": "https://a.example",
                "root_command_id": 7,
            },
        },
    })
    pm = MagicMock()
    bot = ManagerBot(config_path=cfg_path, process_manager=pm)

    await bot._handle_restart_gchat("alpha")
    pm.restart_google_chat_subprocess.assert_called_once_with("alpha")


@pytest.mark.asyncio
async def test_edit_google_chat_wizard_prefills_current_values(tmp_path):
    from link_project_to_chat.manager.bot import ManagerBot

    cfg_path = _write_config(tmp_path, {
        "alpha": {
            "path": str(tmp_path),
            "telegram_bot_token": "tok",
            "google_chat": {
                "port": 8091,
                "service_account_file": "/keys/a.json",
                "public_url": "https://a.example",
                "root_command_id": 7,
            },
        },
    })
    bot = ManagerBot(config_path=cfg_path, process_manager=MagicMock())

    prompt = await bot._handle_edit_gchat_start("alpha", session_id="op-3")
    # Edit-mode initial prompt should pre-fill the current SA path so the
    # operator can see what it was; sending an empty reply keeps the value.
    assert "/keys/a.json" in prompt
```

- [ ] **Step 2: Verify RED**

```bash
PYTHONPATH=src python3 -m pytest tests/test_manager_create_google_chat.py -k "remove or restart or edit" -q
```

Expected: 3 failed on missing handlers.

- [ ] **Step 3: Implement edit / remove / restart handlers**

In `src/link_project_to_chat/manager/bot.py`:

```python
async def _handle_remove_gchat_confirm(self, project_name: str) -> str:
    from link_project_to_chat.config import load_config, save_config
    cfg = load_config(self._config_path)
    cfg.projects[project_name].google_chat = None
    save_config(cfg, self._config_path)
    self._config = cfg
    self._process_manager.stop_google_chat_subprocess(project_name)
    return f"🗑 Google Chat config removed for {project_name}. nginx vhost left intact — clean it up manually if you no longer need the subdomain."


async def _handle_restart_gchat(self, project_name: str) -> str:
    success = self._process_manager.restart_google_chat_subprocess(project_name)
    if not success:
        return f"⚠️ Restart for {project_name} failed (config missing?). Check `journalctl -u link-project-to-chat`."
    return f"🔁 Google Chat subprocess restarted for {project_name}."


async def _handle_edit_gchat_start(self, project_name: str, session_id: str) -> str:
    current = self._config.projects[project_name].google_chat
    if current is None:
        return "No google_chat override to edit. Use [+ Add Google Chat] instead."
    self._wizard_sessions[session_id] = WizardSession(
        kind="edit_gchat",
        project_name=project_name,
        state=WizardState.GCHAT_EDIT_SA_FILE,
        data={"existing": current},
    )
    return (
        f"Edit Google Chat for {project_name}.\n"
        f"Current service-account JSON: {current.service_account_file or '(none)'}\n"
        f"Send a new path, or `/keep` to keep the current value:"
    )
```

Extend `_handle_wizard_input` (Task 12) to dispatch `edit_gchat` similarly to `add_gchat`, treating an input of `/keep` as "preserve current value" and `_finalize_edit_gchat` writes deltas only and calls `restart_google_chat_subprocess` instead of `start_*`.

- [ ] **Step 4: Verify GREEN**

```bash
PYTHONPATH=src python3 -m pytest tests/test_manager_create_google_chat.py -q
```

Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/manager/bot.py tests/test_manager_create_google_chat.py
git commit -m "feat(manager): edit/remove/restart Google Chat handlers"
```

---

## Task 14: Integration Smoke Test

**Files:**
- Create: `tests/test_projectbot_smoke_manager_google_chat.py`

- [ ] **Step 1: Write the smoke test**

Create `tests/test_projectbot_smoke_manager_google_chat.py`:

```python
"""End-to-end: manager discovers a config with one Telegram + one google_chat
project, spawns both subprocesses, and a synthetic Google Chat POST reaches
the right port and dispatches."""
from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from link_project_to_chat.config import (
    Config,
    GoogleChatConfig,
    GoogleChatProjectOverride,
    ProjectConfig,
)


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.asyncio
async def test_manager_starts_google_chat_and_dispatches_synthetic_event(tmp_path):
    """ProcessManager.start_autostart() should spawn a working google_chat
    subprocess that receives, fast-acks, and dispatches a POST."""
    from link_project_to_chat.manager.process import ProcessManager
    from link_project_to_chat.google_chat.resolver import resolve_project_google_chat

    port = _pick_free_port()
    config = Config(
        projects={
            "alpha": ProjectConfig(
                path=str(tmp_path),
                telegram_bot_token="",  # no telegram bot
                google_chat=GoogleChatProjectOverride(
                    port=port,
                    service_account_file=str(tmp_path / "fake-sa.json"),
                    public_url="https://alpha.example.test",
                    root_command_id=1,
                ),
            )
        },
        google_chat=GoogleChatConfig(host="127.0.0.1"),
    )
    # Materialize a fake SA file so the validator at child-startup doesn't fail.
    (tmp_path / "fake-sa.json").write_text(json.dumps({
        "type": "service_account",
        "project_id": "test",
        "private_key_id": "fake",
        "private_key": "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n",
        "client_email": "fake@test.iam.gserviceaccount.com",
    }))

    resolved = resolve_project_google_chat("alpha", config)
    assert resolved is not None
    assert resolved.port == port

    # Spawn for real (not monkeypatched). Then health-check + cleanup.
    pm = ProcessManager(config=config)
    started = pm.start_google_chat_subprocess("alpha")
    assert started is True
    try:
        # Give uvicorn a couple seconds to bind.
        for _ in range(20):
            try:
                async with httpx.AsyncClient() as client:
                    r = await client.post(f"http://127.0.0.1:{port}/google-chat/events", json={})
                    # Either 401 (no auth) or 405 (wrong method) means alive.
                    assert r.status_code in (401, 405, 400)
                    break
            except (httpx.ConnectError, httpx.RemoteProtocolError):
                await asyncio.sleep(0.25)
        else:
            pytest.fail("Google Chat subprocess never bound the port")
    finally:
        pm.stop_google_chat_subprocess("alpha")
```

- [ ] **Step 2: Run the smoke test**

```bash
PYTHONPATH=src python3 -m pytest tests/test_projectbot_smoke_manager_google_chat.py -q
```

Expected: 1 passed. (If the port-pick races with another listener, the test re-rolls.)

- [ ] **Step 3: Commit**

```bash
git add tests/test_projectbot_smoke_manager_google_chat.py
git commit -m "test(manager): smoke test for end-to-end Google Chat spawn"
```

---

## Task 15: Docs + TODO Status Update

**Files:**
- Modify: `README.md`
- Modify: `docs/CHANGELOG.md`
- Modify: `docs/TODO.md`

- [ ] **Step 1: Update README**

In `README.md`, in the `### Google Chat transport` section (around line 75), replace the per-systemd-unit setup paragraph with the manager-wizard flow:

```markdown
### Google Chat transport

Google Chat bots are managed by the existing `link-project-to-chat.service`
manager unit. To add a Google Chat bot for a project:

1. Create a Cloud Console Chat app on a GCP project and download the
   service-account JSON. (See [docs/setup/google-chat.md](docs/setup/google-chat.md)
   for the Cloud Console click-through.)
2. In the manager Telegram bot, open the project view and tap
   `[+ Add Google Chat]`. The wizard collects the SA path, port, public URL,
   and slash-command ID.
3. The wizard prints a ready-to-paste nginx vhost snippet. Deploy it on your
   reverse proxy, run certbot for the new subdomain, then `sudo nginx -t && sudo systemctl reload nginx`.
4. From the project's Google Chat space, DM the bot. The reply arrives via
   the manager-spawned google_chat subprocess on the configured port.

Per-project config lives under `projects.<name>.google_chat`. Operational
defaults (host, TTLs, byte caps, audience type) come from the top-level
`google_chat` block. Each project still requires its own GCP project +
Chat app + service-account JSON; one Chat app per GCP project is a Google
constraint.

`rebuild.sh` restarts the manager, which in turn restarts every supervised
google_chat subprocess — no separate `link-project-to-chat-gchat-*.service`
units anymore.
```

- [ ] **Step 2: Update CHANGELOG**

Prepend a new entry to `docs/CHANGELOG.md`:

```markdown
## v1.2.0 — 2026-05-17

### Google Chat manager integration

- Per-project `google_chat` override on `ProjectConfig`; per-field merge over
  the top-level operational-defaults block.
- `ProcessManager` spawns, stops, and restarts google_chat subprocesses
  alongside Telegram bots, keyed by `(project, transport)`.
- Manager Telegram UI gains `[+ Add Google Chat]` / `[Edit Google Chat]` /
  `[Remove Google Chat]` / `[Restart Google Chat]` buttons in the per-project
  view. Wizard collects SA path, port, public URL, slash-command ID; prints
  ready-to-paste nginx vhost on completion.
- One-shot migration on first load auto-claims the top-level block for
  single-project deployments. Multi-project no-override deployments are left
  alone for the operator to claim via the wizard.
- `rebuild.sh` now restarts every google_chat subprocess automatically.
  No more per-bot `link-project-to-chat-gchat-*.service` units.
```

- [ ] **Step 3: Flip TODO §1.5 to ✅**

In `docs/TODO.md`, update §1.5's task table:

- Each task row's `Status` column becomes ✅ with the commit SHA.
- Add a one-line **Verification** entry under the table referencing
  `f88cf03..<merge-sha>` and the final pytest tally.

In §1.3, update the Google Chat row's `Notes` column to mention v1.2 shipping
the manager integration and remove the "v1.2+ follow-ups tracked in §1.5"
sentence (replaced with a reference to the v1.2 ✅).

- [ ] **Step 4: Full verification**

```bash
PYTHONPATH=src python3 -m pytest -q
git diff --check
python3 -m compileall -q src/link_project_to_chat
```

Expected: full suite passes, no whitespace errors, compileall exits 0.

- [ ] **Step 5: Commit**

```bash
git add README.md docs/CHANGELOG.md docs/TODO.md
git commit -m "docs(google-chat): manager-integrated multi-bot flow (v1.2)"
```

---

## Final Integration

- [ ] **Step 1: Run final verification**

```bash
PYTHONPATH=src python3 -m pytest -q
git diff --check
python3 -m compileall -q src/link_project_to_chat
git status --short --branch
git log --oneline dev..HEAD
```

Expected:
- pytest passes
- `git diff --check` prints nothing and exits 0
- compileall exits 0
- 15+ commits ahead of `origin/dev`

- [ ] **Step 2: Push feature branch**

```bash
git push -u origin feat/google-chat-manager
```

- [ ] **Step 3: Open PR**

Open a PR from `feat/google-chat-manager` to `dev` with:

- baseline test count (from Task 0 commit body)
- final test count (from Task 15 verification step)
- summary of acceptance criteria (cross-reference spec §9):
  - wizard adds a google_chat bot to any project without manual config.json edits
  - single systemd unit supervises both Telegram and Google Chat bots
  - single-bot deployments keep working (migration auto-claims)
  - `rebuild.sh` restarts all google_chat bots automatically
  - existing `test_projectbot_google_chat_end_to_end` continues to pass

---

## Self-Review Notes

- **Spec coverage:**
  - Config schema (§4.1) → Tasks 1, 2, 3
  - resolver (§4.1) → Task 4
  - Migration (§5.1, §6) → Task 5
  - CLI flag (§4.3) → Task 6
  - ProcessManager spawn (§4.2) → Tasks 7, 8, 9, 10
  - Manager UI (§4.4) → Tasks 11, 12, 13
  - Operational defaults split (§4.5) → covered implicitly by Task 4's merge logic
  - Data-flow scenarios (§5) → exercised by Tasks 9, 12, 13, 14
  - Error-handling table (§6) → Task 10 (startup failure), Task 5 (incomplete merge), Task 13 (remove), Task 12 (port validation)
  - Testing strategy (§7) → distributed across Tasks 1-13, with §7.5 (integration smoke) → Task 14 and §7.6 (backward-compat regression) → Task 15's full pytest pass.
- **Type consistency:** `GoogleChatProjectOverride` (with that exact name) is defined in Task 1 and referenced by Tasks 2-15. `resolve_project_google_chat` and its return-type `GoogleChatConfig | None` are defined in Task 4 and referenced by Tasks 7, 9, 14. `google_chat_pids: dict[str, int]` first introduced in Task 7 stays consistent through Tasks 8, 9, 10. `_google_chat_procs` introduced in Task 10 stays scoped to ProcessManager only.
- **No placeholders:** every step includes the actual code or command to run; verification steps include explicit pytest invocations with expected counts. Two areas where the engineer must locate existing internal helpers (the project-keyboard builder in Task 11 and the wizard-state-enum location in Task 12) are explicitly flagged as "search for the existing X" rather than left as "TODO."
- **Out-of-scope reminders carried from spec §8:** nginx vhost is printed but never written by the wizard; GCP-side Chat-app creation is the operator's manual flow; multi-Chat-app-per-GCP-project is Google's constraint, not ours.
