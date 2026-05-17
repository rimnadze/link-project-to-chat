# Google Chat manager integration + per-project config (v1.2)

**Date:** 2026-05-17
**Status:** Design — approved by user, pending implementation plan
**Scope:** Single feature spec, one implementation cycle. Tracked as `docs/TODO.md` §1.5.

## Motivation

After v1.1 shipped, the production deployment hit a real limit: the bot supports exactly one Google Chat identity. Today the `google_chat` config is a single top-level block in `config.json`; every `link-project-to-chat start --project NAME --transport google_chat` invocation reads it. The user runs six lptc projects on Telegram and asked how to add a Google Chat bot per project. Concretely they hit three friction points:

1. **No per-project Google Chat config.** Two projects can't have different `service_account_file` / `port` / `public_url` / `root_command_id`.
2. **No manager integration.** Each Google Chat bot needs its own hand-written systemd unit, separate from the manager-supervised Telegram bots, with the loud caveat that `rebuild.sh` doesn't restart them.
3. **No add-Google-Chat UX.** The operator edits `config.json` by hand, designs the nginx vhost themselves, and stitches the systemd unit. Telegram has a manager wizard for this; Google Chat doesn't.

The goal of v1.2 is parity: adding a Google Chat bot to a project should feel like adding a Telegram bot does today.

## Design decisions (locked)

1. **Port allocation:** explicit per-project config. Operator writes `port: 8091` into the override. Manager binds that exact port; refuses to start on collision. Rationale: nginx vhosts are already operator-managed; the port stays stable across restarts.
2. **Precedence:** per-field merge. `projects.<name>.google_chat` overlays the top-level `google_chat` block field-by-field. Top-level remains the source of operational defaults (host, TTLs, byte caps, audience type). Rationale: backwards-compatible with the current single-bot config; minimal copy-paste for added projects.
3. **Manager UI level:** full wizard parity with Telegram. Per-project view gets `[+ Add Google Chat]` / `[Edit Google Chat]` / `[Remove Google Chat]` / `[Restart Google Chat]`. Operator never edits `config.json` by hand for this flow. Rationale: this is the one UX choice that justifies the manager-integration work in the first place.
4. **Lifecycle:** fully independent subprocesses keyed by `(project, transport)`. Restart-Google-Chat doesn't touch Telegram and vice versa. No `ProjectBot` refactor needed. Rationale: matches the existing one-process-per-bot model and keeps flaky transports isolated.
5. **nginx provisioning:** print the suggested vhost snippet at wizard completion. The bot doesn't write to `/etc/nginx/`. The operator deploys it themselves. Rationale: zero privilege escalation; the snippet is one paste from working.

## Architecture

One OS subprocess per `(project, transport)` pair, all supervised by the existing `link-project-to-chat.service` systemd unit. The manager (`start-manager` process) becomes the parent of N Telegram bots + M Google Chat bots, where M ≤ N.

At manager startup, for each project in `config.json`:

- If `projects.<name>.bot_token` is set → spawn Telegram subprocess (existing behavior).
- If `resolve_project_google_chat(<name>, config)` returns a valid config → spawn Google Chat subprocess on the resolved port.

The resolved config is computed once by the manager and passed to the child subprocess via a new `--google-chat-config-json` CLI argument, so the child doesn't re-read disk and can't disagree with the parent about which override applies.

Manager-spawned Google Chat bots share the existing supervision policy: restart-on-failure with 5 s backoff, pidfile + orphan-adoption from TODO §8, status tracking under `google_chat_pids: dict[str, int]` in `ProcessManager`.

## Components & schema

### Config schema (`src/link_project_to_chat/config.py`)

- `ProjectConfig` gains `google_chat: GoogleChatProjectOverride | None` (default `None`).
- `GoogleChatProjectOverride` is a new dataclass with every existing `GoogleChatConfig` field as `Optional`. `port` is the one field that becomes effectively required at validation time (every project needs a unique port — two bots can't share one).
- New helper `resolve_project_google_chat(project_name: str, config: Config) -> GoogleChatConfig | None`:
  - Starts from the top-level `google_chat` block (or all-defaults if absent).
  - Overlays any project-level fields that are non-`None`.
  - Runs the merged dict through the existing `google_chat/validators.py` startup-validation path.
  - Returns the merged `GoogleChatConfig` on success, or `None` if no override and no top-level block — i.e. the project doesn't run a Google Chat bot.
- Load/save round-trips through `_parse_project_config` / `_serialize_project_config` exactly like the existing `bot_username` / `bot_peer` fields, with `_parse_google_chat_override` / `_serialize_google_chat_override` helpers.

### Subprocess spawn (`src/link_project_to_chat/manager/process.py`)

- `ProcessManager.google_chat_pids: dict[str, int]` keyed by project name (alongside existing `bot_pids`).
- `start_google_chat_subprocess(project_name: str) -> bool` — resolves the config, JSON-encodes it, execs the child:
  ```
  link-project-to-chat start --project <name> --transport google_chat --google-chat-config-json <json>
  ```
  Records the PID; returns `False` if the resolved config is `None` or validation failed.
- `stop_google_chat_subprocess(project_name: str)` — SIGTERM the child, wait, clear the PID entry.
- `restart_google_chat_subprocess(project_name: str)` — stop then start; if start fails, the project drops out of `google_chat_pids` with the error logged.
- `discover_projects()` (existing method, extend it) — after the Telegram spawn loop, walk projects again and spawn Google Chat bots for any project whose `resolve_project_google_chat` returns non-`None`.

### CLI (`src/link_project_to_chat/cli.py`)

- New flag on `start`: `--google-chat-config-json STR`. When present, the child uses this resolved override instead of reading `config.json` for the `google_chat` block. The existing `--google-chat-port` / `--google-chat-host` / `--google-chat-public-url` flags continue to work as last-mile overrides for manual / single-bot invocations.

### Manager UI wizard (`src/link_project_to_chat/manager/conversation.py`, `src/link_project_to_chat/manager/bot.py`)

- Per-project view's button row grows three entries (executor-only, viewer can read the status):
  - When no override exists for the project: `[+ Add Google Chat]`.
  - When an override exists: `[Edit Google Chat]`, `[Remove Google Chat]`, `[Restart Google Chat]`.
- New wizard states `WIZARD_STATE_GCHAT_SA_PATH`, `WIZARD_STATE_GCHAT_PORT`, `WIZARD_STATE_GCHAT_PUBLIC_URL`, `WIZARD_STATE_GCHAT_COMMAND_ID`.
- Each state collects one field with validation (file exists / port in 1–65535 / URL well-formed / command-id is a positive int) and either re-prompts on invalid input or transitions to the next.
- Auth-audience-type defaults to `endpoint_url`. `project_number` defaults to the top-level value when set; the wizard can re-prompt it as a final optional field.
- On successful completion:
  1. `locked_config_rmw` writes `projects.<name>.google_chat` atomically.
  2. Wizard prints the suggested nginx vhost snippet to chat (one paste away from working — including the `proxy_pass http://127.0.0.1:<port>/google-chat/events` line and a `certbot --nginx -d <hostname>` reminder).
  3. `ProcessManager.start_google_chat_subprocess(<name>)` fires.
  4. Wizard confirms with a status message.

### Operational fields that stay at the top level

The top-level `google_chat` block is the operational-defaults source. Per-project overrides typically don't set these — they inherit:

- `host` (almost always `127.0.0.1` for the localhost+nginx topology)
- `callback_token_ttl_seconds`, `pending_prompt_ttl_seconds`
- `max_message_bytes`, `attachment_max_bytes`
- `auth_audience_type`

Per-project overrides typically set:

- `service_account_file` (per Chat app)
- `port` (always unique)
- `public_url` (per nginx vhost)
- `root_command_id` (per Chat app, picked in Cloud Console)
- `project_number` (per GCP project, for Workspace add-on flow)

## Data flow

**Cold start (manager boot, after systemd or `rebuild.sh`):**

```
systemd starts link-project-to-chat.service
  → start-manager process
    → ProcessManager.discover_projects() reads config.json
    → for each project:
        if bot_token set         → spawn Telegram subprocess (existing path)
        if google_chat resolves  → spawn Google Chat subprocess
    → ProcessManager.supervise_loop() watches all children
```

Each Google Chat subprocess is exactly today's `link-project-to-chat start --project X --transport google_chat ...` with the resolved config piped in via `--google-chat-config-json` instead of read from disk.

**Add-Google-Chat wizard:**

```
Operator → manager bot DM: tap project name → tap [+ Add Google Chat]
  → wizard prompts: service-account JSON path
  → wizard prompts: port
  → wizard prompts: public URL
  → wizard prompts: root_command_id
  → wizard saves projects.<name>.google_chat to config.json (atomic RMW)
  → wizard prints suggested nginx vhost snippet to chat
  → ProcessManager.start_google_chat_subprocess(<name>)
  → bot binds :<port>, prints uvicorn-ready log, registers on_message handler
  → wizard confirms "Bot started; deploy the nginx snippet and DM the bot from Google Chat to verify."
```

**Inbound message (steady state):**

```
Google Chat HTTPS POST → nginx :443 → 127.0.0.1:<port> (google_chat subprocess)
  → GoogleChatTransport.verify_request → enqueue
  → consumer loop → dispatch_event → MESSAGE handler
  → ProjectBot routes to backend (Claude) → reply via create_message
```

Unchanged from v1.1. The manager isn't in the message hot path.

**Restart via `rebuild.sh`:**

```
sudo rebuild.sh
  → pip install -e .[all]
  → systemctl restart link-project-to-chat.service
    → manager exits (SIGTERM)
    → manager respawns
    → ProcessManager.discover_projects() → spawn all Telegram + Google Chat subprocesses fresh
```

Closes the v1.1 caveat that "Google Chat bots don't auto-restart after rebuild" — they do now.

**Edit / Remove:**

- Edit: re-prompt the four fields pre-filled with current values; on save, atomic config update + `ProcessManager.restart_google_chat_subprocess(<name>)`.
- Remove: prompt confirmation, delete `projects.<name>.google_chat`, `ProcessManager.stop_google_chat_subprocess(<name>)`. nginx vhost is left in place — operator removes it manually since it's their reverse-proxy infra.

## Error handling

| Scenario | Behavior |
|---|---|
| Port collision at spawn | `GoogleChatTransport.start()` raises `RuntimeError: ... Address already in use`. `ProcessManager` catches non-zero exit within first ~5 s, logs WARNING, **no retry**. Manager UI shows `❌ failed to bind :<port>` with the tail. |
| Bad / missing service-account JSON | `validators.py` raises `GoogleChatStartupError` at startup. Subprocess exits, manager logs + reports, no retry. |
| Subprocess crash mid-flight | Existing supervise loop restarts on failure with 5 s backoff. After N crashes within a window, manager backs off (no restart storm). |
| Manager itself crashes | `systemd Restart=on-failure` revives it. `discover_projects()` re-spawns google_chat children. Orphans from prior life hold `:<port>` → new spawn logs `Address already in use` → operator runs the pidfile cleanup helper (TODO §8). |
| Wizard partial-write (operator disconnects mid-wizard) | All wizard steps mutate in-memory state; persistence happens once at the end via `locked_config_rmw`. Disconnect → no config change, no orphan subprocess. |
| nginx forwarding failure (vhost not deployed) | Out of scope for the bot. Operator sees "App not responding" in Chat; nginx access log shows 502. README documents the diagnostic chain. |
| Stale per-project override (top-level removed but override depended on it) | `resolve_project_google_chat()` returns `None` if the merge can't produce a valid config. Manager logs and skips the spawn; project status shows `❌ google_chat config incomplete`. Operator fixes via the wizard. |

**Backward compatibility:** today's single-top-level-block config keeps working. The migration is state-driven (like `_migrate_legacy_auth` — no `schema_version` field needed): on every load, `Config.load` checks the invariant "top-level `google_chat` exists AND no project has a `google_chat` override". When that's true:

- If exactly **one** project is configured → add `projects.<lone-project>.google_chat: {port: <top-level-port or 8090 default>}` automatically. Unambiguous. Migration is idempotent because the second run sees the override and exits early.
- If **multiple** projects are configured → no auto-association (we don't know which project owns the top-level block). The manager surfaces a one-shot setup message asking the operator to claim the top-level block for one project via the wizard. The message persists across restarts (it's just a derived UI state, not stored config) until the invariant is broken — which happens the moment any project gets an override.

Once any project has an override, the migration condition is false and the manager runs normally. There is no permanent "we ran the migration" flag.

## Testing strategy

### Unit tests

`tests/google_chat/test_config.py` — new tests:

- `resolve_project_google_chat` returns merged result when both top-level + per-project exist.
- Per-project `service_account_file` wins over top-level when both are set.
- Returns `None` cleanly when no config exists at all.
- Returns `None` when merge produces incomplete config (validates required-field gate).
- `port` is required on per-project override; missing port raises `ConfigError`.

`tests/test_config.py` — `GoogleChatProjectOverride` round-trips through `load_config` / `save_config` like other config types.

### ProcessManager tests (`tests/test_process_manager*.py`)

- `start_google_chat_subprocess` execs the right command line + tracks the PID under `google_chat_pids`.
- `stop_google_chat_subprocess` sends SIGTERM and removes the PID entry.
- `restart_google_chat_subprocess` is stop-then-start; preserves status reporting through both phases.
- Discovery on manager startup iterates projects and starts the right transports per project (mixed Telegram-only / Google-Chat-only / both / neither).
- Crash-and-restart: simulated subprocess exit within 5 s logs + reports + does **not** retry. Crash after the bind succeeded triggers normal respawn with backoff.

### Manager UI / wizard tests (`tests/test_manager*.py`)

- Add-Google-Chat wizard collects 4 fields, persists `projects.<name>.google_chat`, calls `start_google_chat_subprocess`, prints nginx snippet.
- Edit wizard pre-fills current values, persists deltas, calls `restart_google_chat_subprocess`.
- Remove flow confirms, deletes the override, calls `stop_google_chat_subprocess`, leaves nginx vhost alone.
- Buttons hide/show based on whether the override exists.
- Wizard rejects invalid port (out of 1–65535) and re-prompts.
- Disconnect mid-wizard leaves config untouched (no orphan subprocess).
- Executor-only: viewer role can see status but `[+ Add Google Chat]` is hidden / rejected.

### Migration tests

- First-load migration: a config with top-level `google_chat` block and exactly one project gets an explicit `projects.<lone-project>.google_chat: {port: <derived>}` added.
- With multiple projects and no overrides, migration surfaces a one-shot setup message instead of guessing.
- Idempotent: running the migration twice doesn't duplicate or change anything.

### Integration smoke test (`tests/test_projectbot_smoke.py` extension)

- Spin up the manager in test mode → discover a config with one Telegram + one Google Chat project → assert both subprocesses come up → POST a synthetic Google Chat event to the right port → assert the bot fast-acks + dispatches.

### Backward-compat regression

- The existing `test_projectbot_google_chat_end_to_end` test (the v1.1 one we have today, with only the top-level block) must continue to pass unchanged. That's the proof we didn't break single-bot deployments.

## Out of scope

- **nginx vhost provisioning** (writing, symlinking, cert renewal). The wizard prints a snippet; the operator deploys it.
- **GCP Chat-app creation.** Manual through the Cloud Console. One Chat app per GCP project, per Google's own limitation.
- **Multiple Chat apps per GCP project.** Google limitation; no workaround inside lptc.
- **`ProjectBot` multi-transport refactor.** Each `(project, transport)` is a separate subprocess. We don't merge them at the Python level.

## Acceptance criteria

- `link-project-to-chat configure` (or the manager wizard) adds a Google Chat bot to any project without editing `config.json` by hand.
- A single systemd unit (the manager) supervises both Telegram and Google Chat bots for every project that has one configured.
- Existing single-bot deployments keep working with no manual config migration — the one-shot top-level → per-project migration runs automatically and is idempotent.
- `sudo rebuild.sh` restarts all Google Chat bots alongside the manager. No more "Google Chat bot won't pick up new code" caveat.
- v1.1 regression: the existing `test_projectbot_google_chat_end_to_end` keeps passing.

## Estimated effort

~2-3 days of focused work. One feature branch, executable as a single subagent-driven-development plan with five tasks:

1. Per-project `google_chat` override on `ProjectConfig` (schema, parse/serialize, `resolve_project_google_chat`, validators, migration). Mechanical.
2. CLI: `--google-chat-config-json` resolved-override flag. Trivial.
3. `ProcessManager.start_google_chat_subprocess` + sibling stop/restart, discovery extension. Mechanical.
4. Manager UI: wizard states, buttons, persistence, nginx snippet printer. Hardest task — wizard state machine grows.
5. Docs (README + CHANGELOG + `docs/TODO.md` §1.5 → ✅) and integration smoke test.

## Cross-references

- TODO entry: `docs/TODO.md` §1.5.
- Predecessor spec: `docs/superpowers/specs/2026-04-25-transport-google-chat-design.md` (v1).
- v1.1 fixes plan: `docs/superpowers/plans/2026-05-17-google-chat-transport-fixes.md`.
- The two v1.1 production fix commits this builds on: `7da738a` (Workspace-add-on support) and `f88cf03` (test + bot.py startup-welcome skip).
