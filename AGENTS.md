# AGENTS.md

This file provides guidance to Codex (and any other AGENTS-spec-aware agent) when working with code in this repository. Keep in sync with [CLAUDE.md](CLAUDE.md); both are pointers to [docs/TODO.md](docs/TODO.md) for live status.

## Project Overview

A Python CLI that connects a local project directory to a chat platform (Telegram, or a local web UI) for chat-based interactions with an LLM agent. Two backends ship today: Claude (via the `claude` CLI, default) and Codex (via the `codex` CLI, opt-in via `/backend codex`). Features: streaming responses, background shell execution, task management, skills/personas, voice transcription, multi-project management via a manager bot, team/group chat support, **transport-portable plugin framework**, and **`AllowedUser` role model** (viewer/executor) as the sole auth source.

## Commands

```bash
# Install (editable dev mode)
pip install -e "."            # core only
pip install -e ".[all]"       # all optional deps (httpx, telethon, openai)

# Run tests
pytest                        # all tests (async auto-mode)
pytest tests/test_bot_streaming.py  # single file
pytest tests/transport/       # transport tests only

# Run the bot
link-project-to-chat start --project NAME
link-project-to-chat start-manager
```

No Makefile, tox, or linter configured. Build system is hatchling. Entry point: `link_project_to_chat.cli:main`.

## Architecture

### Core flow
`cli.py` → `ProjectBot` (bot.py) → `Transport` (telegram or web) → user messages → `TaskManager` → `AgentBackend` (backends/) → streaming response → `StreamingMessage` (transport/streaming.py)

### Key modules
- **bot.py** — Main ProjectBot handler. Zero direct Telegram imports (enforced by `tests/test_transport_lockout.py`). All platform calls go through Transport. Plugin lifecycle wired via `_init_plugins` (called from `_after_ready`) and `_shutdown_plugins` (registered as a `Transport.on_stop` callback). Role enforcement: `_guard_executor` + `_wrap_with_persist` gate every state-changing command and 16 button-prefix groups.
- **plugin.py** — Plugin framework: `Plugin` base class, `PluginContext` (with live `is_allowed`/`is_executor` helpers + transport-portable `send_message`), `BotCommand` dataclass (defaults to executor-only; `viewer_ok=True` opts in), `load_plugin` via `importlib.metadata.entry_points(group="lptc.plugins")`. Plugins are external Python packages declared per-project in `config.json`.
- **transport/** — `Transport` Protocol (base.py), `TelegramTransport` (telegram.py), `FakeTransport` (fake.py) for tests, `StreamingMessage` (streaming.py) for rate-limited edits. Telegram-specific helpers (`_telegram_relay.py`, `_telegram_group.py`) live alongside but are private. `Transport.on_stop(callback)` lets plugins shut down cleanly before the platform tears down.
- **web/** — `WebTransport` (transport.py), FastAPI app + SSE (app.py), SQLite store (store.py). First non-Telegram transport, shipped under spec #1.
- **backends/** — `AgentBackend` Protocol + `BaseBackend` env-policy helper (base.py), `ClaudeBackend` (claude.py) and Claude JSONL parser (claude_parser.py), `CodexBackend` (codex.py) and Codex JSONL parser (codex_parser.py), `factory.py`. The bot constructs a backend via the factory; backend extraction landed under backend phase 1, and Codex landed under phase 3 as an opt-in via `/backend codex`. Per-backend env scrub/keep allowlists run through `BaseBackend._prepare_env`. Live Codex coverage gates behind `RUN_CODEX_LIVE=1` and the `codex_live` pytest marker.
- **stream.py** — Event types: TextDelta, ThinkingDelta, ToolUse, AskQuestion, Result, Error.
- **task_manager.py** — Tracks concurrent agent tasks and shell commands with lifecycle callbacks.
- **manager/** — `ManagerBot` (ported to `TelegramTransport` under spec #0c, with a 7-name allowlist for `Update`/`ConversationHandler` family enforced by `tests/test_manager_lockout.py`). `ProcessManager` handles project-bot subprocess lifecycle. Wizard state lives in `conversation.py` above the transport layer. Manager owns the **Plugins toggle UI** (per-project) and the **user-management commands** (`/users`, `/add_user`, `/remove_user`, `/promote_user`, `/demote_user`, `/reset_user_identity`). The `/create_project` and `/create_team` wizards open with a GitHub-vs-GitLab provider picker before the browse / paste-URL / clone steps.
- **_auth.py** — `AuthMixin` (v1.0.0 rewrite): identity-based auth around `AllowedUser` as the sole source of truth. `_get_user_role` does identity-lock fast path + username fallback with same-transport spoof guard. `_auth_identity` fails closed on empty `_allowed_users`. `_require_executor` gates state-changing actions. `_failed_auth_counts` and `_rate_limits` are keyed on `_identity_key(identity) = "transport_id:native_id"` so Discord/Slack/Telegram identities never collide.
- **github_client.py / gitlab_client.py** — Repo browsing + cloning for the manager wizard's `/create_project` and `/create_team` flows. `github_client.py` uses `gh` CLI when available, falls back to `httpx + GITHUB_TOKEN`. `gitlab_client.py` mirrors it for GitLab: uses `glab` CLI if available, falls back to `httpx + GITLAB_TOKEN`, and honors `Config.gitlab_host` for self-hosted instances. Both clients satisfy the `RepoProvider` Protocol in `repo_provider.py`.
- **skills.py** — Skill/persona loading with priority: project > global > Claude Code user > bundled.
- **config.py** — Dataclass-based config. Files written with `0o600`. `AllowedUser(username, role, locked_identities: list[str])` replaces the legacy `allowed_usernames` / `trusted_users` / `trusted_user_ids` fields. `_migrate_legacy_auth` reads legacy keys once on load (synthesizes `AllowedUser{role="executor"}` entries); legacy keys are stripped on next save. `resolve_project_allowed_users(project, config)` returns `(users, source)` (project → global fallback). `locked_config_rmw` + `save_config_within_lock` are atomic-RMW helpers used by `_persist_auth_if_dirty`. Includes transport-agnostic `BotPeerRef` and `RoomBinding` types for team routing.
- **formatting.py** — Markdown-to-Telegram HTML conversion, message chunking at ~4096 chars.

### Transport abstraction
The `Transport` Protocol decouples bot logic from Telegram. `bot.py` only communicates through transport primitives (`ChatRef`, `MessageRef`, `Identity`, `IncomingMessage`, `PromptSpec`). New transports must implement the Protocol and pass the parametrized contract test in `tests/transport/test_contract.py` (currently runs against `[fake, telegram, web]`).

### Manager & team support
- `manager/bot.py` — Multi-project orchestration bot, runs through `TelegramTransport` (spec #0c). Residual `telegram` imports are limited to `Update` + `ConversationHandler` family.
- `transport/_telegram_relay.py` — Telegram team relay; lifecycle owned by `TelegramTransport.enable_team_relay`.
- `transport/_telegram_group.py` — Telethon TL helpers used by the manager for supergroup creation/deletion.
- `group_filters.py` / `group_state.py` — Group chat filtering and persistent state. `group_state` is keyed by `ChatRef`; `group_filters` consumes `IncomingMessage.mentions` (structured mentions, not regex parsing).

## Coding Conventions

- Single-purpose functions; split anything over ~100 lines
- Avoid nesting — extract helpers
- Fail early; no defensive error handling for internal code, only at system boundaries
- No duplicate logic — extract shared helpers
- Minimum complexity for the current task; no over-engineering
- All async I/O; tests use `asyncio_mode = "auto"`
- `FakeTransport` for testing bot logic without network calls

## Testing

- pytest with `asyncio_mode = "auto"` — no need to mark individual async tests
- Transport contract test (`tests/transport/test_contract.py`) parametrized across all Transport implementations
- Import lockout test (`tests/test_transport_lockout.py`) ensures bot.py has zero telegram imports
- Test doubles: `FakeTransport`, `FakeBot` (inline in test files)

## Current Development

Active branch `feat/plugin-system` carries the plugin-system port + the `AllowedUser`-sole auth-model rewrite, slated for **v1.0.0**. Previous branch `feat/transport-abstraction` shipped the transport-abstraction track plus the first non-Telegram transport and is folded into `main`.

- Shipped: spec #0 (core, v0.13.0), #0b (voice, v0.14.0), #0a (group/team, v0.15.0), #0c (manager, v0.16.0), #1 (Web UI). Backend abstraction phases 1–4 (Claude extraction, `/backend` command, Codex adapter, capability expansion). TODO hardening commit `b396b1e` closed the active Phase 4/Web security, process lifecycle, context-history, and test portability follow-ups.
- **Shipped on `feat/plugin-system` (v1.0.0):** Plugin framework (`plugin.py`) with transport-portable handler signatures; `Transport.on_stop` Protocol method across Telegram/Fake/Web; TelegramTransport dynamic `on_command` PTB-handler registration fix; manager Plugins toggle UI; `plugin-call` / `migrate-config` CLI subcommands. **Breaking auth model change:** `AllowedUser{username, role, locked_identities}` replaces `allowed_usernames` / `trusted_users` / `trusted_user_ids` as the sole auth source (legacy keys auto-migrate on load, stripped on next save). Identity-keyed auth + role enforcement on 22 state-changing commands and 16 button prefixes. Six new manager user-management commands. Operational scripts `scripts/restart.sh` / `scripts/stop.sh`.
- Designed but not yet implemented: Discord (#2), Slack (#3), Google Chat (#4); Backend Phase 5 (Gemini adapter).

[docs/TODO.md](docs/TODO.md) is the live status source — update there first; this section is a pointer.
