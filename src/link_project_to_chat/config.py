from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl

logger = logging.getLogger(__name__)

DEFAULT_CONFIG = Path.home() / ".link-project-to-chat" / "config.json"

DEFAULT_META_DIR = DEFAULT_CONFIG.parent / "meta"


def resolve_project_meta_dir(meta_dir: Path, project_name: str) -> Path:
    """Return <meta_dir>/<project_name>/, creating parents idempotently.

    Used by `bot._init_plugins` to give each project its own storage root
    under the operator-chosen meta_dir.
    """
    path = meta_dir / project_name
    path.mkdir(parents=True, exist_ok=True)
    return path


class ConfigError(Exception):
    """Raised when config.json contains structurally invalid data."""


_VALID_ROLES = ("viewer", "executor")

# Canonical truthy/falsy literals for user-supplied bool fields (CLI flags,
# manager-bot edit inputs, config-string overrides). Kept here so CLI and
# manager paths share a single vocabulary — avoids drift if we ever extend
# the accepted set.
_USER_BOOL_TRUE = frozenset({"1", "true", "yes", "on"})
_USER_BOOL_FALSE = frozenset({"0", "false", "no", "off"})


def parse_user_bool(value: str) -> bool | None:
    """Parse a user-supplied bool string. Returns None for unrecognized input.

    Accepts (case-insensitive, stripped): true/false, yes/no, on/off, 1/0.
    Callers decide how to report unrecognized input — this function never
    raises, so it works in both ``raise SystemExit`` (CLI) and
    ``send_text(...)`` (manager-bot) contexts.
    """
    if not isinstance(value, str):
        return None
    lowered = value.strip().lower()
    if lowered in _USER_BOOL_TRUE:
        return True
    if lowered in _USER_BOOL_FALSE:
        return False
    return None


def _is_web_native_id(native_id: str) -> bool:
    return (
        native_id == "browser_user"
        or native_id.startswith("web-session:")
        or native_id.startswith("web-user:")
    )


def _repair_locked_identity(identity_key: str) -> str:
    if identity_key.startswith("telegram:"):
        native_id = identity_key[len("telegram:"):]
        if _is_web_native_id(native_id):
            return f"web:{native_id}"
    return identity_key


@dataclass
class AllowedUser:
    username: str
    role: str = "viewer"
    locked_identities: list[str] = field(default_factory=list)
    # Platform-portable identity locks: list of "transport_id:native_id" strings.
    # Each transport a user contacts from gets a new entry appended on first
    # contact (no replacement). Auth succeeds if any entry matches the
    # current identity. Examples after first contact:
    #   ["telegram:12345"]
    #   ["web:web-session:abc-def"]
    #   ["telegram:12345", "web:web-session:abc-def"]  (same user, two transports)
    # Replaces the int-only ID locking from the legacy design.


def _parse_allowed_users(raw_list) -> list["AllowedUser"]:
    out: list[AllowedUser] = []
    if not isinstance(raw_list, list):
        return out
    for entry in raw_list:
        if not isinstance(entry, dict):
            logger.warning("malformed allowed_users entry (not a dict): %r", entry)
            continue
        username = entry.get("username")
        if not username:
            logger.warning("malformed allowed_users entry (missing username): %r", entry)
            continue
        role = entry.get("role", "viewer")
        if role not in _VALID_ROLES:
            logger.warning("unknown role %r for %s; defaulting to viewer", role, username)
            role = "viewer"
        # Accept the new list shape; tolerate the single-string shape used
        # during the migration window (auto-wrap).
        raw_locked = entry.get("locked_identities", [])
        if isinstance(raw_locked, str):
            raw_locked = [raw_locked]
        if not isinstance(raw_locked, list):
            logger.warning(
                "malformed locked_identities for %s (expected list of strings): %r; dropping",
                username, raw_locked,
            )
            raw_locked = []
        locked_identities = [
            _repair_locked_identity(s) for s in raw_locked if isinstance(s, str)
        ]
        if len(locked_identities) != len(raw_locked):
            logger.warning(
                "dropped non-string entries from locked_identities for %s", username,
            )
        out.append(AllowedUser(
            username=str(username).lstrip("@").lower(),
            role=role,
            locked_identities=locked_identities,
        ))
    return out


def _serialize_allowed_users(users: list["AllowedUser"]) -> list[dict]:
    out = []
    for u in users:
        entry: dict = {"username": u.username, "role": u.role}
        if u.locked_identities:
            entry["locked_identities"] = list(u.locked_identities)
        out.append(entry)
    return out


def _parse_plugins(raw_list) -> list[dict]:
    out: list[dict] = []
    if not isinstance(raw_list, list):
        return out
    for entry in raw_list:
        if not isinstance(entry, dict):
            continue
        if not entry.get("name"):
            continue
        out.append(entry)
    return out


def _migrate_legacy_auth(raw: dict) -> tuple[list["AllowedUser"], bool]:
    """One-way migration from legacy fields -> AllowedUser list.

    Reads `allowed_usernames` (list[str]), `trusted_users` (dict[str, int|str]
    OR legacy list[str]), and `trusted_user_ids` (list[int], legacy-only) from
    `raw` and synthesizes `AllowedUser{role="executor"}` entries.

    Returns (allowed_users, migrated) where `migrated` is True iff any legacy
    field was present in `raw`. The caller uses `migrated` to set
    `migration_pending` on the Config so the CLI saves on first start.

    Username normalization: lowercase, strip leading `@`.
    Locked ID source order:
      1. `trusted_users` dict - explicit username -> user_id mapping (current shape).
      2. `trusted_users` list (pre-A1) aligned with `trusted_user_ids` by index.
      3. `trusted_user_ids` aligned with `allowed_usernames` by index (oldest shape).
    On mismatched lengths in the list paths, IDs are dropped and the entries
    re-lock on next contact (logged at WARNING).
    """
    # Accept both the list-shaped (``allowed_usernames``) and the singular
    # (``allowed_username``) keys, plus the per-project ``username`` key,
    # so older configs migrate cleanly. Similarly for ``trusted_user_id``.
    legacy_unames = list(raw.get("allowed_usernames") or [])
    if not legacy_unames:
        singular_uname = raw.get("allowed_username") or raw.get("username")
        if singular_uname:
            legacy_unames = [singular_uname]
    raw_trusted = raw.get("trusted_users")
    legacy_ids = list(raw.get("trusted_user_ids") or [])
    if not legacy_ids:
        singular_tid = raw.get("trusted_user_id")
        if singular_tid is not None:
            try:
                legacy_ids = [int(singular_tid)]
            except (TypeError, ValueError):
                pass
    if not (legacy_unames or raw_trusted or legacy_ids):
        return [], False

    def _norm(name) -> str:
        return str(name).lstrip("@").lower()

    # Build a username -> locked_identities map ("telegram:<id>" strings).
    # Legacy fields predate multi-transport support, so every legacy ID belongs
    # to Telegram; we prefix with "telegram:" so each entry in locked_identities is
    # immediately usable by the new identity-keyed auth comparison.
    # Returns lists (not single strings) so the migration is shape-compatible
    # with the new `locked_identities` field.
    identities_for: dict[str, list[str]] = {}
    legacy_trusted_names: list[str] = []
    # Only values that already start with a KNOWN transport prefix are passed
    # through; everything else is assumed Telegram (legacy default). This is
    # the fix for the Web case: pre-v1.0 Web bound trusted_users["alice"] =
    # "web-session:abc" - contains ":" but does NOT have the "web:" transport
    # prefix that auth comparisons need. We detect this by matching a known
    # transport whitelist; anything else falls back to telegram or to bare
    # passthrough only if a known prefix is present.
    _KNOWN_TRANSPORT_PREFIXES = ("telegram:", "web:", "discord:", "slack:")

    def _normalize_legacy_trust_id(uid_str: str) -> str:
        """Turn a legacy trusted_users value into a 'transport_id:native_id' string."""
        # Already correctly prefixed?
        for prefix in _KNOWN_TRANSPORT_PREFIXES:
            if uid_str.startswith(prefix):
                return uid_str
        # The Web cases: bare Web native ids -> "web:<native_id>".
        if _is_web_native_id(uid_str):
            return f"web:{uid_str}"
        # Plain numeric or arbitrary string -> telegram (legacy default).
        try:
            return f"telegram:{int(uid_str)}"
        except (TypeError, ValueError):
            return f"telegram:{uid_str}"

    # SHAPE DISCRIMINATOR - isinstance() dispatches the three on-disk
    # trusted_users shapes (see spec section "Migration semantics" and
    # Step 2's golden-file tests (b)/(c) [dict shape] and (d) [list shape]).
    # Without this branch, dict-shape configs would silently fall into the
    # list branch and crash on `for name, uid in zip(...)` with a TypeError.
    if isinstance(raw_trusted, dict):
        # Current on-disk shape: username -> user_id (int or str).
        # Covered by tests (b) test_migration_b_trusted_users_dict_subset
        # and (c) test_migration_c_trusted_users_dict_full.
        for uname, uid in raw_trusted.items():
            norm = _norm(uname)
            legacy_trusted_names.append(norm)
            if uid is None:
                continue
            identities_for.setdefault(norm, []).append(_normalize_legacy_trust_id(str(uid)))
    elif isinstance(raw_trusted, list):
        # Pre-A1 shape: list of usernames aligned with trusted_user_ids by index.
        # Covered by test (d) test_migration_d_legacy_list_with_ids_aligned.
        legacy_trusted_names = [_norm(n) for n in raw_trusted]
        if len(legacy_trusted_names) == len(legacy_ids):
            for name, uid in zip(legacy_trusted_names, legacy_ids):
                identities_for.setdefault(name, []).append(f"telegram:{int(uid)}")
        elif legacy_ids:
            logger.warning(
                "legacy trusted_users(list) / trusted_user_ids length mismatch "
                "(%d vs %d); dropping IDs - affected users will re-lock on next contact",
                len(legacy_trusted_names), len(legacy_ids),
            )
        else:
            # list trusted_users without ids; emit WARNING for length mismatch
            # so the test_legacy_list_length_mismatch_drops_ids case is covered.
            logger.warning(
                "legacy trusted_users(list) / trusted_user_ids length mismatch "
                "(%d vs %d); dropping IDs - affected users will re-lock on next contact",
                len(legacy_trusted_names), len(legacy_ids),
            )
    elif legacy_ids and legacy_unames:
        # Oldest shape: trusted_user_ids aligned with allowed_usernames by index.
        norm_allowed = [_norm(n) for n in legacy_unames]
        if len(norm_allowed) == len(legacy_ids):
            for name, uid in zip(norm_allowed, legacy_ids):
                identities_for.setdefault(name, []).append(f"telegram:{int(uid)}")
        else:
            logger.warning(
                "legacy allowed_usernames / trusted_user_ids length mismatch "
                "(%d vs %d); dropping IDs", len(norm_allowed), len(legacy_ids),
            )

    # Union of allowed_usernames + any trusted-only usernames (orphan trust);
    # all become executor.
    seen: set[str] = set()
    out: list[AllowedUser] = []
    for raw_name in list(legacy_unames) + list(legacy_trusted_names):
        norm = _norm(raw_name)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        out.append(AllowedUser(
            username=norm,
            role="executor",
            locked_identities=list(identities_for.get(norm, [])),
        ))
    locked_count = sum(1 for u in out if u.locked_identities)
    logger.info(
        "migrated legacy auth fields -> %d AllowedUser entries (%d with locked identities)",
        len(out), locked_count,
    )
    return out, True


def resolve_project_allowed_users(project, config) -> tuple[list["AllowedUser"], str]:
    """Project allow-list with global fallback. Returns (users, source).

    Source is "project" when the project's own allow-list is non-empty,
    "global" when falling back to Config.allowed_users. The bot uses the
    source to write back to the matching scope when persisting first-contact
    locks via `_persist_auth_if_dirty`.

    Matches the precedence of the existing `resolve_project_auth_scope`
    (project overrides global, falls back to global) so deployments where the
    project list is empty don't suddenly fail-closed when only the global
    list is populated.

    Callers in bot.py / cli.py / manager/bot.py use this helper instead of
    reading project.allowed_usernames / project.trusted_users directly.
    """
    if project.allowed_users:
        return project.allowed_users, "project"
    return config.allowed_users, "global"


# Removed in Task 5 Step 12:
#   - ``_synthesize_allowed_users_from_legacy`` (legacy-field synthesis)
#   - ``_union_save_view`` (save-time merge of legacy and new shapes)
#   - ``_mirror_allowed_users_to_legacy`` (load-time mirror to legacy fields)
# All three existed to bridge the legacy ``allowed_usernames`` /
# ``trusted_users`` / ``trusted_user_ids`` fields with the new
# ``allowed_users`` list shape. With the legacy fields gone from the
# dataclasses, these helpers are dead code.


@dataclass(frozen=True)
class BotPeerRef:
    transport_id: str
    native_id: str
    handle: str | None = None
    display_name: str = ""


@dataclass(frozen=True)
class RoomBinding:
    transport_id: str
    native_id: str


@dataclass
class GoogleChatConfig:
    service_account_file: str = ""
    app_id: str = ""
    project_number: str = ""
    auth_audience_type: str = "endpoint_url"
    allowed_audiences: list[str] = field(default_factory=list)
    endpoint_path: str = "/google-chat/events"
    public_url: str = ""
    host: str = "127.0.0.1"
    port: int = 8090
    root_command_name: str = "lp2c"
    root_command_id: int | None = None
    callback_token_ttl_seconds: int = 900
    pending_prompt_ttl_seconds: int = 900
    max_message_bytes: int = 32_000
    attachment_max_bytes: int = 25_000_000


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


@dataclass
class ProjectConfig:
    path: str
    telegram_bot_token: str
    model: str | None = None
    effort: str | None = None
    permissions: str | None = None  # one of PERMISSION_MODES or "dangerously-skip-permissions"
    session_id: str | None = None
    autostart: bool = False
    active_persona: str | None = None
    show_thinking: bool = False
    backend: str = "claude"
    backend_state: dict[str, dict] = field(default_factory=dict)
    # Per-chat conversation history (cross-backend). Bot-level — independent
    # of the active backend, so swapping ``/backend codex`` keeps the same
    # log visible to the next prompt.
    context_enabled: bool = True
    context_history_limit: int = 10
    # New (Task 3) - plugin system + identity-keyed auth. Legacy fields above
    # stay through Task 4 as transitional read-only inputs.
    allowed_users: list[AllowedUser] = field(default_factory=list)
    plugins: list[dict] = field(default_factory=list)
    respond_in_groups: bool = False
    # When True, the project bot responds in Telegram groups to messages that
    # @mention `@<bot_username>` OR reply to a prior bot message. All other
    # group messages are silently ignored. Default False (pre-v1.1.0 behavior:
    # DM-only). Independent of team mode: a team bot's group routing is
    # governed by team_name + role, not this flag. The PTB filter is set
    # once at startup, so toggling this field requires a bot restart.
    safety_prompt: str | None = None
    # None  → use DEFAULT_SAFETY_SYSTEM_PROMPT (safety on, default text)
    # ""    → safety off (explicit operator decision)
    # "..." → custom safety text replaces the default
    # Resolved into backend.safety_system_prompt once at ProjectBot.__init__
    # via _resolve_safety_prompt(). Each backend renders the field in its
    # native style (Claude: --append-system-prompt parts list;
    # Codex: <system-reminder> block).


@dataclass
class TeamBotConfig:
    telegram_bot_token: str
    active_persona: str | None = None
    autostart: bool = False
    # None means "use the team default" — resolved at CLI startup to
    # "dangerously-skip-permissions" so team bots don't block on tool prompts.
    permissions: str | None = None
    # The bot's @username, captured at /create_team time (or backfilled via
    # getMe on first startup). Used so each team bot knows its peer's @handle.
    bot_username: str = ""
    session_id: str | None = None
    model: str | None = None
    effort: str | None = None
    show_thinking: bool = False
    bot_peer: BotPeerRef | None = None
    backend: str = "claude"
    backend_state: dict[str, dict] = field(default_factory=dict)
    # Per-chat conversation history (see ProjectConfig).
    context_enabled: bool = True
    context_history_limit: int = 10


@dataclass
class TeamConfig:
    path: str
    group_chat_id: int = 0  # 0 = sentinel "not yet captured"
    bots: dict[str, TeamBotConfig] = field(default_factory=dict)
    room: RoomBinding | None = None
    max_autonomous_turns: int = 5
    safety_mode: str = "strict"


@dataclass
class Config:
    github_pat: str = ""
    telegram_api_id: int = 0
    telegram_api_hash: str = ""
    manager_telegram_bot_token: str = ""
    stt_backend: str = ""            # "whisper-api" or "whisper-cli" or "" (disabled)
    openai_api_key: str = ""
    whisper_model: str = "whisper-1" # OpenAI model name or local whisper.cpp model size
    whisper_language: str = ""       # ISO 639-1 code (e.g. "en", "ka"), empty = auto-detect
    tts_backend: str = ""            # "openai" or "" (disabled)
    tts_model: str = "tts-1"        # OpenAI TTS model
    tts_voice: str = "alloy"        # OpenAI TTS voice
    default_model: str = ""          # legacy load-side fallback only; never written on save (v1.1 removal candidate)
    default_backend: str = "claude"
    default_model_claude: str = ""
    projects: dict[str, ProjectConfig] = field(default_factory=dict)
    teams: dict[str, TeamConfig] = field(default_factory=dict)
    # New (Task 3) - identity-keyed auth at the global scope.
    allowed_users: list[AllowedUser] = field(default_factory=list)
    # Operator-chosen storage root for per-bot/per-plugin persistent state.
    # Defaults to ``~/.link-project-to-chat/meta`` (computed at import time).
    # Each project gets a ``<meta_dir>/<project_name>/`` subdir resolved via
    # ``resolve_project_meta_dir``; plugins receive it as
    # ``PluginContext.data_dir``. Operators set ``meta_dir`` in config.json
    # to relocate storage to a different volume (e.g. ``/var/lib/lptc/data``).
    meta_dir: Path = field(default_factory=lambda: DEFAULT_META_DIR)
    # Google Chat transport configuration. Omitted from saved JSON when equal
    # to the default (all fields at their zero/default values).
    google_chat: GoogleChatConfig = field(default_factory=GoogleChatConfig)
    # Runtime flag set by load_config when legacy auth fields were read; CLI
    # start uses it to force a save before serving traffic. ``save_config``
    # does not emit this key; ``repr=False, compare=False`` keeps it out of
    # debug output and equality.
    migration_pending: bool = field(default=False, repr=False, compare=False)


def _lock_file(lock_handle) -> None:
    if os.name == "nt":
        lock_handle.seek(0, os.SEEK_END)
        if lock_handle.tell() == 0:
            lock_handle.write(b"0")
            lock_handle.flush()
        while True:
            try:
                lock_handle.seek(0)
                msvcrt.locking(lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                time.sleep(0.05)
    else:
        fcntl.flock(lock_handle, fcntl.LOCK_EX)


def _unlock_file(lock_handle) -> None:
    if os.name == "nt":
        lock_handle.seek(0)
        msvcrt.locking(lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(lock_handle, fcntl.LOCK_UN)


@contextmanager
def _config_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Predictable by design: the lock sits beside config.json so every process
    # coordinating on the same config path contends on the same file. The
    # config directory is chmod 0700 on POSIX before writes.
    lock = path.with_suffix(".lock")
    with open(lock, "a+b") as lf:
        _lock_file(lf)
        try:
            yield
        finally:
            _unlock_file(lf)


# ``resolve_project_auth_scope`` was removed in Task 5 Step 12 — callers
# now resolve auth via ``resolve_project_allowed_users`` (which returns
# ``(list[AllowedUser], "project"|"global")``).


def _normalize_username(username: str) -> str:
    return username.lower().lstrip("@")


def _load_permissions(proj: dict) -> str | None:
    """Read permissions from a project dict, with backward compat for old keys.

    Source-of-truth order: ``backend_state["claude"]["permissions"]`` first
    (canonical post-v1.0 shape), then the legacy top-level ``permissions``
    key, then the pre-v1.0 ``dangerously_skip_permissions`` flag, then the
    even-older ``permission_mode`` field.
    """
    claude_state = (proj.get("backend_state") or {}).get("claude") or {}
    if "permissions" in claude_state and claude_state["permissions"] is not None:
        return claude_state["permissions"] or None
    if "permissions" in proj:
        return proj["permissions"] or None
    if proj.get("dangerously_skip_permissions"):
        return "dangerously-skip-permissions"
    return proj.get("permission_mode") or None


def _load_claude_field(proj: dict, key: str, default=None):
    """Read a Claude-shaped legacy field from a project / team-bot entry.

    Source-of-truth order: ``backend_state["claude"][key]`` first (canonical
    post-v1.0 shape), then the legacy top-level ``key`` (pre-v1.0 mirror).
    Used to populate the dataclass mirror fields (model, effort, session_id,
    show_thinking) on load so a save->load roundtrip preserves them.
    """
    claude_state = (proj.get("backend_state") or {}).get("claude") or {}
    if key in claude_state and claude_state[key] is not None:
        return claude_state[key]
    return proj.get(key, default)


# TODO v1.1: remove this helper and its callers. It exists to fold pre-v1.0
# on-disk configs (top-level model / effort / permissions / session_id /
# show_thinking) into backend_state["claude"] on load. v1.0.0 stopped
# writing the legacy mirror, so by v1.1 every on-disk config has already
# been rewritten in the new shape.
def _legacy_backend_state(
    model: str | None,
    effort: str | None,
    permissions: str | None,
    session_id: str | None,
    show_thinking: bool,
) -> dict[str, dict]:
    """Fold legacy Claude-shaped flat fields into a backend_state map."""
    state: dict[str, dict] = {}
    claude: dict[str, object] = {}
    if model is not None:
        claude["model"] = model
    if effort is not None:
        claude["effort"] = effort
    if permissions is not None:
        claude["permissions"] = permissions
    if session_id is not None:
        claude["session_id"] = session_id
    if show_thinking:
        claude["show_thinking"] = True
    if claude:
        state["claude"] = claude
    return state


_LEGACY_CLAUDE_KEYS = ("model", "effort", "permissions", "session_id", "show_thinking")


def _strip_legacy_claude_fields(target: dict) -> None:
    """Remove the legacy top-level Claude-shaped flat keys from *target*.

    Phase 2 of the backend abstraction dual-wrote these keys at the project /
    team-bot entry top level alongside ``backend_state["claude"]`` as a
    one-release downgrade-safety mirror. v1.0.0 shipped — the mirror is now
    dropped on save. Existing on-disk duplicates are stripped on first save
    after upgrade.

    The load-side ``_legacy_backend_state`` helper still folds these keys into
    ``backend_state["claude"]`` for one more release so pre-v1.0 configs
    upgrade cleanly; it's marked for v1.1 removal.
    """
    for key in _LEGACY_CLAUDE_KEYS:
        target.pop(key, None)


def _effective_backend_state(
    backend_state: dict[str, dict],
    *,
    model: str | None,
    effort: str | None,
    permissions: str | None,
    session_id: str | None,
    show_thinking: bool,
) -> dict[str, dict]:
    """Resolve the canonical backend_state for save, folding in legacy fields.

    ``backend_state`` is the source of truth. When it is empty (e.g. a freshly
    constructed dataclass that only set legacy attributes), fall back to
    folding the legacy fields into ``backend_state["claude"]`` so save still
    persists them in the new shape. In particular, ``ProjectConfig(model=...)``
    constructed by tests or by callers that haven't migrated yet should still
    round-trip correctly through save.
    """
    if backend_state:
        return backend_state
    return _legacy_backend_state(model, effort, permissions, session_id, show_thinking)


def resolve_permissions(permissions: str | None) -> tuple[bool, str | None]:
    """Convert permissions string to (skip_permissions, permission_mode) for CLI/bot use."""
    if permissions == "dangerously-skip-permissions":
        return True, None
    if permissions and permissions != "default":
        return False, permissions
    return False, None


def resolve_start_model(
    backend_name: str,
    *,
    explicit_model: str | None = None,
    backend_model: str | None = None,
    legacy_claude_model: str | None = None,
    default_model_claude: str | None = None,
    default_model: str | None = None,
) -> str | None:
    """Resolve the model passed to a starting bot.

    ``backend_state[backend].model`` and an explicit CLI override belong to
    the active backend. Legacy flat project/team model fields and global
    defaults are Claude-shaped, so they must not seed Codex/Gemini/etc. with
    stale Claude slugs like ``opus[1m]``.
    """
    if explicit_model:
        return explicit_model
    if backend_model:
        return backend_model
    if backend_name == "claude":
        return legacy_claude_model or default_model_claude or default_model or None
    return None


# Removed in Task 5 Step 12:
#   - ``_migrate_usernames`` (legacy ``allowed_usernames`` / ``allowed_username``)
#   - ``_migrate_user_ids`` (legacy ``trusted_user_ids`` / ``trusted_user_id``)
#   - ``_coerce_user_id`` (int-with-fallback for cross-platform user ids)
#   - ``_effective_trusted_users``
#   - ``_migrate_trusted_users``
#   - ``_write_raw_trusted_users``
# All six supported the legacy ``allowed_usernames`` / ``trusted_users`` /
# ``trusted_user_ids`` fields, gone now from the dataclass and on-disk format.
# ``_migrate_legacy_auth`` (the loader's compat shim) reads raw JSON keys
# directly so it doesn't need these helpers either.


def _split_project_entries(projects: object) -> tuple[dict[str, dict], list[str]]:
    """Partition raw project entries into valid configs and malformed leftovers."""
    if not isinstance(projects, dict):
        return {}, []

    valid: dict[str, dict] = {}
    malformed: list[str] = []
    for name, proj in projects.items():
        if isinstance(proj, dict) and "path" in proj:
            valid[name] = proj
        else:
            malformed.append(name)
    return valid, malformed


def _cleanup_malformed_projects(path: Path, names: list[str]) -> None:
    """Best-effort removal of known-bad project entries created by old bugs."""
    if not names:
        return

    def _patch(raw: dict) -> None:
        projects = raw.get("projects")
        if not isinstance(projects, dict):
            return
        for name in names:
            projects.pop(name, None)

    try:
        _patch_json(_patch, path)
    except OSError:
        # Reading config should still succeed even if cleanup cannot write.
        pass


def _is_valid_room_id(transport_id: str, native_id: str) -> bool:
    """Per-transport shape check for a room binding's native_id.

    Centralises the prefix rules so adding Discord/Slack later updates one
    place instead of every call site.
    """
    if not transport_id or not native_id:
        return False
    if transport_id == "google_chat":
        return native_id.startswith("spaces/")
    return True


def _team_has_room(team: dict) -> bool:
    """Return True if the team dict has a structured room binding with valid shape."""
    room = team.get("room")
    if not isinstance(room, dict):
        return False
    transport_id = room.get("transport_id")
    native_id = room.get("native_id")
    if not isinstance(transport_id, str) or not isinstance(native_id, str):
        return False
    return _is_valid_room_id(transport_id, native_id)


def _split_team_entries(
    teams: object,
) -> tuple[dict[str, dict], list[tuple[str, list[str]]]]:
    """Partition raw team entries into valid configs and malformed leftovers.

    Returns ``(valid, malformed)`` where ``malformed`` is a list of
    ``(team_name, missing_fields)`` tuples. A team is valid when its raw
    entry is a dict and contains ``path`` AND either ``group_chat_id`` or
    a structured ``room`` block (transport-portable alternative).
    """
    if not isinstance(teams, dict):
        return {}, []

    valid: dict[str, dict] = {}
    malformed: list[tuple[str, list[str]]] = []
    for name, team in teams.items():
        if not isinstance(team, dict):
            malformed.append((name, ["entry-not-dict"]))
            continue
        has_path = "path" in team
        has_room_id = "group_chat_id" in team or _team_has_room(team)
        missing = (["path"] if not has_path else []) + (["group_chat_id or room"] if not has_room_id else [])
        if missing:
            malformed.append((name, missing))
        else:
            valid[name] = team
    return valid, malformed


def _cleanup_malformed_teams(path: Path, names: list[str]) -> None:
    """Best-effort removal of partial team entries that the loader skipped."""
    if not names:
        return

    def _patch(raw: dict) -> None:
        teams = raw.get("teams")
        if not isinstance(teams, dict):
            return
        for name in names:
            teams.pop(name, None)

    try:
        _patch_json(_patch, path)
    except OSError:
        pass


def _team_is_configured(raw: dict, team_name: str) -> bool:
    """True if a team entry has the required fields to be loaded.

    Used by team-bot writer helpers to refuse creating partial entries
    when the team has not been fully configured yet.
    """
    team = raw.get("teams", {}).get(team_name)
    return (
        isinstance(team, dict)
        and "path" in team
        and ("group_chat_id" in team or _team_has_room(team))
    )


def _make_room_binding(raw: dict | None) -> RoomBinding | None:
    """Build a structured room binding from raw config, ignoring malformed rows.

    For Google Chat entries, the native_id must start with ``spaces/`` — any
    other shape is silently dropped so the manager can re-derive it.
    """
    if not raw:
        return None
    transport_id = raw.get("transport_id")
    native_id = raw.get("native_id")
    if not transport_id or not native_id:
        return None
    if not _is_valid_room_id(str(transport_id), str(native_id)):
        return None
    return RoomBinding(transport_id=str(transport_id), native_id=str(native_id))


def _parse_bot_peer(raw: object) -> "BotPeerRef | None":
    """Build a BotPeerRef from raw config, with Google Chat shape validation.

    For Google Chat entries, the native_id must start with ``users/`` — any
    other shape is silently dropped so the manager can re-derive the peer
    from the next addition response.
    """
    if not isinstance(raw, dict):
        return None
    transport_id = raw.get("transport_id")
    native_id = raw.get("native_id")
    if not isinstance(transport_id, str) or not isinstance(native_id, str):
        return None
    if transport_id == "google_chat" and not native_id.startswith("users/"):
        # Google Chat REST identifies app/bot peers as `users/<id>`.
        # A malformed entry would cause downstream API calls to 4xx,
        # so we drop it here and let the manager re-derive the peer
        # from the next addition response.
        logger.warning(
            "dropping malformed bot_peer for transport %r: native_id %r is not a 'users/' path",
            transport_id,
            native_id,
        )
        return None
    handle = raw.get("handle")
    display_name = raw.get("display_name")
    return BotPeerRef(
        transport_id=transport_id,
        native_id=native_id,
        handle=handle if isinstance(handle, str) else None,
        display_name=display_name if isinstance(display_name, str) else "",
    )


def _make_team_bot_config(b: dict) -> TeamBotConfig:
    """Build a TeamBotConfig from a raw dict, folding legacy fields into backend_state.

    Reads each Claude-shaped legacy field from ``backend_state["claude"]``
    first (canonical post-v1.0 shape); pre-v1.0 on-disk configs fall back
    to the top-level keys.
    """
    bot_model = _load_claude_field(b, "model")
    bot_effort = _load_claude_field(b, "effort")
    bot_permissions = _load_permissions(b)
    bot_session_id = _load_claude_field(b, "session_id")
    bot_show_thinking = bool(_load_claude_field(b, "show_thinking", False))
    backend_state = b.get("backend_state") or _legacy_backend_state(
        bot_model,
        bot_effort,
        bot_permissions,
        bot_session_id,
        bot_show_thinking,
    )
    return TeamBotConfig(
        telegram_bot_token=b.get("telegram_bot_token", ""),
        active_persona=b.get("active_persona"),
        autostart=b.get("autostart", False),
        permissions=bot_permissions,
        bot_username=b.get("bot_username", ""),
        session_id=bot_session_id,
        model=bot_model,
        effort=bot_effort,
        show_thinking=bot_show_thinking,
        bot_peer=_parse_bot_peer(b.get("bot_peer")),
        backend=b.get("backend", "claude"),
        backend_state=backend_state,
        context_enabled=bool(b.get("context_enabled", True)),
        context_history_limit=int(b.get("context_history_limit", 10)),
    )


def _parse_google_chat(raw: object) -> GoogleChatConfig:
    if raw is None:
        return GoogleChatConfig()
    if not isinstance(raw, dict):
        raise ConfigError("google_chat must be an object")
    allowed = raw.get("allowed_audiences", [])
    if not isinstance(allowed, list) or not all(isinstance(v, str) for v in allowed):
        raise ConfigError("google_chat.allowed_audiences must be a list of strings")
    auth_type = str(raw.get("auth_audience_type", "endpoint_url"))
    if auth_type not in {"endpoint_url", "project_number"}:
        raise ConfigError("google_chat.auth_audience_type must be endpoint_url or project_number")
    return GoogleChatConfig(
        service_account_file=str(raw.get("service_account_file", "")),
        app_id=str(raw.get("app_id", "")),
        project_number=str(raw.get("project_number", "")),
        auth_audience_type=auth_type,
        allowed_audiences=allowed,
        endpoint_path=str(raw.get("endpoint_path", "/google-chat/events")),
        public_url=str(raw.get("public_url", "")),
        host=str(raw.get("host", "127.0.0.1")),
        port=int(raw.get("port", 8090)),
        root_command_name=str(raw.get("root_command_name", "lp2c")),
        root_command_id=None if raw.get("root_command_id") is None else int(raw.get("root_command_id")),
        callback_token_ttl_seconds=int(raw.get("callback_token_ttl_seconds", 900)),
        pending_prompt_ttl_seconds=int(raw.get("pending_prompt_ttl_seconds", 900)),
        max_message_bytes=int(raw.get("max_message_bytes", 32_000)),
        attachment_max_bytes=int(raw.get("attachment_max_bytes", 25_000_000)),
    )


def _serialize_google_chat(cfg: GoogleChatConfig) -> dict:
    return {
        "service_account_file": cfg.service_account_file,
        "app_id": cfg.app_id,
        "project_number": cfg.project_number,
        "auth_audience_type": cfg.auth_audience_type,
        "allowed_audiences": list(cfg.allowed_audiences),
        "endpoint_path": cfg.endpoint_path,
        "public_url": cfg.public_url,
        "host": cfg.host,
        "port": cfg.port,
        "root_command_name": cfg.root_command_name,
        "root_command_id": cfg.root_command_id,
        "callback_token_ttl_seconds": cfg.callback_token_ttl_seconds,
        "pending_prompt_ttl_seconds": cfg.pending_prompt_ttl_seconds,
        "max_message_bytes": cfg.max_message_bytes,
        "attachment_max_bytes": cfg.attachment_max_bytes,
    }


def _google_chat_is_default(cfg: GoogleChatConfig) -> bool:
    return cfg == GoogleChatConfig()


def load_config(path: Path = DEFAULT_CONFIG) -> Config:
    """Public API: acquires _config_lock for the read.

    Returns a fresh Config built from the file on disk. Runs the legacy-auth
    migration during parsing (sets ``Config.migration_pending`` when any
    legacy field was present) - see ``_migrate_legacy_auth``. Best-effort
    on-disk cleanup of malformed project/team entries runs AFTER the lock
    is released (each cleanup call re-acquires the lock; doing it inside
    would deadlock).
    """
    malformed_projects: list[str] = []
    malformed_teams: list[str] = []
    with _config_lock(path):
        config = _load_config_unlocked(
            path,
            _malformed_projects=malformed_projects,
            _malformed_teams=malformed_teams,
        )
    if malformed_projects:
        _cleanup_malformed_projects(path, malformed_projects)
    if malformed_teams:
        _cleanup_malformed_teams(path, malformed_teams)
    return config


def _load_config_unlocked(
    path: Path,
    *,
    _malformed_projects: list[str] | None = None,
    _malformed_teams: list[str] | None = None,
) -> Config:
    """Load Config without acquiring _config_lock. Caller must hold the lock.

    When ``_malformed_projects`` / ``_malformed_teams`` lists are provided,
    the loader appends names of skipped malformed entries to them for the
    caller to clean up outside the lock. Used by the public ``load_config``
    wrapper. ``locked_config_rmw`` callers pass nothing (None) since they're
    doing their own RMW and don't need disk cleanup.
    """
    config = Config()
    if path.exists():
        raw = json.loads(path.read_text())
        # Global allow-list: explicit `allowed_users` always wins; the legacy
        # migration helper fills in only when no explicit list is present.
        explicit_global = _parse_allowed_users(raw.get("allowed_users", []))
        migrated_global, did_migrate_global = _migrate_legacy_auth(raw)
        config.allowed_users = explicit_global or migrated_global
        if did_migrate_global:
            config.migration_pending = True
        raw_meta = raw.get("meta_dir")
        if raw_meta is None:
            config.meta_dir = DEFAULT_META_DIR
        elif isinstance(raw_meta, str):
            config.meta_dir = Path(raw_meta).expanduser()
        else:
            logger.warning(
                "meta_dir must be a string path; got %r (treating as default)",
                raw_meta,
            )
            config.meta_dir = DEFAULT_META_DIR
        config.github_pat = raw.get("github_pat", "")
        config.telegram_api_id = raw.get("telegram_api_id", 0)
        config.telegram_api_hash = raw.get("telegram_api_hash", "")
        # Support old name for backward compatibility
        config.manager_telegram_bot_token = raw.get(
            "manager_telegram_bot_token", raw.get("manager_bot_token", "")
        )
        config.stt_backend = raw.get("stt_backend", "")
        config.openai_api_key = raw.get("openai_api_key", "")
        config.whisper_model = raw.get("whisper_model", "whisper-1")
        config.whisper_language = raw.get("whisper_language", "")
        config.tts_backend = raw.get("tts_backend", "")
        config.tts_model = raw.get("tts_model", "tts-1")
        config.tts_voice = raw.get("tts_voice", "alloy")
        config.google_chat = _parse_google_chat(raw.get("google_chat"))
        config.default_backend = raw.get("default_backend", "claude")
        config.default_model_claude = raw.get(
            "default_model_claude", raw.get("default_model", "")
        )
        config.default_model = raw.get("default_model", config.default_model_claude)
        valid_projects, malformed_projects = _split_project_entries(raw.get("projects", {}))
        for name in malformed_projects:
            # Tolerate phantom projects entries (no `path`) left over from a
            # pre-34b8dc5 bug where /persona on a team bot wrote to
            # projects[<team>_<role>] instead of the team config. Skipping
            # lets the manager start; we also clean them up best-effort so the
            # warning does not repeat on every subsequent load.
            logger.warning("skipping malformed project %r (no path) in config", name)
        if malformed_projects and _malformed_projects is not None:
            _malformed_projects.extend(malformed_projects)

        for name, proj in valid_projects.items():
            # Source-of-truth for the legacy mirror fields is
            # ``backend_state["claude"]`` first; the top-level keys are only
            # consulted for pre-v1.0 configs that predate the new shape.
            proj_model = _load_claude_field(proj, "model")
            proj_effort = _load_claude_field(proj, "effort")
            proj_permissions = _load_permissions(proj)
            proj_session_id = _load_claude_field(proj, "session_id")
            proj_show_thinking = bool(_load_claude_field(proj, "show_thinking", False))
            backend_state = proj.get("backend_state") or _legacy_backend_state(
                proj_model,
                proj_effort,
                proj_permissions,
                proj_session_id,
                proj_show_thinking,
            )
            # Per-project allowed_users: explicit shape wins, migration helper
            # synthesizes from legacy fields when no explicit list is present.
            explicit_proj = _parse_allowed_users(proj.get("allowed_users", []))
            migrated_proj, did_migrate_proj = _migrate_legacy_auth(proj)
            effective = explicit_proj or migrated_proj
            if did_migrate_proj:
                config.migration_pending = True
            raw_rig = proj.get("respond_in_groups", False)
            if isinstance(raw_rig, bool):
                respond_in_groups = raw_rig
            elif isinstance(raw_rig, int) and not isinstance(raw_rig, bool):
                # Python: bool is subclass of int. Treat int 0/1 as bool via bool().
                respond_in_groups = bool(raw_rig)
            else:
                logger.warning(
                    "project %r: respond_in_groups must be a bool; got %r (treating as False)",
                    name, raw_rig,
                )
                respond_in_groups = False
            raw_sp = proj.get("safety_prompt", None)
            if raw_sp is None:
                safety_prompt = None
            elif isinstance(raw_sp, str):
                safety_prompt = raw_sp  # string (possibly empty)
            else:
                logger.warning(
                    "project %r: safety_prompt must be a string or absent; got %r (treating as default)",
                    name, raw_sp,
                )
                safety_prompt = None
            config.projects[name] = ProjectConfig(
                path=proj["path"],
                telegram_bot_token=proj.get("telegram_bot_token", ""),
                model=proj_model,
                effort=proj_effort,
                permissions=proj_permissions,
                session_id=proj_session_id,
                autostart=proj.get("autostart", False),
                active_persona=proj.get("active_persona"),
                show_thinking=proj_show_thinking,
                backend=proj.get("backend", "claude"),
                backend_state=backend_state,
                context_enabled=bool(proj.get("context_enabled", True)),
                context_history_limit=int(proj.get("context_history_limit", 10)),
                allowed_users=effective,
                plugins=_parse_plugins(proj.get("plugins", [])),
                respond_in_groups=respond_in_groups,
                safety_prompt=safety_prompt,
            )
            if not effective and not config.allowed_users:
                # Only warn when BOTH scopes are empty - global fallback would
                # otherwise cover an empty project list.
                logger.warning(
                    "project %r has no users authorized at either project or "
                    "global scope; bot will reject all messages until populated",
                    name,
                )
        valid_teams, malformed_teams = _split_team_entries(raw.get("teams", {}))
        if malformed_teams:
            for name, missing in malformed_teams:
                logger.warning(
                    "Skipping malformed team %r in %s — missing required field(s): %s",
                    name, path, ", ".join(missing),
                )
            if _malformed_teams is not None:
                _malformed_teams.extend(name for name, _ in malformed_teams)
        for name, team in valid_teams.items():
            team_cfg = TeamConfig(
                path=team["path"],
                group_chat_id=int(team.get("group_chat_id", 0)),
                room=_make_room_binding(team.get("room")),
                bots={
                    role: _make_team_bot_config(b)
                    for role, b in team.get("bots", {}).items()
                },
                max_autonomous_turns=int(team.get("max_autonomous_turns", 5)),
                safety_mode=team.get("safety_mode", "strict"),
            )
            # Backward-compat migration: synthesize a Telegram RoomBinding from
            # the legacy ``group_chat_id`` field so new code can prefer
            # ``team_cfg.room`` while still reading existing configs.
            if team_cfg.group_chat_id != 0 and team_cfg.room is None:
                team_cfg.room = RoomBinding(
                    transport_id="telegram",
                    native_id=str(team_cfg.group_chat_id),
                )
            # Backward-compat migration: synthesize a Telegram BotPeerRef from
            # the legacy ``bot_username`` field for each bot that has a handle
            # but no structured peer ref yet.
            for bot_cfg in team_cfg.bots.values():
                if bot_cfg.bot_username and bot_cfg.bot_peer is None:
                    bot_cfg.bot_peer = BotPeerRef(
                        transport_id="telegram",
                        native_id="",
                        handle=bot_cfg.bot_username,
                    )
            config.teams[name] = team_cfg
    return config


def _serialize_team_bot(b: "TeamBotConfig") -> dict:
    """Build the raw JSON dict for a team bot.

    The canonical home for Claude-shaped state is ``backend_state["claude"]``;
    any leftover legacy top-level keys (model / effort / permissions /
    session_id / show_thinking) from a pre-v1.0 on-disk shape are stripped.
    """
    backend_state = _effective_backend_state(
        b.backend_state,
        model=b.model,
        effort=b.effort,
        permissions=b.permissions,
        session_id=b.session_id,
        show_thinking=b.show_thinking,
    )
    entry: dict = {
        "telegram_bot_token": b.telegram_bot_token,
        "backend": b.backend,
        "backend_state": backend_state,
    }
    if b.active_persona:
        entry["active_persona"] = b.active_persona
    if b.autostart:
        entry["autostart"] = True
    if b.bot_username:
        entry["bot_username"] = b.bot_username
    if b.bot_peer is not None:
        entry["bot_peer"] = {
            "transport_id": b.bot_peer.transport_id,
            "native_id": b.bot_peer.native_id,
            "handle": b.bot_peer.handle,
            "display_name": b.bot_peer.display_name,
        }
    if not b.context_enabled:
        entry["context_enabled"] = False
    if b.context_history_limit != 10:
        entry["context_history_limit"] = b.context_history_limit
    _strip_legacy_claude_fields(entry)
    return entry


def save_config(config: Config, path: Path = DEFAULT_CONFIG) -> None:
    """Public API: acquires _config_lock for the write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        path.parent.chmod(0o700)
    with _config_lock(path):
        _save_config_unlocked(config, path)


@contextmanager
def locked_config_rmw(path: Path = DEFAULT_CONFIG):
    """Hold _config_lock across a load-modify-save cycle.

    Yields a freshly-loaded Config; caller mutates it; the context manager's
    block writes it back via ``save_config_within_lock`` (do NOT call
    ``save_config`` inside the block - it would deadlock by re-acquiring the
    same lock).

    Used by ProjectBot._persist_auth_if_dirty so concurrent first-contact
    locks serialize correctly. Without this, two processes could each
    ``load_config()`` the same pre-write state, append different identities,
    and ``save_config()`` - last writer wins.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        path.parent.chmod(0o700)
    with _config_lock(path):
        yield _load_config_unlocked(path)


def save_config_within_lock(config: Config, path: Path = DEFAULT_CONFIG) -> None:
    """Save Config without re-acquiring _config_lock. For callers inside
    ``locked_config_rmw`` who already hold the lock."""
    _save_config_unlocked(config, path)


def _save_config_unlocked(config: Config, path: Path) -> None:
    raw: dict = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    # Task 5 Step 12: ``Config.allowed_users`` is the sole source of truth.
    # The legacy on-disk fields (``allowed_usernames``, ``trusted_users``,
    # ``trusted_user_ids``) are stripped on every save.
    if config.allowed_users:
        raw["allowed_users"] = _serialize_allowed_users(config.allowed_users)
    else:
        raw.pop("allowed_users", None)
    raw.pop("allowed_usernames", None)
    raw.pop("allowed_username", None)
    raw.pop("trusted_users", None)
    raw.pop("trusted_user_ids", None)
    raw.pop("trusted_user_id", None)
    if config.meta_dir != DEFAULT_META_DIR:
        raw["meta_dir"] = str(config.meta_dir)
    else:
        raw.pop("meta_dir", None)
    if not _google_chat_is_default(config.google_chat):
        raw["google_chat"] = _serialize_google_chat(config.google_chat)
    else:
        raw.pop("google_chat", None)
    raw["manager_telegram_bot_token"] = config.manager_telegram_bot_token
    raw.pop("manager_bot_token", None)  # remove old name if present
    if config.github_pat:
        raw["github_pat"] = config.github_pat
    else:
        raw.pop("github_pat", None)
    if config.telegram_api_id:
        raw["telegram_api_id"] = config.telegram_api_id
    else:
        raw.pop("telegram_api_id", None)
    if config.telegram_api_hash:
        raw["telegram_api_hash"] = config.telegram_api_hash
    else:
        raw.pop("telegram_api_hash", None)
    if config.stt_backend:
        raw["stt_backend"] = config.stt_backend
    else:
        raw.pop("stt_backend", None)
    if config.openai_api_key:
        raw["openai_api_key"] = config.openai_api_key
    else:
        raw.pop("openai_api_key", None)
    # Only omit whisper_model when it's the default "whisper-1" (keeps JSON clean).
    if config.whisper_model and config.whisper_model != "whisper-1":
        raw["whisper_model"] = config.whisper_model
    else:
        raw.pop("whisper_model", None)
    if config.whisper_language:
        raw["whisper_language"] = config.whisper_language
    else:
        raw.pop("whisper_language", None)
    if config.tts_backend:
        raw["tts_backend"] = config.tts_backend
    else:
        raw.pop("tts_backend", None)
    if config.tts_model and config.tts_model != "tts-1":
        raw["tts_model"] = config.tts_model
    else:
        raw.pop("tts_model", None)
    if config.tts_voice and config.tts_voice != "alloy":
        raw["tts_voice"] = config.tts_voice
    else:
        raw.pop("tts_voice", None)
    raw["default_backend"] = config.default_backend
    # ``default_model_claude`` is the canonical field. The legacy
    # ``default_model`` mirror was kept one release for downgrade safety;
    # v1.0.0 shipped, so save now writes only the new field and always
    # strips the legacy key from the raw entry.
    effective_default_model_claude = config.default_model_claude or config.default_model
    if effective_default_model_claude:
        raw["default_model_claude"] = effective_default_model_claude
    else:
        raw.pop("default_model_claude", None)
    raw.pop("default_model", None)
    # Merge per-project data, preserving unknown keys already in the file
    existing_projects: dict = raw.get("projects", {})
    for name, p in config.projects.items():
        proj = existing_projects.get(name, {})
        proj["path"] = p.path
        proj["telegram_bot_token"] = p.telegram_bot_token
        # Task 5 Step 12: per-project ``allowed_users`` is the sole shape.
        if p.allowed_users:
            proj["allowed_users"] = _serialize_allowed_users(p.allowed_users)
        else:
            proj.pop("allowed_users", None)
        if p.plugins:
            proj["plugins"] = p.plugins
        else:
            proj.pop("plugins", None)
        if p.respond_in_groups:
            proj["respond_in_groups"] = True
        else:
            proj.pop("respond_in_groups", None)
        if p.safety_prompt is None:
            proj.pop("safety_prompt", None)
        else:
            # Empty string is a meaningful "explicit disable"; write it through.
            proj["safety_prompt"] = p.safety_prompt
        # Strip any legacy keys lingering on the raw entry.
        proj.pop("allowed_usernames", None)
        proj.pop("username", None)
        proj.pop("trusted_users", None)
        proj.pop("trusted_user_ids", None)
        proj.pop("trusted_user_id", None)
        backend_state = _effective_backend_state(
            p.backend_state,
            model=p.model,
            effort=p.effort,
            permissions=p.permissions,
            session_id=p.session_id,
            show_thinking=p.show_thinking,
        )
        proj["backend"] = p.backend
        proj["backend_state"] = backend_state
        _strip_legacy_claude_fields(proj)
        proj.pop("permission_mode", None)
        proj.pop("dangerously_skip_permissions", None)
        proj["autostart"] = p.autostart
        if p.active_persona:
            proj["active_persona"] = p.active_persona
        else:
            proj.pop("active_persona", None)
        # Conversation-log toggles. Persist only when non-default to keep the
        # JSON tidy; loader treats absent keys as the defaults.
        if not p.context_enabled:
            proj["context_enabled"] = False
        else:
            proj.pop("context_enabled", None)
        if p.context_history_limit != 10:
            proj["context_history_limit"] = p.context_history_limit
        else:
            proj.pop("context_history_limit", None)
        existing_projects[name] = proj
    # Merge teams
    existing_teams: dict = raw.get("teams", {})
    for name, team in config.teams.items():
        entry = existing_teams.get(name, {})
        entry["path"] = team.path
        entry["group_chat_id"] = team.group_chat_id
        entry["max_autonomous_turns"] = team.max_autonomous_turns
        entry["safety_mode"] = team.safety_mode
        if team.room is not None:
            entry["room"] = {
                "transport_id": team.room.transport_id,
                "native_id": team.room.native_id,
            }
        else:
            entry.pop("room", None)
        entry["bots"] = {
            role: _serialize_team_bot(b)
            for role, b in team.bots.items()
        }
        existing_teams[name] = entry
    raw["teams"] = {k: v for k, v in existing_teams.items() if k in config.teams}
    if not raw["teams"]:
        raw.pop("teams", None)
    # Remove projects that no longer exist in config
    raw["projects"] = {k: v for k, v in existing_projects.items() if k in config.projects}
    _atomic_write(path, json.dumps(raw, indent=2) + "\n")
    # New (Task 3): clear the in-memory migration flag so callers can re-read
    # state without re-saving. The disk format no longer contains legacy keys,
    # so the next load will load with migration_pending=False.
    config.migration_pending = False


def _atomic_write(path: Path, data: str) -> None:
    """Write data to path atomically via tempfile + rename."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    closed = False
    try:
        os.write(fd, data.encode())
        if sys.platform != "win32":
            os.fchmod(fd, 0o600)
        os.close(fd)
        closed = True
        os.replace(tmp, path)
    except BaseException:
        if not closed:
            os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _patch_json(patch_fn, path: Path) -> None:
    """Read-modify-write config JSON with file locking."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _config_lock(path):
        raw: dict = {}
        if path.exists():
            try:
                raw = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        patch_fn(raw)
        _atomic_write(path, json.dumps(raw, indent=2) + "\n")


def _active_backend_name(entry: dict) -> str:
    """Return the active backend for a config entry, defaulting to ``claude``."""
    return entry.get("backend") or "claude"


def _session_from_entry(entry: dict) -> str | None:
    """Read session_id from an entry, preferring backend_state over legacy flat key."""
    backend_name = _active_backend_name(entry)
    backend_state = entry.get("backend_state") or {}
    state = backend_state.get(backend_name) or {}
    return state.get("session_id") or entry.get("session_id")


def _ensure_backend_state_seeded(entry: dict) -> dict:
    """Fold legacy Claude flat fields into backend_state on-the-fly during a patch.

    Patching helpers operate directly on raw JSON, so a legacy-only entry
    (no ``backend_state`` yet) would lose its sibling flat values when the
    helper writes a single key under ``backend_state[claude]`` and then
    re-mirrors. Seeding the in-progress raw entry from legacy fields keeps
    the round-trip lossless. Returns the (possibly mutated) backend_state.
    """
    backend_state = entry.setdefault("backend_state", {})
    if "claude" not in backend_state:
        legacy = _legacy_backend_state(
            entry.get("model"),
            entry.get("effort"),
            entry.get("permissions"),
            entry.get("session_id"),
            entry.get("show_thinking", False),
        )
        if legacy.get("claude"):
            backend_state["claude"] = legacy["claude"]
    return backend_state


def load_sessions(path: Path = DEFAULT_CONFIG) -> dict[str, str]:
    """Load all session IDs from config.json per-project entries."""
    if path.exists():
        try:
            raw = json.loads(path.read_text())
            sessions: dict[str, str] = {}
            for name, proj in raw.get("projects", {}).items():
                sid = _session_from_entry(proj)
                if sid:
                    sessions[name] = sid
            for t_name, t_data in raw.get("teams", {}).items():
                for r_name, r_data in t_data.get("bots", {}).items():
                    sid = _session_from_entry(r_data)
                    if sid:
                        sessions[f"{t_name}_{r_name}"] = sid
            return sessions
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def load_session(
    project_name: str,
    path: Path = DEFAULT_CONFIG,
    *,
    team_name: str | None = None,
    role: str | None = None,
) -> str | None:
    """Load one persisted session for either a project or a team bot.

    Prefers the new ``backend_state[<active_backend>]["session_id"]`` shape and
    falls back to the legacy flat ``session_id`` for old configs.
    """
    if path.exists():
        try:
            raw = json.loads(path.read_text())
            if team_name and role:
                entry = (
                    raw.get("teams", {})
                    .get(team_name, {})
                    .get("bots", {})
                    .get(role, {})
                )
                return _session_from_entry(entry)
            entry = raw.get("projects", {}).get(project_name, {})
            return _session_from_entry(entry)
        except (json.JSONDecodeError, OSError):
            pass
    return None


def patch_project(project_name: str, fields: dict, path: Path = DEFAULT_CONFIG) -> None:
    """Update specific fields on a project entry. None values remove the key."""
    def _patch(raw: dict) -> None:
        proj = raw.setdefault("projects", {}).setdefault(project_name, {})
        for k, v in fields.items():
            if v is None:
                proj.pop(k, None)
            else:
                proj[k] = v
    _patch_json(_patch, path)


def patch_team(team_name: str, fields: dict, path: Path = DEFAULT_CONFIG) -> None:
    """Update specific fields on a team entry. None values remove the key.

    Top-level replacement only: passing {"bots": {...}} replaces the entire
    `bots` dict (not a deep merge). Callers that need to update one bot must
    read the current team, modify the bots dict, and write it back whole.
    """
    def _patch(raw: dict) -> None:
        team = raw.setdefault("teams", {}).setdefault(team_name, {})
        for k, v in fields.items():
            if v is None:
                team.pop(k, None)
            else:
                team[k] = v
    _patch_json(_patch, path)


def load_teams(path: Path = DEFAULT_CONFIG) -> dict[str, TeamConfig]:
    """Load all team entries. Returns empty dict if the file is missing or invalid."""
    if path.exists():
        try:
            raw = json.loads(path.read_text())
            return {
                name: TeamConfig(
                    path=team["path"],
                    group_chat_id=int(team.get("group_chat_id", 0)),
                    room=_make_room_binding(team.get("room")),
                    bots={
                        role: _make_team_bot_config(b)
                        for role, b in team.get("bots", {}).items()
                    },
                    max_autonomous_turns=int(team.get("max_autonomous_turns", 5)),
                    safety_mode=team.get("safety_mode", "strict"),
                )
                for name, team in raw.get("teams", {}).items()
            }
        except (json.JSONDecodeError, OSError, KeyError):
            pass
    return {}


def save_session(
    project_name: str,
    session_id: str,
    path: Path = DEFAULT_CONFIG,
    *,
    team_name: str | None = None,
    role: str | None = None,
) -> None:
    """Persist session_id into backend_state[<active_backend>].

    Always strips any leftover legacy top-level Claude-shaped keys from the
    entry on save — those keys belong to the pre-v1.0 mirror and are no
    longer written by any code path.
    """
    def _patch(raw: dict) -> None:
        if team_name and role:
            if not _team_is_configured(raw, team_name):
                logger.warning(
                    "Ignoring session save for team %r role %r — "
                    "team not configured.",
                    team_name, role,
                )
                return
            entry = (
                raw["teams"][team_name]
                .setdefault("bots", {})
                .setdefault(role, {})
            )
        else:
            entry = raw.setdefault("projects", {}).setdefault(project_name, {})
        backend_name = _active_backend_name(entry)
        backend_state = _ensure_backend_state_seeded(entry)
        state = backend_state.setdefault(backend_name, {})
        state["session_id"] = session_id
        _strip_legacy_claude_fields(entry)

    _patch_json(_patch, path)


def clear_session(
    project_name: str,
    path: Path = DEFAULT_CONFIG,
    *,
    team_name: str | None = None,
    role: str | None = None,
) -> None:
    """Drop session_id from backend_state[<active_backend>] and strip any
    leftover legacy top-level Claude-shaped keys."""
    def _patch(raw: dict) -> None:
        if team_name and role:
            if not _team_is_configured(raw, team_name):
                logger.warning(
                    "Ignoring session clear for team %r role %r — "
                    "team not configured.",
                    team_name, role,
                )
                return
            entry = (
                raw["teams"][team_name]
                .setdefault("bots", {})
                .setdefault(role, {})
            )
        else:
            entry = raw.setdefault("projects", {}).setdefault(project_name, {})
        backend_name = _active_backend_name(entry)
        backend_state = _ensure_backend_state_seeded(entry)
        state = backend_state.setdefault(backend_name, {})
        state.pop("session_id", None)
        _strip_legacy_claude_fields(entry)

    _patch_json(_patch, path)


def patch_backend_state(
    project_name: str,
    backend_name: str,
    fields: dict,
    path: Path = DEFAULT_CONFIG,
) -> None:
    """Update backend_state[<backend_name>] for a project entry.

    None values remove the key. If the on-disk entry only has legacy flat
    fields, they are folded into ``backend_state["claude"]`` first so
    unrelated fields are not lost during the partial update. For
    ``backend_name == "claude"`` any leftover legacy top-level keys are
    stripped from the entry on save.
    """
    def _patch(raw: dict) -> None:
        proj = raw.setdefault("projects", {}).setdefault(project_name, {})
        backend_state = _ensure_backend_state_seeded(proj)
        state = backend_state.setdefault(backend_name, {})
        for key, value in fields.items():
            if value is None:
                state.pop(key, None)
            else:
                state[key] = value
        _strip_legacy_claude_fields(proj)

    _patch_json(_patch, path)


def patch_team_bot_backend_state(
    team_name: str,
    role: str,
    backend_name: str,
    fields: dict,
    path: Path = DEFAULT_CONFIG,
) -> None:
    """Update backend_state[<backend_name>] for a team-bot entry.

    None values remove the key. Legacy flat fields are folded into
    ``backend_state["claude"]`` first when the on-disk entry only has the
    legacy shape. For ``backend_name == "claude"`` any leftover legacy
    top-level keys are stripped from the entry on save.

    Refuses to materialize a team that hasn't been configured (no ``path`` /
    ``group_chat_id``). Without this guard a stray write from a team-bot
    process whose team was deleted upstream would re-create a partial entry
    that the loader then rejects, taking down the manager service.
    """
    def _patch(raw: dict) -> None:
        if not _team_is_configured(raw, team_name):
            logger.warning(
                "Ignoring backend_state write for team %r role %r — "
                "team not configured (missing 'path' / 'group_chat_id').",
                team_name, role,
            )
            return
        bot = (
            raw["teams"][team_name]
            .setdefault("bots", {})
            .setdefault(role, {})
        )
        backend_state = _ensure_backend_state_seeded(bot)
        state = backend_state.setdefault(backend_name, {})
        for key, value in fields.items():
            if value is None:
                state.pop(key, None)
            else:
                state[key] = value
        _strip_legacy_claude_fields(bot)

    _patch_json(_patch, path)


def patch_team_bot_backend(
    team_name: str,
    role: str,
    backend_name: str,
    path: Path = DEFAULT_CONFIG,
) -> None:
    """Set the active backend on a team-bot entry without touching state.

    Refuses to write if the team is not configured; see the safety note on
    ``patch_team_bot_backend_state``.
    """
    def _patch(raw: dict) -> None:
        if not _team_is_configured(raw, team_name):
            logger.warning(
                "Ignoring backend write for team %r role %r — "
                "team not configured (missing 'path' / 'group_chat_id').",
                team_name, role,
            )
            return
        bot = (
            raw["teams"][team_name]
            .setdefault("bots", {})
            .setdefault(role, {})
        )
        bot["backend"] = backend_name

    _patch_json(_patch, path)


# Removed in Task 5 Step 12 (all wrote to legacy ``trusted_user_id`` /
# ``trusted_user_ids`` keys on disk):
#   - ``load_trusted_user_id`` / ``save_trusted_user_id``
#   - ``save_project_trusted_user_id`` / ``clear_trusted_user_id``
#   - ``add_trusted_user_id`` / ``add_project_trusted_user_id``
#   - ``bind_trusted_user`` / ``bind_project_trusted_user``
#   - ``unbind_trusted_user`` / ``unbind_project_trusted_user``
# The new auth flow appends to ``AllowedUser.locked_identities`` via
# ``_persist_auth_if_dirty``; manager-bot ``/add_user`` / ``/remove_user``
# mutate ``Config.allowed_users``.
