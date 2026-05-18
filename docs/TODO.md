# Project TODO — Consolidated Specs & Plans

_Last refreshed 2026-05-16 to add §8 for the post-v1.0.0 integration hardening sweep on branch `dev` (HEAD `d68d5c4`, 16 commits ahead of `main` after the `b86370c` plugin-system merge). Prior refresh 2026-05-15 to record the plugin-system port + `AllowedUser`-sole auth model rewrite (v1.0.0) on branch `feat/plugin-system` HEAD `cc7661e`. Refresh 2026-04-27 from all spec/plan documents under `docs/superpowers/specs/`, `docs/superpowers/plans/`, root-level planning docs, and direct code audit findings. Phase 4 post-completion audit added §2.1 on 2026-04-26 (HEAD `1d45dea`); Phase 5 (Gemini adapter) design drafted 2026-04-27._

Status legend: ✅ shipped · 🟡 in progress / partial · 📋 designed, not started · ⏳ small pending fix

Latest verification on `feat/plugin-system` HEAD `b20c40f`: `pytest -q` → **1142 passed, 5 skipped, 2 warnings** in 15.06s. Baseline at Task 0 was 1003 passed; +115 new tests across the 8 plan tasks (1118 at `cc7661e`), then +24 more from the post-merge P1 fix (`b20c40f`). Earlier TODO hardening batch verification was `pytest -q` → **992 passed, 5 skipped, 2 warnings**. The §8 hardening sweep on `dev` (HEAD `d68d5c4`) ships per-commit regression tests (≈30 new tests across 16 commits) but `pytest -q` has not been re-run end-to-end since the v1.0.0 baseline.

---

## 1. Transport Abstraction Track

Decouples bot logic from Telegram so other platforms can plug in.

### 1.1 Shipped specs

| Spec | Version | Spec doc | Plan | Status |
|---|---|---|---|---|
| #0 Core transport | v0.13.0 | [spec](superpowers/specs/2026-04-20-transport-abstraction-design.md) | [plan](superpowers/plans/2026-04-20-transport-abstraction.md) | ✅ |
| #0b Voice port | v0.14.0 | [spec](superpowers/specs/2026-04-20-transport-voice-port-design.md) | [plan](superpowers/plans/2026-04-20-transport-voice-port.md) | ✅ |
| #0a Group / team port | v0.15.0 | [spec](superpowers/specs/2026-04-21-transport-group-team-port-design.md) | [plan](superpowers/plans/2026-04-21-transport-group-team-port.md) | ✅ |
| #0c Manager port | v0.16.0 | [spec](superpowers/specs/2026-04-21-transport-manager-port-design.md) | [plan](superpowers/plans/2026-04-21-transport-manager-port.md) | ✅ |

`bot.py` has zero direct telegram imports; `manager/bot.py` runs through `TelegramTransport` with a 7-name allowlist for `Update`/`ConversationHandler` family pinned in [tests/test_manager_lockout.py](../tests/test_manager_lockout.py). See [where-are-we.md](../where-are-we.md) for full deliverable list.

### 1.2 Spec #0 review-fix follow-ups

Plan: [2026-04-25-transport-spec0-review-fixes.md](superpowers/plans/2026-04-25-transport-spec0-review-fixes.md) (Tasks 1–11; PR #6 review feedback).

Status tracker: [2026-04-25-spec0-followups.md](2026-04-25-spec0-followups.md)

| ID | Item | Severity | Status |
|---|---|---|---|
| F1 | Soften filename sanitizer's dotfile rejection (`transport/telegram.py:_safe_basename`) | Low | ✅ closed in `4a0bb69` |
| F2 | `bot.build()` should return `None` (`bot.py:1974`) | Trivial | ✅ closed in `4a0bb69` |
| F3 | Document `TelegramTransport.start()` vs `run()` dual entry | Trivial | ✅ closed in `4a0bb69` |
| F4 | Lockout test missing `encoding="utf-8"` on `Path.read_text()` (`tests/test_transport_lockout.py:37`) — fails on non-UTF-8 default locales since `bot.py` contains em-dashes/emojis | Trivial | ✅ closed |
| A1 | Migrate `_trusted_users` persistence to string identity ids (`config.py:bind_trusted_user`) | Medium | ✅ closed by spec #1 (`2a7b8e7`) |
| A2 | Replace `int(.native_id)` casts for `group_chat_id` (4 sites in `bot.py`) | Medium | ✅ closed — schema in `13dbdd9` (`BotPeerRef`/`RoomBinding`), call-site rewrite in `8906b51` |
| A3 | Manager `_guard` legacy int path vs `_guard_invocation` string path | Low | 📋 deferred to future Conversation primitive spec |
| C1 | Port `manager/bot.py` to Transport Protocol | — | ✅ closed by spec #0c |

### 1.3 New transport platforms (designed, not implemented)

| Spec | Spec doc | Plan | Status | Notes |
|---|---|---|---|---|
| #1 Web UI | [spec](superpowers/specs/2026-04-21-transport-web-ui-design.md) | [plan](superpowers/plans/2026-04-21-web-transport.md) · [review-fix plan](superpowers/plans/2026-04-25-transport-spec1-review-fixes.md) | ✅ | Shipped 2026-04-25 (commits `6c12b39`..`d24ef52`); review-fix landed same day (commits `77abcff`..`7b73b8d`) closing P1.1/P1.2/P1.3/P1.4/P2. First non-Telegram transport. FastAPI + HTMX + SSE + SQLite. Closed A1, partially closed A2 (schema only). |
| #2 Discord | [spec](superpowers/specs/2026-04-21-transport-discord-design.md) | [plan](superpowers/plans/2026-04-21-discord-transport.md) | 📋 | Uses discord.py 2.x, depends on #1 primitives. |
| #3 Slack | [spec](superpowers/specs/2026-04-21-transport-slack-design.md) | [plan](superpowers/plans/2026-04-21-slack-transport.md) | 📋 | slack_bolt + Socket Mode; final cross-platform validation. |
| #4 Google Chat | [spec](superpowers/specs/2026-04-25-transport-google-chat-design.md) | [plan](superpowers/plans/2026-04-25-transport-google-chat.md) | ✅ | HTTP Chat app events + Google Chat REST API; lifecycle, command auth, cards in/out, prompt submit/update, attachment download, and both audience modes implemented. v1.2 ships manager integration + per-project `google_chat` override (see §1.5 ✅). v1.1 caveats: outbound upload deferred pending user-OAuth support, per-process callback tokens, in-memory event dedupe, space-bound prompts by default, native inline `REQUEST_DIALOG` deferred, and no Pub/Sub delivery mode. |

### 1.4 Plugin system port + `AllowedUser` auth model rewrite (v1.0.0)

Branch: `feat/plugin-system` (HEAD `cc7661e`, 11 commits ahead of `main` as of 2026-05-15).

Design doc: [2026-05-13-merge-gitlab-plugin-system-design.md](superpowers/specs/2026-05-13-merge-gitlab-plugin-system-design.md) (14 review iterations: rev `2026-05-14` → `2026-05-14n`).
Plan: [2026-05-13-merge-gitlab-plugin-system.md](superpowers/plans/2026-05-13-merge-gitlab-plugin-system.md) (8 tasks; executed via subagent-driven-development with two-stage review per task).

Verification: `pytest -q` → **1118 passed, 5 skipped** (baseline 1003 + 115 net new tests).

| Task | Commit | Scope | Status |
|---|---|---|---|
| 0 | `9dd1d2d` | Branch + baseline pin | ✅ |
| 1 | `9dd40b1` | `plugin.py` framework (`Plugin`, `PluginContext`, `BotCommand`, `load_plugin`); `Transport.on_stop` Protocol method across Telegram/Fake/Web; TelegramTransport dynamic `on_command` PTB-handler registration fix; operational scripts `restart.sh` / `stop.sh` | ✅ |
| 2 | `2a9c719` | ProjectBot plugin lifecycle: `_topo_sort`, `_dispatch_plugin_*`, `_plugin_context_prepend` (Claude-only), `_wrap_plugin_command` (active-plugin + auth+role + persist), `_init_plugins` (CORE_COMMAND_NAMES blocklist + cross-plugin collision check), `_shutdown_plugins`. Hooks fire from `_on_stream_event`, `_on_task_complete`, `_on_text`, `_on_button`, `_build_user_prompt`. Plus I-1 follow-up filtering kwargs in `PluginContext.send_message` Transport fallback. | ✅ |
| 3 | `50e5cb7` + `92dc9b9` + `7792155` | Config schema: `AllowedUser(username, role, locked_identities: list[str])` dataclass, `_migrate_legacy_auth` (3-shape discriminator + web-session normalization), `resolve_project_allowed_users` (project→global fallback returning `(users, source)`), `locked_config_rmw` + `save_config_within_lock` atomic RMW. Two follow-up fixes for save-time precedence (legacy mutations win on save; per-user UNION preserves roles + non-Telegram identities). | ✅ |
| 4 | `4abece6` | CLI surfaces: `plugin-call` (standalone plugin tool invocation), `migrate-config [--dry-run]`, `configure --add-user USER[:ROLE]` / `--remove-user` / `--reset-user-identity USER[:TRANSPORT]`. Legacy `--username` / `--remove-username` aliased with deprecation. `start` and `start-manager` honor `Config.migration_pending` (eager save). `start-manager` hard-fails on empty `Config.allowed_users`. ManagerBot.__init__ transition shim. | ✅ |
| 5 | `ab6c4fc` + `ce04662` | AuthMixin rewrite around `AllowedUser` (sole source). `_get_user_role` (identity-lock + username fallback + same-transport spoof guard); `_auth_identity` (fail-closed on empty + brute-force lockout); `_require_executor`. ProjectBot `_persist_auth_if_dirty` (scope-aware project/global, per-user merge via `locked_config_rmw`), `_guard_executor` (persists on success and viewer-deny), `_with_auth_persist` + `_wrap_with_persist`. 22 state-changing commands gated. 16 state-changing button prefixes/exact-values gated. Manager bot rewrite (`_users_config_path`, `_persist_auth_if_dirty`, rewritten `_guard` + `_edit_field_save`). Legacy fields removed from `ProjectConfig` and `Config`. Follow-up fix wraps manager command/button paths with `_wrap_with_persist` so first-contact locks survive on transport-native paths. | ✅ |
| 6 | `9e0b994` + `f3d00ec` | Manager UI: Plugins toggle (per-project, restart-required) with active/inactive markers, executor-gated. Six new user-management commands: `/users` (viewer-allowed), `/add_user`, `/remove_user`, `/promote_user`, `/demote_user`, `/reset_user_identity` (all executor-only via `_require_executor_or_reply`). Legacy `_on_*_from_transport` handlers REMOVED. Follow-up fix adds `COMMANDS` entries for the three new manager commands, rate-limit check, and `Unauthorized.` reply for consistency with `_guard_invocation`. | ✅ |
| 7 | `cc7661e` | Docs + version bump: README Plugins section (activation, transport-portable authoring, role-based access), CHANGELOG v1.0.0 entry with BREAKING CHANGES call-out, version bumped to `1.0.0` in both `pyproject.toml` and `__init__.py`. `test_version_is_consistent_across_pyproject_and_init` regression test. Parametrized `on_stop` contract test in `tests/transport/test_contract.py` over `[fake, telegram, web]` (closes Task 1 I-2 follow-up). Dead-code cleanup: `_on_trust`, `_with_auth_persist`, dead `trusted_user_*` pass-through kwargs on `ProjectBot.__init__` and `run_bot`. | ✅ |
| 7+ | `fbe3172` | Docs sync: CLAUDE.md/AGENTS.md updated to describe `plugin.py`, the new `_auth.py` AuthMixin, `AllowedUser`+migration helpers in `config.py`, plugin lifecycle wiring in `bot.py`, manager UI for plugins + user-management. New `docs/TODO.md` §1.4 plugin-system-port section. | ✅ |
| **7++** | `b20c40f` | **P1 fix (post-merge review).** (a) Manager bot's viewer role now enforced on all write paths: new `_guard_executor_invocation` / `_guard_executor` / `_require_executor_button` helpers gate `/start_all`, `/stop_all`, `/setup`, `/model`, `/add_project`, `/edit_project`, `/create_project`, `/create_team`, `/delete_team`, and the button branches `proj_start_*`, `proj_stop_*`, `proj_edit_*`, `proj_efld_*`, `proj_model_*`, `proj_remove_*`, `global_model_*`, `team_start_*`, `team_stop_*`, `setup_*`. `_handle_setup_input` and the `pending_edit` branch of `_edit_field_save` got defense-in-depth executor checks too. (b) Project add/edit `--username` (CLI) and `username` (manager wizard + `_apply_edit`) now translate to `allowed_users=[{username,executor}]` instead of writing the legacy `username` key (which `save_config` silently strips, previously authorizing nobody). +24 regression tests. | ✅ |

#### Outstanding follow-ups (non-blocking, queue for v1.0.1)

| Severity | Item | Location |
|---|---|---|
| 🟠 Important | Stale "use --username" error message in `run_bot` (should mention `--add-user`). | [bot.py:3194](../src/link_project_to_chat/bot.py) |
| 🟠 Important | No test for plugin-`start()`-failure → inert command behavior. The `_RecordingPlugin.start_raises` flag exists but no test sets it `True`. | [tests/test_bot_plugin_hooks.py](../tests/test_bot_plugin_hooks.py) |
| 🟠 Important | Username-fallback path in `_get_user_role` silently rejected a Google Chat user whose `AllowedUser` had only `username` (no `locked_identities`). Identity arrived with the correct `handle`; no spoof-guard WARNING and no "Locked identity ... on first contact" INFO log fired, so the failure is invisible without temporary INFO probes. Workaround: lock the user manually via `locked_identities: ["google_chat:users/<id>"]`. Suspected cause: `_failed_auth_counts[ident_key]` saturated by upstream routing drops *before* the user was added to `allowed_users`, OR the hot-reload merge dropped the new entry. Add a regression that reproduces from a clean slate + verifies the `Locked identity` INFO log on first contact. | [_auth.py](../src/link_project_to_chat/_auth.py) |
| 🟡 Minor | README Quick start / Multi-user sections still recommend the deprecated `--username` flag. CHANGELOG promotes `--add-user`. | [README.md](../README.md) |
| 🟡 Minor | README plugin snippet imports unused `ChatRef`. | [README.md](../README.md) |
| 🟡 Minor | `ProjectBot.__init__` comment slightly imprecise about which legacy kwargs remain. | [bot.py](../src/link_project_to_chat/bot.py) |
| 🟡 Minor | `_persist_auth_if_dirty` swallows all exceptions; no N-failure alerting for repeated disk-write failures. | [bot.py](../src/link_project_to_chat/bot.py), [manager/bot.py](../src/link_project_to_chat/manager/bot.py) |

### 1.5 Google Chat v1.2 (manager integration + multi-bot config) ✅

**Goal:** make adding a Google Chat bot for an lptc-project feel like adding a Telegram bot does today — `/add_project` (or the equivalent) in the manager, fill in the per-project Chat-app secrets, and the manager handles the supervised subprocess. Before v1.2 every Google Chat bot stood alone: separate config block, separate port, separate systemd unit, separate nginx vhost. That's fine for 1-2 bots, untenable for the 6 projects already in `config.json`.

**Shipped on `feat/google-chat-manager`** (branched off `dev` at `b2097f5`, baseline pin).

| # | Scope | Files | Status |
|---|---|---|---|
| 1 | **Per-project `google_chat` override on `ProjectConfig`.** `ProcessManager` picks per-project values when starting; top-level `google_chat` block becomes the operational-defaults source. Config schema + load/save + per-field merge + one-shot migration of single-project deployments. | `config.py` (+ tests in `tests/google_chat/test_config.py`) | ✅ `03ca136`..`8f0c285` (12 commits: dataclass, validate, parse/serialize, audience, project override, malformed warn, resolve helper, docstrings, migration, heuristic, CLI flag + diagnostics) |
| 2 | **`ProcessManager` spawns `google_chat` bots.** Loop alongside the Telegram spawn path; per-project transport selection via per-project config; pass `--transport google_chat --google-chat-config-json …`; PID tracking, clean-exit reaping, failure capture without restart loop, SIGKILL escalation, autostart asymmetry documented. | `manager/process.py` (+ tests in `tests/test_process_manager*.py`) | ✅ `c89ee42`..`71b7c0d` (8 commits: start subprocess, double-spawn guard, stop/restart, SIGKILL escalation, autostart, autostart docs, failure capture, clean-exit reap) |
| 3 | **Port allocation + collision detection.** Explicit per-project `port` field; manager refuses to start on conflict (matches the explicit nginx vhost the operator already maintains). | `manager/process.py`, `config.py` | ✅ folded into task 1 (config schema includes `port`) + task 2 (spawn reads it) |
| 4 | **Manager UI: add/edit Google Chat bot for a project.** Wizard collects service-account JSON path, port, public URL, root_command_id; persists to `projects.<name>.google_chat`; prints ready-to-paste nginx vhost on completion. Buttons under per-project view: `[Add Google Chat]` / `[Edit Google Chat]` / `[Remove Google Chat]` / `[Restart Google Chat]`. Executor-only. | `manager/bot.py`, `manager/conversation.py` (+ tests in `tests/test_manager*.py`) | ✅ `03f7b45`..`32a9e1c` (5 commits: buttons, dispatch stub, wizard, URL/command-id validation, edit/remove/restart handlers) |
| 5 | **Docs + migration.** README setup section reflects per-project flow as the new default; CHANGELOG entry; this TODO update. Migration: today's single top-level `google_chat` block auto-claims for single-project deployments on first load; multi-project no-override deployments are left alone for the operator to claim via the wizard. | `README.md`, `docs/CHANGELOG.md`, `docs/TODO.md` | ✅ this commit |

**End-to-end smoke** (Task 14): `tests/manager/test_google_chat_smoke.py` — `slow`-marked, spawns a real google_chat subprocess against the manager and asserts the supervised lifecycle. Commits `489c00c`..`a9e1b7c`.

**Verification:** merge SHA range `f88cf03..a9e1b7c` (Tasks 1-14, plus baseline pin `b2097f5` and pre-plan diagnostic/cleanup commits). `pytest -q` → **1602 passed, 1 failed, 6 skipped, 2 warnings** in 39.93s. The single failure is the pre-existing environmental port-8090 collision (`tests/google_chat/test_transport.py::test_start_fires_on_ready_callbacks_with_self_identity`) flagged in earlier task verifications; it is not caused by this work.

**Operational items intentionally out of scope:**

- **nginx vhost provisioning** — operator-supplied. Each new Google Chat bot still needs an HTTPS subdomain (or path) routed to the per-project port. The wizard prints a ready-to-paste vhost; we don't automate certbot.
- **Cloud Console Chat app creation** — manual. One GCP project per Chat app per Google's own constraint.
- **Multiple Chat apps per GCP project** — Google limitation, no workaround inside lptc.

---

## 2. Backend Abstraction Track

Multi-backend support (Claude → Codex → Gemini / others). Phases 1–4 shipped for the Claude + Codex pair; Phase 5 (Gemini adapter) drafted 2026-04-27.

| Phase | Spec | Plan | Status |
|---|---|---|---|
| Phase 1 — Claude extraction | [spec](superpowers/specs/2026-04-23-backend-phase-1-claude-extraction-design.md) | [plan](superpowers/plans/2026-04-23-backend-phase-1-claude-extraction.md) | ✅ |
| Phase 2 — Config & `/backend` command | [spec](superpowers/specs/2026-04-23-backend-phase-2-config-and-backend-command-design.md) | [plan](superpowers/plans/2026-04-23-backend-phase-2-config-and-backend-command.md) | ✅ |
| Phase 3 — Codex adapter | [spec](superpowers/specs/2026-04-23-backend-phase-3-codex-adapter-design.md) | [plan](superpowers/plans/2026-04-23-backend-phase-3-codex-adapter.md) | ✅ |
| Phase 4 — Capability expansion & hardening | [spec](superpowers/specs/2026-04-23-backend-phase-4-capability-expansion-design.md) | [plan](superpowers/plans/2026-04-23-backend-phase-4-capability-expansion-readiness.md) | ✅ |
| Phase 5 — Gemini adapter (conservative) | [spec](superpowers/specs/2026-04-27-backend-phase-5-gemini-adapter-design.md) | [plan](superpowers/plans/2026-04-27-backend-phase-5-gemini-adapter.md) | 📋 |

Phase 1 evidence: `src/link_project_to_chat/backends/` (5 files: `base.py` `AgentBackend` Protocol, `claude.py` `ClaudeBackend`, `claude_parser.py`, `factory.py`, `__init__.py`). `claude_client.py` was removed; `ProjectBot` constructs a Claude backend via the factory. Commits: `0ab1c56` (Protocol+factory), `ee53d19` (move Claude client), `f1acefd` (inject into TaskManager), `f20d8d1` (route through factory + remove shim).

Phase 2 evidence: `ProjectConfig.backend` + `backend_state` dataclass fields (`config.py:55-56`), `Config.default_backend` (`config.py:104`), legacy-flat-field migration helpers (`_legacy_backend_state`, `_mirror_legacy_claude_fields`, `_effective_backend_state` at `config.py:194-241`), `/backend` command registered in `bot.py:53`, `ProjectBot.__init__(backend_name, backend_state)` (`bot.py:111`), capability-gated `/thinking`/`/permissions`/`/compact` responses, and manager-side propagation. Commits: `4828120` (config migration + dual-write), `7917b44` (helper migration + persistence call sites), `f73b43e` (`/backend` switching + capability gating), `283c5ed` (manager+CLI propagation), `cb91bb4` (parameterize Telegram-awareness preamble), `552df09` (post-phase-2 cleanup).

Phase 3 evidence: `CodexBackend` (`backends/codex.py`) implements the `AgentBackend` Protocol against the `codex exec --json` / `codex exec resume --json` CLI surface, registered with the factory under name `codex` and selectable via `/backend codex`. `codex_parser.py` translates the Codex JSONL stream (`thread.started`, `item.completed` agent_message, `turn.completed`, error frames) into the shared `StreamEvent` taxonomy. The new `BaseBackend` helper (`backends/base.py`) hosts a shared `_prepare_env` with per-backend keep/scrub allowlists — Claude scrubs `OPENAI_*` and `ANTHROPIC_*`, while Codex keeps `OPENAI_*`/`CODEX_*` but still scrubs `ANTHROPIC_*` and other token patterns. `CODEX_CAPABILITIES` declares conservative flags (no thinking, no compact, no allowed_tools, no usage-cap detection; resume enabled) and now exposes `/permissions` via Codex CLI sandbox controls: `plan` maps to read-only sandbox, `acceptEdits`/`dontAsk`/`auto` map to `--full-auto`, and bypass modes map to Codex's explicit dangerous bypass flag. Team routing context is now backend-level: `ProjectBot` injects the same peer/self @handle and relay rules into Claude and Codex team bots, and Codex prepends that note to the `codex exec` prompt. Live coverage runs only when `RUN_CODEX_LIVE=1` is set and the `codex_live` pytest marker is selected (`tests/backends/test_codex_live.py`); the live tests spawn a real `codex` subprocess inside a fresh git-initialised tmp dir, verify the round-trip emits OK as both a `TextDelta` and the closing `Result`, and confirm a follow-up turn replies AGAIN while reusing the same `session_id`. Commits: `da86be3` (codex CLI findings + parser fixtures), `5cccb8b` (shared env-policy helper), `efa1ea6` (codex JSONL parser), `01d5b80` (codex backend adapter), `7d216ec` (guard claude-only bot commands under codex), plus this Task 6 commit locking capability declarations, env policy, contract test, and live coverage.

Phase 4 evidence: Codex model selection and reasoning effort shipped in `93f8b9c`; `/backend` button picker shipped in `e2e2143`; provider-aware `/status` surfaced effort, request count, last duration, and Codex token usage in `7245199`; friendly model-label resolution shipped in `d0e4b97`; per-chat cross-backend context history shipped in `caabb76`; backend-level permissions were generalized and Codex `/permissions` enabled in `2b1dba6`; the final status slice records and displays permissions, Claude tool allow/deny lists, usage-cap state, and last backend error. Remaining Codex `False` capability flags (`supports_thinking`, `supports_compact`, `supports_allowed_tools`, `supports_usage_cap_detection`) reflect missing CLI evidence rather than adapter conservatism.

Phase 5 scope (designed 2026-04-27, not started): adds `GeminiBackend` wrapping Google's official `gemini-cli` (npm `@google/gemini-cli`). Phase-3-style conservative adapter — single design + plan doc, ships opt-in via `/backend gemini`, all `BackendCapabilities` flags `False` except `supports_resume` (which Task 1 may flip True if a session-id surface is found). Module layout mirrors Codex (`backends/gemini.py` + `gemini_parser.py` + capture-driven test fixtures). Env policy keeps `GEMINI_*` / `GOOGLE_*`, scrubs Anthropic/OpenAI/Codex tokens. Ships in parallel with §2.1 follow-ups; preempts the P4-C2 zombie-proc fix in Gemini's own lifecycle code so the third backend doesn't inherit the bug. Out of scope: `/model`, `/effort`, `/permissions`, `/thinking`, `/compact` for Gemini — promotion deferred to a future Phase 6 once real-usage gaps surface (the Phase 4 trigger pattern). Implementation plan: [2026-04-27-backend-phase-5-gemini-adapter.md](superpowers/plans/2026-04-27-backend-phase-5-gemini-adapter.md) — 6 tasks, Task 1 captures CLI findings as a hard gate before any adapter code lands.

### 2.1 Phase 4 post-completion audit (2026-04-26)

Two code-review passes against the phase 4 range (`9280aef..1d45dea` on `feat/transport-abstraction`) surfaced follow-ups. The phase 4 design and tests are shipped — these are quality / hardening items, not blockers.

Severity legend: 🔴 Critical · 🟠 Important · 🟡 Minor · 📚 Doc / Test

#### Critical — correctness, leaks, security

| ID | File:line | Item | Status |
|---|---|---|---|
| P4-C1 | [bot.py:649-660](../src/link_project_to_chat/bot.py) | `_log_assistant_turn(task.chat, task.result)` ran BEFORE `_finalize_claude_task`, capturing only the LAST text block. Buffer with full narration is in `live_text.buffer` and only popped inside `_finalize_claude_task`. Conversation history replayed to the next turn was missing the narration the user actually saw. | ✅ closed — logs after finalize from the full live buffer |
| P4-C2 | [backends/codex.py:214-218](../src/link_project_to_chat/backends/codex.py) | `chat_stream`'s early-close path could orphan the codex subprocess before `turn.completed`. | ✅ closed — early generator close terminates/reaps proc with regression coverage |
| P4-C3 | [tests/conftest.py:25-29](../tests/conftest.py) | `_isolate_home` set `HOME` only, which does not isolate `Path.home()` on Windows. | ✅ closed — fixture sets `HOME`, `USERPROFILE`, `HOMEDRIVE`, and `HOMEPATH` with sentinel coverage |
| P4-C4 | [conversation_log.py](../src/link_project_to_chat/conversation_log.py) callers in [bot.py:264-293](../src/link_project_to_chat/bot.py) | Synchronous `sqlite3` open+insert+commit ran directly on the asyncio event loop in conversation-history paths. | ✅ closed — async wrappers use `asyncio.to_thread` |

#### Important — UX, races, contract gaps

| ID | File:line | Item | Status |
|---|---|---|---|
| P4-I1 | [conversation_log.py:157-176](../src/link_project_to_chat/conversation_log.py) | `format_history_block` had per-turn truncation but no total-block cap. | ✅ closed — bounded by `HISTORY_BLOCK_CHAR_CAP`, oldest turns dropped first |
| P4-I2 | [bot.py:1042-1053](../src/link_project_to_chat/bot.py), [bot.py:1065-1076](../src/link_project_to_chat/bot.py) | `/model` and `/effort` silently dropped typed args. | ✅ closed — typed args apply or return usage text |
| P4-I3 | [bot.py:61](../src/link_project_to_chat/bot.py) | `COMMANDS["effort"]` hardcoded Claude/Codex-specific levels. | ✅ closed — backend-agnostic help text |
| P4-I4 | [bot.py:1805-1814](../src/link_project_to_chat/bot.py) | `thinking_set_*` button branch was not gated on `supports_thinking`. | ✅ closed — stale thinking buttons are rejected under unsupported backends |
| P4-I5 | [bot.py:1238-1276](../src/link_project_to_chat/bot.py) | `_switch_backend` was non-atomic against concurrent submissions. | ✅ closed — per-bot backend-switch lock covers switch and submit paths |
| P4-I6 | [bot.py:638-650](../src/link_project_to_chat/bot.py) | `_on_task_complete` could persist a session under the wrong backend after a concurrent swap. | ✅ closed — task captures backend at submit time and completion persists to that backend slot |
| P4-I7 | [backends/codex.py:106-116, 271-272](../src/link_project_to_chat/backends/codex.py) | `set_permission` accepted arbitrary strings until command build time. | ✅ closed — Codex validates permission modes immediately |
| P4-I8 | [backends/codex.py:64, 184](../src/link_project_to_chat/backends/codex.py) | Codex default model displayed as `default`. | ✅ closed — default seeded from Codex model options and startup no longer leaks Claude defaults |
| P4-I9 | [bot.py:2025-2092](../src/link_project_to_chat/bot.py) | `_compose_status` mixed required and optional status-key access. | ✅ closed — status uses safe `.get` access and `BackendStatus` documents optional keys |

#### Minor — hardening, polish, drift

| ID | File:line | Item | Status |
|---|---|---|---|
| P4-M1 | [conversation_log.py:36-37](../src/link_project_to_chat/conversation_log.py) | `…[truncated]` marker could collide with literal user text. | ✅ closed — sentinel is `[__history_truncated__]` |
| P4-M2 | [conversation_log.py:50-51](../src/link_project_to_chat/conversation_log.py) | `_connect()` opened without `check_same_thread=False`. | ✅ closed |
| P4-M3 | [conversation_log.py:46-49, 65-71](../src/link_project_to_chat/conversation_log.py) | Conversation-log chmod failures were swallowed silently. | ✅ closed — warnings include exception context |
| P4-M4 | [bot.py:1128-1132](../src/link_project_to_chat/bot.py) | `_persist_context_settings` docstring was stale. | ✅ closed |
| P4-M5 | [backends/codex.py:25, 48-54](../src/link_project_to_chat/backends/codex.py) | `CODEX_MODELS` and `MODEL_OPTIONS` duplicated model lists. | ✅ closed — derived from one source |
| P4-M6 | [bot.py:2087-2092](../src/link_project_to_chat/bot.py) | `_short_status_value` collapsed newlines. | ✅ closed — preserves whitespace while truncating |
| P4-M7 | [bot.py:2094-2098](../src/link_project_to_chat/bot.py) | `_on_status_t` bypassed chunking. | ✅ closed — uses `_send_to_chat` |
| P4-M8 | [backends/claude.py:81-82](../src/link_project_to_chat/backends/claude.py) | Claude model constants could drift from `MODEL_OPTIONS`. | ✅ closed — derived from one source |
| P4-M9 | [bot.py:248-262](../src/link_project_to_chat/bot.py) | `_history_block` defensive `getattr` lacked coverage/comment. | ✅ closed — async-wrapper tests exercise the defensive path |
| P4-M10 | [backends/codex.py:25, 48-54](../src/link_project_to_chat/backends/codex.py) | Codex model-order comment overclaimed cache sync. | ✅ closed — stale claim removed |
| P4-M11 | [conversation_log.py:157-176](../src/link_project_to_chat/conversation_log.py) | `/context off` logging behavior was undocumented. | ✅ closed — inline comment documents re-enable continuity |

#### Test gaps

| ID | Item | Status |
|---|---|---|
| P4-T1 | [tests/test_conversation_log.py:108-113](../tests/test_conversation_log.py) — uses `try/except` rather than `pytest.raises(ValueError)`; can mask unrelated `ValueError`s. | ✅ closed |
| P4-T2 | [tests/test_context_command.py:1199-1232](../tests/test_context_command.py) — `asyncio.gather(..., return_exceptions=True)` silently eats backend errors. | ✅ closed |
| P4-T3 | [tests/test_backend_command.py](../tests/test_backend_command.py) — status test depended on `_last_usage` instead of the public status surface. | ✅ closed |
| P4-T4 | No regression test for `CodexBackend.chat_stream` early-cancellation (P4-C2). | ✅ closed |
| P4-T5 | No test for `_switch_backend` race window (P4-I5/P4-I6). | ✅ closed |
| P4-T6 | [tests/backends/test_contract.py](../tests/backends/test_contract.py) — no contract test for `set_permission`/`current_permission` round-trip across registered backends. | ✅ closed |

#### Doc drift

| ID | File | Item | Status |
|---|---|---|---|
| P4-D1 | [docs/CHANGELOG.md](CHANGELOG.md) | Phase 4 entry didn't cite the commit range. | ✅ closed |
| P4-D2 | [README.md](../README.md) | `/thinking` doc named Claude instead of capability-gated active backend support. | ✅ closed |
| P4-D3 | [docs/superpowers/specs/2026-04-23-backend-phase-4-rollout-review.md](superpowers/specs/2026-04-23-backend-phase-4-rollout-review.md) | Rollout review needed a follow-up note after post-completion audit work. | ✅ closed |
| P4-D4 | [tests/conftest.py:9-12](../tests/conftest.py) | Caveat about `Path.home()` module-load constants was documented but not enforced. | ✅ closed — fresh-import sentinel test added |

#### Reproduction notes

- Historical full-suite note from audit HEAD: 885 passed, 30 skipped, 2 failed (`tests/test_cli_transport.py` hardcoded `/tmp/x`). Superseded by the TODO hardening batch, which closes that blocker.
- Targeted phase 4 suites: `pytest tests/backends/{test_env_policy,test_capability_declaration,test_codex_backend,test_contract,test_claude_backend}.py` → **41 passed, 1 skipped**; `pytest tests/test_{backend_command,capability_gating,bot_streaming,context_command,conversation_log}.py` → **90 passed**; `pytest tests/test_{bot_backend_lockout,bot_team_wiring}.py` → **49 passed**.

PM analysis: [backend-abstraction-pm-analysis.md](backend-abstraction-pm-analysis.md). Phase 1 smoke evidence: [backend-phase-1-smoke-evidence.md](backend-phase-1-smoke-evidence.md).

§8 below tracks the env-policy follow-up (`cb4e967` + `6d3d6c1`) under the hardening sweep — it carries §2 backend hygiene but landed alongside the manager/web/CLI fixes on `dev`.

---

## 3. Earlier Feature Tracks (Shipped)

| Feature | Spec | Plan | Status |
|---|---|---|---|
| Automated project creation | [spec](superpowers/specs/2026-04-13-automated-project-creation-design.md) | [plan](superpowers/plans/2026-04-13-automated-project-creation.md) | ✅ |
| Claude skills / personas | [spec](superpowers/specs/2026-04-13-claude-skills-design.md) | (rolled into earlier work) | ✅ |
| Voice messages | (folded into spec #0b) | [plan](superpowers/plans/2026-04-14-voice-messages.md) | ✅ |
| `/create_team` command | [spec](superpowers/specs/2026-04-17-create-team-command-design.md) | [plan](superpowers/plans/2026-04-17-create-team-command.md) | ✅ |
| Dual-agent AI team | [spec](superpowers/specs/2026-04-17-dual-agent-ai-team-design.md) | [plan](superpowers/plans/2026-04-17-dual-agent-ai-team.md), [merged v2](superpowers/plans/2026-04-19-dual-agent-team-merged.md) | ✅ |
| Live streaming + thinking toggle | [spec](superpowers/specs/2026-04-20-live-stream-and-thinking-toggle-design.md) | [plan](superpowers/plans/2026-04-20-live-stream-and-thinking-toggle.md) | ✅ |

---

## 4. Security & Quality Audit

Audit: [issues-2026-04-22.md](issues-2026-04-22.md) — 29 issues + 3 post-audit operational fixes. Remediation: [2026-04-22-remediation-plan.md](2026-04-22-remediation-plan.md). PM gate: [review-2026-04-22-batch1.md](review-2026-04-22-batch1.md).

### 4.1 Batch 1 — Critical + High (security)

| ID | File | Item | Status |
|---|---|---|---|
| C1 | `task_manager.py:329–336` | `/run` resource exhaustion — concurrent cap (3) | ✅ APPROVED |
| H1 | `task_manager.py:241` | Scrub error messages (`_SENSITIVE_RE` for tokens, paths) | ✅ APPROVED w/ note |
| H2 | `bot.py:1636` | `str.startswith` → `Path.is_relative_to` (path traversal) | ✅ APPROVED (commit `3710342`) |
| H3 | `claude_client.py:255–261` | Scrub env vars (`*_TOKEN`, `*_KEY`, `AWS_*`, etc.) before subprocess | ✅ APPROVED (commit `3710342`) |
| H4 | `botfather.py:110` | Session file chmod race — chmod before `client.start()` | ✅ APPROVED |
| H5 | `tests/test_security.py` | Path-traversal tests for `_send_image` | ✅ APPROVED |
| H6 | `tests/test_security.py` | Env-var scrubbing tests | ✅ APPROVED |

**Batch 1: FULLY APPROVED.**

### 4.2 Batch 2 — Medium

| ID | File | Item | Status |
|---|---|---|---|
| M1 | `bot.py` | Atomic `find_by_message` + cancel under lock / `asyncio.shield` | ✅ shipped — cancel happens synchronously before live-message await; backend switch uses lock |
| M2 | `backends/claude.py` | `chat()` raises typed exception instead of `"Error:..."` strings (`ClaudeStreamError`) | ✅ shipped (PR #6 commit `04619cf`) |
| M4 | `config.py` | Document predictable lock path | ✅ closed |
| M5 | `config.py` | Replace O(n) loop with dict-keyed update (`_merge_project_entry`) | ✅ shipped (commit `84ef5f0`) |
| M6 | `task_manager.py:518–522` | `heapq.nlargest` instead of full sort in `list_tasks` | ✅ shipped (commit `84ef5f0`) |
| M8 | `bot.py` | `tempfile.gettempdir()` instead of hardcoded `/tmp/...` | ✅ shipped |
| M10 | `tests/` | Auth tests: concurrent attempts, 30 msg/min boundary, multi-user precedence | ✅ shipped (`tests/test_auth_m10.py`) |
| M11 | `tests/` | Config I/O: malformed JSON, perm errors, concurrent access | ✅ shipped (`tests/test_config_m11.py`) |
| M12 | `tests/` | `LiveMessage._rotate_once` boundary tests (now in `transport/streaming.py`) | ✅ closed (`tests/transport/test_streaming.py`) |
| M13 | `_auth.py` + `docs/` | Document `_auth()` + `docs/auth-migration.md` | ✅ shipped (commit `84ef5f0`) |

Retired: M3, M7, M9 (re-verified 2026-04-23).

### 4.3 Batch 3 — Low (all shipped)

| ID | File | Item | Status |
|---|---|---|---|
| L1 | `config.py` | Replace `print(..., file=sys.stderr)` with `logger.warning` | ✅ shipped (commit `60f3dba`) |
| L2 | `transport/streaming.py` (was `livestream.py`) | Hard-truncate fallback after 5 binary-search iterations | ✅ shipped (commit `60f3dba`) |
| L3 | `group_state.py` | LRU eviction (max 500) on `_states` | ✅ shipped (commit `60f3dba`) |
| L4 | `transport/streaming.py` | Named constants for magic numbers | ✅ shipped (commit `60f3dba`) |
| L5 | `_auth.py` | `.strip()` before `.lower()` on usernames | ✅ shipped (commit `60f3dba`) |
| L6 | `task_manager.py` | Document `COMPACT_PROMPT` | ✅ shipped (commit `60f3dba`) |
| L7 | `docs/CHANGELOG.md` | Auth refactor entry | ✅ shipped (commit `b8ee1af`) |

### 4.4 Test issues

| ID | Test | Status |
|---|---|---|
| F1 | `tests/test_task_manager.py::test_cancelling_waiting_input_task_releases_next_claude_task` | 🟡 intermittent; passed in latest full run; async-race suspected |
| F2 | `tests/transport/test_telegram_transport.py::test_enable_team_relay_lifecycle` | 🟡 intermittent; passed in latest full run |
| F3 | `tests/test_cli_transport.py::test_start_accepts_transport_web_flag` + `::test_start_default_transport_is_telegram` | ✅ closed — uses `tmp_path` |
| — | `tests/test_transport_lockout.py:37` — `Path(...).read_text()` encoding bug | ✅ closed (see F4 in §1.2) |

### 4.5 Post-audit operational fixes

| ID | Area | Resolution | Status |
|---|---|---|---|
| R1 | `bot.py` + `team_relay.py` | Partial-message relay short-circuit in `_on_stream_event` for group mode | ✅ commit `01f4645` |
| R2 | `team_relay.py` | `(sender, reply_to_msg_id)` coalesce buffer w/ 3s window for split messages | ✅ commit `01f4645` |
| R3 | `personas/software_manager.md` | Brevity guard (~3000 char cap on group messages) | ✅ commit `01f4645` |

---

## 5. Maintenance Fixes (Pending)

Small-scope plans, ready to implement.

| Plan | Scope | Status |
|---|---|---|
| [Fix CLI Telethon session permissions](superpowers/plans/2026-04-24-fix-cli-telethon-session-permissions.md) | Close perm race in `setup --phone`; mirror BotFatherClient pattern | ✅ shipped |
| [Fix Windows config M11 collection](superpowers/plans/2026-04-24-fix-windows-config-m11-collection.md) | Make M11 tests collect on Windows; preserve Unix root-skip | ✅ shipped |
| [Isolate OpenAI transcriber tests](superpowers/plans/2026-04-24-isolate-openai-transcriber-tests.md) | Tests pass without optional `openai` dep | ✅ shipped |
| [Update team-relay lifecycle test](superpowers/plans/2026-04-24-update-team-relay-lifecycle-test.md) | Match current TeamRelay contract (new + edited handlers) | ✅ shipped |
| Spec D′ — StringSession for team-bot relays | Manager exports `telethon.session` once and seeds subprocesses via `LP2C_TELETHON_SESSION_STRING`; eliminates the `database is locked` race on concurrent autostart (path-mode env var kept as fallback) | ✅ branch `fix/team-relay-string-session` |

---

## 6. Sandbox / Directory Jailing

[sandbox-plan.md](../sandbox-plan.md) — 📋 designed, not implemented.

Optionally restrict claude subprocess + `/run` to project directory. macOS Seatbelt (`sandbox-exec`) + Linux `bwrap`. Adds `ProjectConfig.jailed: bool = True`, `Config.projects_dir: str | None`, CLI flags `--jail/--no-jail`, manager wizard toggle.

Open questions:
1. Linux: use `landlock` package as fallback when `bwrap` absent?
2. `start --jail` runtime-only vs persisted? (rec: runtime-only)
3. `configure --projects-dir` create eagerly vs lazily? (rec: lazy)
4. `~/.claude/` writes — allow / block? (rec: allow reads, block writes)

---

## 7. Known Pending Issues (from where-are-we.md)

| Item | Location | Status |
|---|---|---|
| Browser username spoofing enables web command execution: client-controlled `username` + constant `browser_user` can satisfy allowlist auth and reach `/run` | `web/app.py:52-78`, `_auth.py:138-181`, `bot.py:955-968` | ✅ closed — server-issued session id, server-side handle binding, and CSRF |
| Streamed Web UI output is rendered as trusted HTML: `render_markdown()` passes model text through, then `messages.html` uses `|safe` | `web/transport.py:193-195`, `transport/streaming.py:218-226`, `web/templates/messages.html:4` | ✅ closed — template escapes stored text |
| Web transport drops `Buttons`, making picker-only workflows unusable (`/backend`, `/model`, `/effort`, `/permissions`, `/reset`, task controls, AskUserQuestion) | `web/transport.py:135-154`, `web/templates/messages.html` | ✅ closed — buttons persist, render, and dispatch through web route |
| Non-Telegram team `room` bindings are written but not loaded/saved/passed on restart, allowing wrong-room recapture | `config.py:594-621`, `config.py:790-799`, `cli.py:426-456` | ✅ closed — `RoomBinding` load/save/load_teams/startup path covered |
| Codex subprocess can be orphaned if `chat_stream()` exits before `turn.completed` because `_proc` is cleared before termination/reap | `backends/codex.py:214-218` | ✅ closed — terminate/reap before clearing `_proc`, with early-generator-close regression |
| `_proc` is single slot — concurrent Claude tasks could overwrite | `backends/claude.py:158` (was `claude_client.py`) | ✅ closed — `TaskManager` serializes agent turns per backend slot |
| Manager bot `/add_project` wizard allows skipping token (inconsistent with CLI) | `manager/bot.py` | ✅ closed |
| ~~`livestream.LiveMessage` dead code~~ | ~~`livestream.py`~~ | ✅ removed (file no longer exists; project bot uses `transport/streaming.py`) |
| `WebTransport.stop()` doesn't fully release uvicorn listener; tests hardcode ports → `[Errno 98]` flakes when running suite end-to-end | `web/transport.py:stop` | ✅ closed |
| `tests/test_cli_transport.py` depends on `/tmp/x` existing (Click `Path(exists=True)` validation) | `tests/test_cli_transport.py:21,42` | ✅ closed |
| No end-to-end test wires `ProjectBot` + `WebTransport` + `_auth_identity` + handler in one flow | new test | ✅ closed (`tests/web/test_projectbot_web_e2e.py`) |

### 7.1 Direct code audit findings — 2026-04-27

These were found by reading current code paths directly, not by reusing the documented backlog or existing tests.

| ID | Severity | Item | Location | Status |
|---|---|---|---|---|
| CA-1 | 🔴 Critical | Web UI still has no real user authentication. The old client-controlled username spoofing path is closed, but `ProjectBot.build()` maps every Web browser session to the single configured allowed username via `authenticated_handle`; any browser that can load the UI receives cookies/CSRF and reaches the normal authorizer as that user. | `bot.py:2529-2537`, `web/app.py:66-126`, `web/transport.py:297-307`, `_auth.py:138-181` | ✅ closed — ProjectBot-backed Web transports now generate a per-process auth token and `create_app(..., auth_token=...)` requires that token before serving chat pages, partials, SSE, message posts, or button posts. Visiting `/chat/default?token=<token>` sets an HTTP-only Web auth cookie for subsequent requests. The non-loopback CRITICAL log remains as deploy-time defense. Regressions: `tests/web/test_app_smoke.py::test_chat_page_requires_web_auth_token_when_configured`, `tests/web/test_web_transport.py::test_web_transport_warns_critically_on_non_loopback_bind`. |
| CA-2 | 🟠 Important | Rejected Web uploads leak temp files and can consume memory/disk. `post_message()` reads the whole upload into memory and writes a `lp2c-web-*` tempdir before auth dispatch; if `_dispatch_event()` rejects the identity, it returns before the upload cleanup block. | `web/app.py:90-126`, `web/transport.py:296-307`, `web/transport.py:364-391` | ✅ closed — `web/app.py` introduces `MAX_UPLOAD_BYTES = 25 MB`, streams the body to disk in 64 KB chunks, raises `HTTPException(413)` once the cap is exceeded, and removes partial tempdirs on upload failure. `WebTransport._dispatch_event` now also cleans queued payload files on auth rejection, command dispatch, unknown-event exits, and handler failures. Regressions: `tests/web/test_web_upload.py::test_upload_rejects_oversized_payload_with_413`, `::test_upload_cleanup_on_oversized_rejection`, and `tests/web/test_web_transport.py::test_rejected_message_cleans_payload_files`. |
| CA-3 | 🟠 Important | `/run` concurrency cap is racy. Each command checks `len(_active_run_pids)` before awaiting `_on_task_started`, and the PID is recorded only after `Popen`; concurrent `/run` tasks can all pass the check and exceed the max-3 limit. | `task_manager.py:430-459` | ✅ closed — `_exec_command` now reserves a slot atomically (negative placeholder PID) BEFORE any await, swaps the placeholder for the real PID after `Popen`, and releases either id in a `finally`. Regression: `tests/test_task_manager.py::test_run_concurrency_cap_is_atomic_under_race` (schedules 2× the cap concurrently and asserts only `_MAX_CONCURRENT_RUNS` reach RUNNING). |
| CA-4 | 🟠 Important | Codex early cancellation can leave child processes alive. Codex is launched in a new session/process group, but the early-close cleanup path calls only `proc.kill()` instead of the shared process-tree terminator. | `backends/codex.py:141-157`, `backends/codex.py:241-244` | ✅ closed — `chat_stream`'s early-close `finally` now lazy-imports `task_manager._terminate_process_tree` and runs it via `asyncio.to_thread` so the whole process group dies and the function still wraps signalling+`proc.wait` synchronously. Regression: `tests/backends/test_codex_backend.py::test_chat_stream_early_close_uses_process_tree_terminator`. |
| CA-5 | 🟡 Minor | Web dispatch failures are swallowed silently. `_dispatch_loop()` catches every handler exception and drops it without logging or notifying the user, making broken commands/buttons look like no-ops. | `web/transport.py:284-291` | ✅ closed — `_dispatch_loop` now calls `logger.exception("Web dispatch failed: %r", event)` instead of `pass`. Regression: `tests/web/test_web_transport.py::test_dispatch_loop_logs_handler_exceptions` (drives a raising handler through the real queue → loop path and asserts the log line). |

---

## 8. Post-v1.0.0 Hardening Sweep (`dev` branch)

Branch: `dev` (HEAD `d68d5c4`, 16 commits ahead of `main` after the `b86370c` plugin-system merge). Emergent integration fixes that surfaced post-merge during multi-process operation and Windows runs — no formal spec/plan, captured here for tracking. Not yet merged to `main`. Each commit ships its own regression tests; full `pytest -q` not re-run on `dev` end-to-end since the v1.0.0 baseline (see header).

#### Plugin loader robustness

| Commit | Item | Status |
|---|---|---|
| `df2bbf6` | `load_plugin` wraps `ep.load()` and constructor calls in try/except so one broken plugin doesn't take down bot startup. Returns `None` on failure; existing callers already handle `None`. Partial coverage of the v1.0.1 follow-up — the plugin-`start()`-failure inert-command case is still open. | ✅ closed |

#### Manager process lifecycle — pidfile + orphan adoption

Pre-fix: a manager crash left subprocesses orphaned; the next manager start spawned duplicates polling the same Telegram token (`Conflict: terminated by other getUpdates request`).

| Commit | Item | Status |
|---|---|---|
| `5abb951` | `ProcessManager` writes per-project pidfiles under `<config_dir>/run/<name>.pid`; `reap_orphans()` adopts surviving processes before the autostart spawn. `cli.start_manager` calls the reap. | ✅ closed |
| `2217323` | `_capture_output()` deletes the pidfile on observed child exit (clean or non-zero) so the same manager doesn't see a phantom pidfile and refuse to spawn / adopt a stranger. | ✅ closed |
| `c4aefa0` | `start_team()` writes a pidfile too — team:NAME:ROLE entries get the same orphan adoption as project bots. | ✅ closed |
| `7b20e05` | Pidfile filenames URL-encode `team:NAME:ROLE` keys so NTFS doesn't reject the reserved `:`. Adds `list_running()` / `restart()` helpers consumed by the user-mutation track below. | ✅ closed |
| `face82f` | Fake `ProcessManager` fixtures get a no-op `reap_orphans()` stub to match the contract. | ✅ closed |

#### Manager user-mutation restart

Pre-fix: running bots cached `_allowed_users` at startup; mutating commands persisted to disk but left the live process serving stale auth.

| Commit | Item | Status |
|---|---|---|
| `5f00a82` | Plugin toggle stops+starts the running project bot — disabled plugins actually stop accepting commands. Does NOT auto-start a deliberately-stopped bot. | ✅ closed |
| `7320f3e` | `/add_user`, `/remove_user`, `/promote_user`, `/demote_user`, `/reset_user_identity` call `_restart_running_bots_for_user_mutation` after persistence. Covers role changes + identity resets the Web revocation-check missed. | ✅ closed |
| `56fed9a` | Restart helper switches from `list_all()`+`start()` (project-only) to `list_running()`+`restart()` so mutations land on running `team:NAME:ROLE` bots too. | ✅ closed |

#### Web auth hardening

| Commit | Item | Status |
|---|---|---|
| `fa921e0` | Drops query-string token acceptance on chat / messages / SSE / message-POST / button-POST routes. New `GET /auth?token=&next=` bootstrap validates and sets the cookie; `_safe_local_redirect` blocks protocol-relative + off-host `next`. | ✅ closed |
| `11f8698` | `revocation_check` callable threaded through `create_app` / `WebTransport` / `ProjectBot.build`. Per-request + per-SSE-iteration live-config check so manager-side revocation no longer waits for a project bot restart; fails closed on config read error. | ✅ closed |
| `a6e51b7` | `GET /auth` renders a sign-in form (no cookie); `POST /auth` is the only path that sets the cookie. Closes residual token-in-URL leak via bookmarked URLs / Referer / proxy logs. Startup banner prints URL and token as separate fields. | ✅ closed |

#### Backend env policy

| Commit | Item | Status |
|---|---|---|
| `cb4e967` | `_prepare_env()` flips from `*_TOKEN` / `*_KEY` / `AWS_*` blacklist to allowlist baseline (PATH, HOME, locale, XDG, SSL, proxy, Node/Python runtime). Custom secrets like `PGPASSWORD` / `OPENID_CLIENT_SECRET` / `INTERNAL_STAGING_URL` no longer leak to the agent CLI subprocess. | ✅ closed |
| `6d3d6c1` | Windows process/profile vars (`APPDATA`, `LOCALAPPDATA`, `USERPROFILE`, `HOMEDRIVE`, `HOMEPATH`, `SystemRoot`, `WINDIR`, `ComSpec`, `PATHEXT`, `PROGRAMDATA`, `ProgramFiles*`, `CommonProgramFiles*`) added to baseline. Native Node-packaged CLIs (the `claude` binary) crash before stderr without these; no-op on POSIX. | ✅ closed |

#### Config web-identity repair + Telegram startup resilience

| Commit | Item | Status |
|---|---|---|
| `27241c5` | `config._repair_locked_identity()` rewrites `telegram:<web-native-id>` → `web:<web-native-id>` on load; `_migrate_legacy_auth` now also catches bare `browser_user` (was `web-session:` only). `bot._after_ready` int-validates Telegram native_ids before constructing `ChatRef` (defends against pre-repair configs), and `_is_expected_startup_delivery_failure` downgrades "chat not found" / "bot was blocked" / "bot can't initiate conversation" / "bot can't send messages to bots" / "forbidden" to warnings with the operator hint that a new Telegram bot must be opened and `/start`-ed before it can DM. | ✅ closed |

#### CLI ASCII safety (Windows console)

| Commit | Item | Status |
|---|---|---|
| `d68d5c4` | Em-dashes in CLI help/error strings replaced with ASCII hyphens; parametrized canary test asserts `--help`, `configure --help`, `migrate-config --help` output is ASCII-encodable. Fixes `UnicodeEncodeError` on default Windows console codepage (cp1252 / cp437) on a fresh install before any configuration. | ✅ closed |

#### Config cleanup — drop legacy save-side mirror

Pre-v1.0 dual-wrote `model` / `effort` / `permissions` / `session_id` / `show_thinking` at the project + team-bot entry top level alongside `backend_state["claude"]`, and `default_model` alongside `default_model_claude`, for downgrade safety. v1.0.0 shipped — the save-side mirror is now dropped so on-disk configs carry only the canonical nested shape. Load-side `_legacy_backend_state` stays one more release (marked for v1.1 removal) so any pre-v1.0 config that still hasn't been touched still migrates cleanly on first load.

| Commit | Item | Status |
|---|---|---|
| _(this commit)_ | Replace `_mirror_legacy_claude_fields` with `_strip_legacy_claude_fields`; loader pulls Claude-shaped fields from `backend_state["claude"]` first then falls back to top-level; CLI `projects add` and manager `_add_model` stop writing the legacy mirror; `save_config` always strips legacy top-level keys (idempotent — existing-on-disk duplicates clear on next save). | ✅ closed |

#### Notes

- The v1.0.1 follow-up list in §1.4 is orthogonal to this sweep and remains open.
- Untracked `link-project-to-chat/` directory in the repo root (manager run state, written by current code) is missing from `.gitignore` — either gitignore-add or relocate manager state under user config dir.
- Three of the 16 sweep commits (`6d3d6c1`, `27241c5`, `d68d5c4`) are Windows-compatibility fixes landed 2026-05-16. The remaining 13 landed earlier on the same `dev` branch and address cross-platform process/auth issues.

---

## Summary by Status

| Status | Count |
|---|---|
| ✅ Shipped | 7 transport specs (#0/#0a/#0b/#0c/#1/#4) + Backend Phases 1–4 + 6 earlier features + security/quality audit fixes + Phase 4 post-completion hardening + Web UI security/buttons + non-Telegram room binding restart path + **Plugin system port + `AllowedUser` auth model rewrite (v1.0.0)** + **Post-v1.0.0 integration hardening on `dev` (16 commits, see §8) — not yet merged to `main`** + **Google Chat transport v1** |
| 🟡 Partial / intermittent | 2 intermittent flaky tests (F1, F2 in §4.4) |
| 📋 Designed, not started | 2 transport specs (Discord #2, Slack #3), Backend Phase 5 (Gemini adapter), sandbox |
| ⏳ Small pending fixes | 1 deferred follow-up (A3 — future Conversation primitive spec). 6 non-blocking follow-ups from v1.0.0 final review queued for v1.0.1 (§1.4). Direct code-audit findings CA-1..CA-5 are closed with regression coverage. `dev` sweep (§8) awaiting merge to `main` + end-to-end `pytest -q` re-verification; untracked `link-project-to-chat/` run-state dir needs gitignore-add or relocation. |

---

## Source Documents

**Specs:** [docs/superpowers/specs/](superpowers/specs/) (18 design docs, incl. plugin system port)
**Plans:** [docs/superpowers/plans/](superpowers/plans/) (24 implementation plans, incl. plugin system port)
**Audit:** [issues-2026-04-22.md](issues-2026-04-22.md) · [2026-04-22-remediation-plan.md](2026-04-22-remediation-plan.md) · [review-2026-04-22-batch1.md](review-2026-04-22-batch1.md)
**Follow-ups:** [2026-04-25-spec0-followups.md](2026-04-25-spec0-followups.md)
**State:** [where-are-we.md](../where-are-we.md) · [CHANGELOG.md](CHANGELOG.md)
**Sandbox:** [sandbox-plan.md](../sandbox-plan.md)
