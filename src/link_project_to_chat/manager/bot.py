from __future__ import annotations

import importlib.metadata
import logging
import time
import warnings
from pathlib import Path
from typing import TYPE_CHECKING

from telegram import Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from .config import load_project_configs, save_project_configs
from .process import ProcessManager
from ..config import AllowedUser, DEFAULT_CONFIG, parse_user_bool, patch_backend_state
from .._auth import AuthMixin
from ..transport import Button, Buttons, ChatRef, MessageRef

if TYPE_CHECKING:
    from ..repo_provider import RepoProvider
    from ..transport import ButtonClick, CommandInvocation, IncomingMessage

logger = logging.getLogger(__name__)

COMMANDS = [
    ("projects", "List all projects"),
    ("start_all", "Start all projects"),
    ("stop_all", "Stop all projects"),
    ("add_project", "Add a new project"),
    ("edit_project", "Edit a project"),
    ("users", "List authorized users"),
    ("add_user", "Add an authorized user"),
    ("remove_user", "Remove an authorized user"),
    ("promote_user", "Promote a user to executor"),
    ("demote_user", "Demote a user to viewer"),
    ("reset_user_identity", "Clear locked identities for a user"),
    ("setup", "Configure GitHub & Telegram API credentials"),
    ("create_project", "Create a new project (GitHub + bot)"),
    ("create_team", "Create a dual-agent team (2 bots + group)"),
    ("delete_team", "Delete a team (bots + group + folder)"),
    ("teams", "List existing teams (start/stop bots)"),
    ("model", "Set default model for all projects"),
    ("version", "Show version"),
    ("help", "Show commands"),
]

_EDITABLE_FIELDS = ("name", "path", "token", "username", "model", "permissions", "respond_in_groups", "safety_prompt")
_BUTTON_EDIT_FIELDS = ("name", "path", "token", "username", "model", "permissions", "respond_in_groups", "safety_prompt")

MODEL_OPTIONS = [
    ("opus[1m]", "Opus 4.7 1M"),
    ("opus", "Opus 4.7"),
    ("sonnet[1m]", "Sonnet 4.6 1M"),
    ("sonnet", "Sonnet 4.6"),
    ("haiku", "Haiku 4.5"),
]

_CREATE_DEPS_MESSAGE = (
    "Missing dependencies. Install with:\n"
    "pip install link-project-to-chat[create]"
)


def _load_botfather_dependency():
    from ..botfather import BotFatherClient

    return BotFatherClient


def _load_team_create_dependencies():
    from ..botfather import sanitize_bot_username
    from ..github_client import GitHubClient, RepoInfo
    from ..transport._telegram_group import (
        add_bot,
        create_supergroup,
        invite_user,
        promote_admin,
    )

    return (
        _load_botfather_dependency(),
        GitHubClient,
        RepoInfo,
        add_bot,
        create_supergroup,
        invite_user,
        promote_admin,
        sanitize_bot_username,
    )


def _load_team_delete_dependencies():
    from ..transport._telegram_group import delete_supergroup

    return _load_botfather_dependency(), delete_supergroup


def _build_persona_keyboard(project_path: Path, callback_prefix: str) -> Buttons:
    """Build a transport-native keyboard listing discovered personas.

    Each button's ``value`` is f'{callback_prefix}:{persona_name}'.
    """
    from ..skills import load_personas
    personas = load_personas(project_path)
    # load_personas may return a dict (name -> content) or a list of names
    names = sorted(personas.keys() if hasattr(personas, "keys") else personas)
    return Buttons(rows=[
        [Button(label=name, value=f"{callback_prefix}:{name}")]
        for name in names
    ])


def _parse_edit_callback(data: str) -> tuple[str, str] | None:
    """Parse 'proj_efld_<field>_<name>' → (field, name). Field comes first."""
    suffix = data[len("proj_efld_"):]
    for field in _EDITABLE_FIELDS:
        if suffix.startswith(field + "_"):
            name = suffix[len(field) + 1:]
            return field, name
    return None


def _create_team_preflight(cfg_path: Path, prefix: str | None = None) -> str | None:
    """Return an error string if pre-flight fails, None if OK.

    When ``prefix`` is None, only checks credential prereqs (Telethon, GitHub).
    When ``prefix`` is given, additionally checks for team-name and legacy project-name
    collisions.
    """
    from ..config import load_config
    from ..github_client import _gh_available

    config = load_config(cfg_path)

    if not config.telegram_api_id or not config.telegram_api_hash:
        return "Run `/setup` first — Telegram API credentials are not configured."
    session_file = cfg_path.parent / "telethon.session"
    if not session_file.exists():
        return "Run `/setup` first — Telethon session is not established."

    if not config.github_pat and not _gh_available():
        return "GitHub auth missing — run `/setup` with a PAT, or authenticate `gh` CLI."

    if prefix is None:
        return None

    # Telegram bot usernames max out at 32 chars. The orchestrator generates
    # `{prefix}_{role}_bot` (8 chars overhead for 3-char roles like "mgr" /
    # "dev") and may append `_N` on collision retries (2 chars). A prefix
    # > 22 would produce invalid usernames and BotFather would silently
    # reject every retry. Fail fast.
    _MAX_PREFIX_LEN = 22
    if len(prefix) > _MAX_PREFIX_LEN:
        return (
            f"Prefix `{prefix}` is too long ({len(prefix)} chars, max {_MAX_PREFIX_LEN}). "
            f"Telegram caps bot usernames at 32 chars and we generate "
            f"`<prefix>_<role>_<N>_bot`. Pick something shorter."
        )

    if prefix in config.teams:
        return f"Team `{prefix}` is already configured."

    legacy_names = [f"{prefix}_mgr", f"{prefix}_dev"]
    taken = [n for n in legacy_names if n in config.projects]
    if taken:
        return f"Those project names are taken: {', '.join(taken)}. Pick a different prefix."

    return None


_MAX_IN_SESSION_RATE_LIMIT_WAIT = 300.0
"""Upper bound (seconds) on how long we'll sleep waiting out a BotFather
throttle inside the /create_team callback. Above this we surface the wait to
the user — blocking a callback for hours is strictly worse than asking them
to retry tomorrow."""


async def _create_bot_with_retry(
    bfc,
    display_name: str,
    base_username: str,
    max_attempts: int = 5,
    max_rate_limit_retries: int = 2,
) -> tuple[str, str]:
    """Try creating a bot with base_username; on failure append _1/_2/..., up to max_attempts.

    A ``BotFatherRateLimit`` on the same candidate sleeps for the hinted
    ``retry_after`` and re-tries the SAME candidate (doesn't eat a suffix
    attempt). Limits total rate-limit retries via ``max_rate_limit_retries``
    so a permanent throttle still surfaces instead of hanging forever.

    If the hinted wait exceeds ``_MAX_IN_SESSION_RATE_LIMIT_WAIT`` we surface
    the throttle immediately (user-actionable) rather than sleeping through it.
    """
    import asyncio
    from ..botfather import _BOT_USERNAME_SUFFIX, BotFatherRateLimit

    # Find where the bot-suffix starts so collision-retry inserts "_N"
    # BEFORE the trailing "_bot" (Telegram requires names to end in "bot").
    # Falls back to end-of-string for callers that pass a base without the
    # suffix (e.g., older tests, hand-typed names).
    suffix_insert_at = base_username.rfind(_BOT_USERNAME_SUFFIX)
    if suffix_insert_at == -1:
        suffix_insert_at = len(base_username)

    rate_limit_retries = 0
    attempt = 0
    while attempt < max_attempts:
        if attempt == 0:
            candidate = base_username
        else:
            candidate = base_username[:suffix_insert_at] + f"_{attempt}" + base_username[suffix_insert_at:]
        try:
            token = await bfc.create_bot(display_name, candidate)
            return token, candidate
        except BotFatherRateLimit as exc:
            wait = max(float(exc.retry_after), 1.0)
            if wait > _MAX_IN_SESSION_RATE_LIMIT_WAIT:
                hours = wait / 3600.0
                raise RuntimeError(
                    f"BotFather is flood-limited for ~{hours:.1f}h ({wait:.0f}s). "
                    f"Retry /create_team after the cooldown; delete any orphaned "
                    f"bot via BotFather /deletebot first."
                ) from exc
            if rate_limit_retries >= max_rate_limit_retries:
                raise RuntimeError(
                    f"BotFather throttled us after {rate_limit_retries + 1} waits; giving up. "
                    f"Try /create_team again in a few minutes."
                ) from exc
            sleep_for = wait + 2.0  # +2s jitter
            logger.warning(
                "BotFather throttle on @%s; sleeping %.1fs before retrying same candidate.",
                candidate, sleep_for,
            )
            await asyncio.sleep(sleep_for)
            rate_limit_retries += 1
            # Don't advance `attempt` — retry the same candidate once BotFather cools down.
            continue
        except Exception:
            if attempt == max_attempts - 1:
                break
            attempt += 1
            continue
        attempt += 1
    raise RuntimeError(f"Bot username unavailable after {max_attempts} attempts (base={base_username})")


def _build_repo_provider(ctx, config) -> "RepoProvider":
    """Construct a RepoProvider from the wizard's stored provider choice.

    Reads ``ctx.user_data["create"]["provider"]`` — set by the new
    CREATE_PROVIDER_PICK callback. Defaults to GitHub when the field
    is missing (legacy callers).
    """
    provider = ctx.user_data.get("create", {}).get("provider", "github")
    if provider == "gitlab":
        from ..gitlab_client import GitLabClient
        return GitLabClient(pat=config.gitlab_pat, host=config.gitlab_host)
    from ..github_client import GitHubClient
    return GitHubClient(pat=config.github_pat)


class ManagerBot(AuthMixin):
    _MAX_MESSAGES_PER_MINUTE = 20

    def __init__(
        self,
        token: str,
        process_manager: ProcessManager,
        allowed_users: list["AllowedUser"] | None = None,
        project_config_path: Path | None = None,
    ):
        self._token = token
        self._pm = process_manager
        self._allowed_users: list = list(allowed_users or [])
        self._started_at = time.monotonic()
        self._app = None
        self._project_config_path = project_config_path
        # Persistent Telethon client shared by /create_team and supergroup
        # deletion. Project bots own their own TeamRelay (see #0c).
        self._telethon_client = None
        self._init_auth()

    def _load_projects(self) -> dict[str, dict]:
        path = self._project_config_path
        return load_project_configs(path) if path else load_project_configs()

    def _save_projects(self, projects: dict[str, dict]) -> None:
        path = self._project_config_path
        if path:
            save_project_configs(projects, path)
        else:
            save_project_configs(projects)

    async def _cleanup_managed_project_resources(
        self, project: dict
    ) -> tuple[list[str], list[str]]:
        import shutil
        from ..config import load_config

        notes: list[str] = []
        failures: list[str] = []

        if not project.get("managed_by_manager"):
            return notes, failures

        repo_path_raw = project.get("managed_repo_path")
        current_path_raw = project.get("path")
        if repo_path_raw:
            repo_path = Path(repo_path_raw)
            if current_path_raw and Path(current_path_raw) != repo_path:
                notes.append("left repo on disk because the project path was changed later")
            elif repo_path.exists():
                try:
                    shutil.rmtree(repo_path)
                    notes.append(f"deleted repo at {repo_path}")
                except Exception as exc:
                    failures.append(f"delete repo {repo_path}: {exc}")

        bot_username = (project.get("managed_bot_username") or "").lstrip("@")
        if bot_username:
            try:
                BotFatherClient = _load_botfather_dependency()
                cfg_path = self._project_config_path or DEFAULT_CONFIG
                config = load_config(cfg_path)
                bfc = BotFatherClient(
                    api_id=config.telegram_api_id,
                    api_hash=config.telegram_api_hash,
                    session_path=cfg_path.parent / "telethon.session",
                    client=getattr(self, "_telethon_client", None),
                )
                await bfc.delete_bot(bot_username)
                notes.append(f"deleted @{bot_username} via BotFather")
            except ImportError:
                failures.append(
                    "could not delete the Telegram bot automatically "
                    "(install link-project-to-chat[create] for BotFather cleanup)"
                )
            except Exception as exc:
                failures.append(f"delete @{bot_username} via BotFather: {exc}")

        return notes, failures

    def _users_config_path(self):
        """Resolve the config path for manager auth ops.

        ``self._project_config_path`` may be None (manager bot constructed
        without an explicit path → use the default). Passing None to
        ``load_config`` / ``save_config`` would TypeError.
        """
        from ..config import DEFAULT_CONFIG
        return self._project_config_path or DEFAULT_CONFIG

    async def _persist_auth_if_dirty(self) -> None:
        """Persist ``_allowed_users`` to disk if a first-contact lock was added.

        Manager bot's equivalent of ``ProjectBot._persist_auth_if_dirty``.
        Always writes to the GLOBAL ``Config.allowed_users`` (the manager bot
        has no project-scoped state). Uses the atomic ``locked_config_rmw``
        context manager from config.py so concurrent first-contacts converge.

        Pre-Step-3 (legacy ``AuthMixin``), ``_auth_dirty`` may not be set by
        any code path — the helper is a no-op and safe to call. Post-Step-3,
        it persists the append performed by ``_get_user_role``.
        """
        if not getattr(self, "_auth_dirty", False):
            return
        from ..config import locked_config_rmw, save_config_within_lock
        cfg_path = self._users_config_path()
        try:
            with locked_config_rmw(cfg_path) as disk:
                in_memory_by_user = {u.username: u for u in self._allowed_users}
                for au in disk.allowed_users:
                    mem = in_memory_by_user.get(au.username)
                    if mem is None:
                        continue
                    merged = list(au.locked_identities)
                    for ident in mem.locked_identities:
                        if ident not in merged:
                            merged.append(ident)
                    au.locked_identities = merged
                save_config_within_lock(disk, cfg_path)
            self._auth_dirty = False
        except Exception:
            logger.exception("Failed to persist manager auth state; will retry on next message")

    async def _guard(self, update: Update) -> bool:
        """Returns True if the user is authorized and not rate-limited.

        Shim around the transport-native _guard_invocation: wizard entry points
        still receive an Update from ConversationHandler, but the reply path
        goes through the Transport so no telegram-native reply_text call
        remains in manager/bot.py.

        Derives the reply ``ChatRef`` directly from ``update.effective_chat``
        rather than routing through ``_incoming_from_update``, because the
        latter unconditionally reads fields off ``effective_user`` and would
        raise on None (anonymous channel admins, service messages, etc.).
        """
        from ..transport.telegram import (
            chat_ref_from_telegram,
            identity_from_telegram_user,
        )
        user = update.effective_user
        chat = chat_ref_from_telegram(update.effective_chat) if update.effective_chat else None
        try:
            if not user:
                if chat is not None:
                    await self._transport.send_text(chat, "Unauthorized.")
                return False
            identity = identity_from_telegram_user(user)
            if not self._auth_identity(identity):
                if chat is not None:
                    await self._transport.send_text(chat, "Unauthorized.")
                return False
            if self._rate_limited(self._identity_key(identity)):
                if chat is not None:
                    await self._transport.send_text(chat, "Rate limited. Try again shortly.")
                return False
            return True
        finally:
            # Step 3 makes _auth_identity → _get_user_role append a first-contact
            # identity; persist before returning regardless of allow/deny branch.
            # Pre-Step-3 this is a no-op (see _persist_auth_if_dirty docstring).
            await self._persist_auth_if_dirty()

    async def _guard_executor(self, update: Update) -> bool:
        """Auth + rate-limit + executor gate for state-changing wizard entry
        points (``/add_project``, ``/edit_project``, ``/create_project``,
        ``/create_team``, ``/delete_team``).

        Wizard handlers receive an ``Update`` from PTB's ConversationHandler
        and must guard via this helper so a viewer cannot kick off a write
        wizard (each follow-up step would otherwise run unchecked once the
        conversation is in flight).
        """
        from ..transport.telegram import (
            chat_ref_from_telegram,
            identity_from_telegram_user,
        )
        if not await self._guard(update):
            return False
        user = update.effective_user
        if not user:
            return False
        identity = identity_from_telegram_user(user)
        if not self._require_executor(identity):
            chat = chat_ref_from_telegram(update.effective_chat) if update.effective_chat else None
            if chat is not None:
                await self._transport.send_text(
                    chat,
                    "Read-only access — only executors can run this command.",
                )
            return False
        return True

    async def _guard_invocation(self, invocation: "CommandInvocation") -> bool:
        """Transport-native counterpart of _guard.

        Sends replies via the transport and reads auth state from the
        CommandInvocation's Identity. Preserves the same Unauthorized./
        Rate limited. reply contract as _guard.
        """
        sender = invocation.sender
        if not sender or not self._auth_identity(sender):
            await self._transport.send_text(invocation.chat, "Unauthorized.")
            return False
        if self._rate_limited(self._identity_key(sender)):
            await self._transport.send_text(invocation.chat, "Rate limited. Try again shortly.")
            return False
        return True

    async def _guard_executor_invocation(self, invocation: "CommandInvocation") -> bool:
        """Auth + rate-limit + executor gate for state-changing manager commands.

        Returns False (after sending an appropriate reply) if the caller is
        unauthorized, rate-limited, OR a viewer trying to run a write command.
        Viewers may use read-only commands (e.g. ``/projects``, ``/teams``,
        ``/users``, ``/help``, ``/version``), but every state-changing path
        (``/start_all``, ``/stop_all``, ``/setup``, ``/model``, etc.) must
        gate through this helper or its button-side sibling
        ``_require_executor_button``.
        """
        if not await self._guard_invocation(invocation):
            return False
        if not self._require_executor(invocation.sender):
            await self._transport.send_text(
                invocation.chat,
                "Read-only access — only executors can run this command.",
            )
            return False
        return True

    async def _require_executor_button(self, click: "ButtonClick") -> bool:
        """Executor gate for state-changing manager buttons.

        Auth was already checked by ``_dispatch_button_click``; this helper just
        adds the role check. Returns False (after sending a reply that threads
        under the original keyboard via ``reply_to=click.message``) if the
        caller is a viewer. Use at the top of every button branch that mutates
        state (proj_start_*, proj_stop_*, proj_remove_*, proj_edit_*,
        proj_efld_*, proj_model_*, proj_rig_*, global_model_*, team_start_*,
        team_stop_*, setup_*, proj_ptog_*).
        """
        if not self._require_executor(click.sender):
            assert self._transport is not None
            await self._transport.send_text(
                click.chat,
                "Read-only access — only executors can run this action.",
                reply_to=click.message,
            )
            return False
        return True

    def _incoming_from_update(self, update) -> "IncomingMessage":
        """Build a transient IncomingMessage from a telegram Update.

        Used by wizard step bodies (Tasks 11-14) to read message data through the
        Transport-shaped contract while ConversationHandler still consumes Updates
        at the boundary.
        """
        from ..transport import IncomingMessage
        from ..transport.telegram import (
            chat_ref_from_telegram,
            identity_from_telegram_user,
            message_ref_from_telegram,
        )
        msg = update.effective_message
        chat = chat_ref_from_telegram(update.effective_chat)
        if msg is not None:
            message_ref = message_ref_from_telegram(msg)
        else:
            message_ref = MessageRef(
                transport_id=chat.transport_id, native_id="0", chat=chat,
            )
        return IncomingMessage(
            chat=chat,
            sender=identity_from_telegram_user(update.effective_user),
            text=(msg.text if msg else "") or "",
            files=[],
            reply_to=None,
            message=message_ref,
            native=msg,
        )

    def _msg_ref_from_query(self, query) -> MessageRef:
        """Build a MessageRef from a telegram CallbackQuery's ``.message``.

        Used by wizard-internal CallbackQueryHandler bodies (Task 14): the handler
        itself must stay PTB-typed because ConversationHandler routes by state,
        but its BODY can edit the underlying message through the Transport.
        """
        from ..transport.telegram import message_ref_from_telegram
        return message_ref_from_telegram(query.message)

    def _projects_text(self) -> str:
        projects = self._pm.list_all()
        running = sum(1 for _, st in projects if st == "running")
        return f"Projects ({running}/{len(projects)} running):"

    def _list_buttons(self) -> Buttons | None:
        """Produce the transport-native project list keyboard."""
        projects = self._pm.list_all()
        if not projects:
            return None
        return Buttons(rows=[
            [Button(
                label=f"{'[+]' if status == 'running' else '[-]'} {name}",
                value=f"proj_info_{name}",
            )]
            for name, status in projects
        ])

    async def _on_projects_from_transport(self, invocation: "CommandInvocation") -> None:
        """Transport-native handler for /projects."""
        if not await self._guard_invocation(invocation):
            return
        buttons = self._list_buttons()
        await self._transport.send_text(
            invocation.chat,
            self._projects_text() if buttons else "No projects configured.",
            buttons=buttons,
        )

    async def _on_start_all_from_transport(self, invocation: "CommandInvocation") -> None:
        """Transport-native handler for /start_all."""
        if not await self._guard_executor_invocation(invocation):
            return
        count = self._pm.start_all()
        await self._transport.send_text(invocation.chat, f"Started {count} project(s).")

    async def _on_stop_all_from_transport(self, invocation: "CommandInvocation") -> None:
        """Transport-native handler for /stop_all."""
        if not await self._guard_executor_invocation(invocation):
            return
        count = self._pm.stop_all()
        await self._transport.send_text(invocation.chat, f"Stopped {count} project(s).")

    def _load_teams(self) -> dict:
        from ..config import load_config
        return load_config(self._project_config_path or DEFAULT_CONFIG).teams

    def _team_running_count(self, team_name: str, team) -> int:
        return sum(
            1 for role in team.bots
            if self._pm.status(f"team:{team_name}:{role}") == "running"
        )

    def _team_detail_text(self, team_name: str, team) -> str:
        lines = [f"Team '{team_name}':"]
        for role in sorted(team.bots):
            status = self._pm.status(f"team:{team_name}:{role}")
            lines.append(f"  {role}: {status}")
        return "\n".join(lines)

    def _teams_list_buttons(self) -> Buttons | None:
        """Produce the transport-native team list keyboard."""
        teams = self._load_teams()
        if not teams:
            return None
        rows = []
        for team_name in sorted(teams):
            team = teams[team_name]
            running = self._team_running_count(team_name, team)
            total = len(team.bots)
            rows.append([Button(
                label=f"[{running}/{total}] {team_name}",
                value=f"team_info_{team_name}",
            )])
        return Buttons(rows=rows)

    async def _on_teams_from_transport(self, invocation: "CommandInvocation") -> None:
        """Transport-native handler for /teams."""
        if not await self._guard_invocation(invocation):
            return
        buttons = self._teams_list_buttons()
        if buttons is None:
            await self._transport.send_text(
                invocation.chat,
                "No teams configured. Use /create_team to create one.",
            )
            return
        teams = self._load_teams()
        await self._transport.send_text(
            invocation.chat,
            f"Teams ({len(teams)}):",
            buttons=buttons,
        )

    def _global_model_buttons(self) -> Buttons:
        """Produce the transport-native global default-model keyboard."""
        from ..config import load_config
        current = load_config(self._project_config_path or DEFAULT_CONFIG).default_model_claude
        rows = []
        for model_id, label in MODEL_OPTIONS:
            prefix = "● " if current == model_id else ""
            rows.append([Button(label=f"{prefix}{label}", value=f"global_model_{model_id}")])
        return Buttons(rows=rows)

    async def _on_model_from_transport(self, invocation: "CommandInvocation") -> None:
        """Transport-native handler for /model.

        Renders the keyboard that ultimately writes ``default_model_claude``,
        so the entry point itself is executor-only — viewers shouldn't see
        a writable picker.
        """
        if not await self._guard_executor_invocation(invocation):
            return
        from ..config import load_config
        current = load_config(self._project_config_path or DEFAULT_CONFIG).default_model_claude
        label = next((l for m, l in MODEL_OPTIONS if m == current), current or "not set")
        await self._transport.send_text(
            invocation.chat,
            f"Default model: {label}\nApplies to projects without a per-project model override.",
            buttons=self._global_model_buttons(),
        )

    async def _on_version_from_transport(self, invocation: "CommandInvocation") -> None:
        """Transport-native handler for /version."""
        if not await self._guard_invocation(invocation):
            return
        from .. import __version__
        await self._transport.send_text(
            invocation.chat, f"link-project-to-chat v{__version__}"
        )

    async def _on_help_from_transport(self, invocation: "CommandInvocation") -> None:
        """Transport-native handler for /help."""
        if not await self._guard_invocation(invocation):
            return
        await self._transport.send_text(
            invocation.chat,
            "\n".join(f"/{name} - {desc}" for name, desc in COMMANDS),
        )

    # ConversationHandler states for /add_project
    ADD_NAME, ADD_PATH, ADD_TOKEN, ADD_USERNAME, ADD_MODEL = range(5)

    # ConversationHandler states for /create_project
    CREATE_SOURCE, CREATE_REPO_LIST, CREATE_REPO_URL, CREATE_NAME, CREATE_NAME_INPUT, CREATE_BOT, CREATE_CLONE = range(11, 18)

    # ConversationHandler states for /create_team
    (
        CREATE_TEAM_SOURCE,
        CREATE_TEAM_REPO_LIST,
        CREATE_TEAM_REPO_URL,
        CREATE_TEAM_NAME,
        CREATE_TEAM_PERSONA_MGR,
        CREATE_TEAM_PERSONA_DEV,
    ) = range(18, 24)

    # ConversationHandler state for /delete_team (single confirmation step)
    DELETE_TEAM_CONFIRM = 24

    # ConversationHandler state for the GitLab provider picker. Shared between
    # the /create_project and /create_team wizards: each ConversationHandler is
    # scoped per-handler, so a single state int suffices.
    CREATE_PROVIDER_PICK = 25

    async def _on_add_project(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        if not await self._guard_executor(update):
            return ConversationHandler.END
        incoming = self._incoming_from_update(update)
        ctx.user_data["new_project"] = {}
        await self._transport.send_text(
            incoming.chat,
            "Let's add a new project.\n\nWhat is the project name?",
        )
        return self.ADD_NAME

    async def _add_name(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        incoming = self._incoming_from_update(update)
        name = incoming.text.strip()
        if name in self._load_projects():
            await self._transport.send_text(
                incoming.chat,
                f"Project '{name}' already exists. Try a different name:",
            )
            return self.ADD_NAME
        ctx.user_data["new_project"]["name"] = name
        await self._transport.send_text(incoming.chat, "Enter the project path:")
        return self.ADD_PATH

    async def _add_path(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        incoming = self._incoming_from_update(update)
        path = incoming.text.strip()
        if not Path(path).exists():
            await self._transport.send_text(
                incoming.chat,
                f"Path does not exist: {path}\nTry again:",
            )
            return self.ADD_PATH
        ctx.user_data["new_project"]["path"] = path
        await self._transport.send_text(
            incoming.chat,
            "Enter the Telegram bot token:",
        )
        return self.ADD_TOKEN

    async def _add_token(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        incoming = self._incoming_from_update(update)
        text = incoming.text.strip()
        if text == "/skip" or not text:
            await self._transport.send_text(
                incoming.chat,
                "Telegram bot token is required. Enter the token:",
            )
            return self.ADD_TOKEN
        ctx.user_data["new_project"]["telegram_bot_token"] = text
        await self._transport.send_text(
            incoming.chat,
            "Enter the allowed username (or /skip):",
        )
        return self.ADD_USERNAME

    async def _add_username(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        incoming = self._incoming_from_update(update)
        text = incoming.text.strip()
        if text != "/skip":
            # Write to the modern ``allowed_users`` shape; the legacy flat
            # ``username`` key would collide with any pre-existing list on
            # next load (explicit list wins, the new user is silently
            # dropped — see _migrate_legacy_auth's effective = explicit_proj
            # or migrated_proj precedence in config.py).
            norm = text.lower().lstrip("@")
            ctx.user_data["new_project"]["allowed_users"] = [
                {"username": norm, "role": "executor"}
            ]
        await self._transport.send_text(
            incoming.chat,
            "Enter the model name (or /skip):",
        )
        return self.ADD_MODEL

    async def _add_model(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        incoming = self._incoming_from_update(update)
        text = incoming.text.strip()
        model_value = text if text != "/skip" else None
        data = ctx.user_data.pop("new_project", {})
        # Drop any stale flat ``model`` from earlier user_data shapes; we now
        # write the new backend/backend_state shape directly.
        data.pop("model", None)
        name = data.pop("name", None)
        if not name:
            await self._transport.send_text(incoming.chat, "Something went wrong. Try again.")
            return ConversationHandler.END
        # Canonical post-v1.0 shape: backend + backend_state["claude"][*].
        # The pre-v1.0 top-level ``model`` mirror was kept one release for
        # downgrade safety; v1.0.0 stopped emitting it.
        data["backend"] = "claude"
        if model_value:
            data["backend_state"] = {"claude": {"model": model_value}}
        else:
            data["backend_state"] = {}
        projects = self._load_projects()
        projects[name] = data
        self._save_projects(projects)
        await self._transport.send_text(incoming.chat, f"Added project '{name}'.")
        return ConversationHandler.END

    # ------------------------------------------------------------------
    # User-management commands (Task 6). Operate on the GLOBAL
    # ``Config.allowed_users`` allow-list; replace the pre-v1.0 single-
    # username-per-call legacy handlers (``_on_users_from_transport`` etc.)
    # whose write paths edited the same list with weaker role semantics.
    # All write commands gate to executor role via
    # ``_require_executor_or_reply``; ``/users`` is viewer-allowed.
    # ------------------------------------------------------------------
    def _load_config_for_users(self):
        """Helper: load the global config (uses _users_config_path from Task 5 Step 2b)."""
        from ..config import load_config
        return load_config(self._users_config_path())

    def _save_config_for_users(self, cfg) -> None:
        from ..config import save_config
        save_config(cfg, self._users_config_path())
        # Refresh our own in-memory allow-list to match.
        self._allowed_users = list(cfg.allowed_users)

    def _format_users_list(self, users) -> str:
        if not users:
            return "No users authorized."
        lines = ["Authorized users:"]
        for u in users:
            locked = f"[identities: {', '.join(u.locked_identities)}]" if u.locked_identities else "[not yet]"
            lines.append(f"  • {u.username} ({u.role}) {locked}")
        return "\n".join(lines)

    async def _require_executor_or_reply(self, ci: "CommandInvocation") -> bool:
        """Common gate for write commands. Auth + rate-limit + executor role enforcement.

        Calling ``_auth_identity`` may append a first-contact identity to a
        user's locked_identities. The persist helper added in Task 5 Step 2b
        (``_persist_auth_if_dirty``) covers both allow and deny branches via
        the try/finally below.

        Mirrors the contract from ``_guard_invocation``: unauthenticated
        callers receive ``Unauthorized.``, rate-limited callers receive
        ``Rate limited. Try again shortly.``, and unauthorised role gets a
        read-only message.
        """
        try:
            if not self._auth_identity(ci.sender):
                await self._transport.send_text(
                    ci.chat,
                    "Unauthorized.",
                    reply_to=ci.message,
                )
                return False
            if self._rate_limited(self._identity_key(ci.sender)):
                await self._transport.send_text(
                    ci.chat,
                    "Rate limited. Try again shortly.",
                    reply_to=ci.message,
                )
                return False
            if not self._require_executor(ci.sender):
                await self._transport.send_text(
                    ci.chat,
                    "Read-only access — only executors can edit the allow-list.",
                    reply_to=ci.message,
                )
                return False
            return True
        finally:
            # Always persist any first-contact lock from the _auth_identity
            # call above. Covers both allow and deny branches.
            await self._persist_auth_if_dirty()

    async def _on_users(self, ci: "CommandInvocation") -> None:
        """List all authorized users (viewer-allowed; read-only).

        Symmetric with ``_guard_invocation``: unauthenticated callers receive
        an ``Unauthorized.`` reply, rate-limited callers receive a
        ``Rate limited. Try again shortly.`` reply.
        """
        # /users LIST is viewer-allowed (read-only). But _auth_identity may
        # still append a first-contact lock; persist before returning.
        try:
            if not self._auth_identity(ci.sender):
                await self._transport.send_text(
                    ci.chat,
                    "Unauthorized.",
                    reply_to=ci.message,
                )
                return
            if self._rate_limited(self._identity_key(ci.sender)):
                await self._transport.send_text(
                    ci.chat,
                    "Rate limited. Try again shortly.",
                    reply_to=ci.message,
                )
                return
            cfg = self._load_config_for_users()
            await self._transport.send_text(
                ci.chat, self._format_users_list(cfg.allowed_users), reply_to=ci.message,
            )
        finally:
            await self._persist_auth_if_dirty()

    def _restart_running_bots_for_user_mutation(self) -> list[str]:
        """Stop+start every currently-running bot under this manager.

        Project bots cache `_allowed_users` at startup, so a /demote_user,
        /remove_user, /promote_user, /add_user, or /reset_user_identity does
        not change a running bot's authorization view until a restart. This
        helper, invoked at the tail of every user-mutation command, makes the
        change effective immediately. Transport-agnostic (Telegram bots have
        the same staleness as web bots) and team-bot-aware: uses
        ProcessManager.list_running() (which surfaces team:NAME:ROLE keys
        that list_all() does not) and ProcessManager.restart() (which
        dispatches team keys through start_team).

        Returns the list of bot keys actually restarted so callers can surface
        the count in the operator reply.
        """
        restarted: list[str] = []
        if getattr(self, "_pm", None) is None:
            return restarted
        for key in list(self._pm.list_running()):
            if self._pm.restart(key):
                restarted.append(key)
        return restarted

    async def _on_add_user(self, ci: "CommandInvocation") -> None:
        """/add_user <username> [viewer|executor] — default executor."""
        if not await self._require_executor_or_reply(ci):
            return
        if not ci.args:
            await self._transport.send_text(
                ci.chat,
                "Usage: /add_user <username> [viewer|executor]",
                reply_to=ci.message,
            )
            return
        username = ci.args[0].lstrip("@").lower()
        role = ci.args[1] if len(ci.args) > 1 else "executor"
        if role not in ("viewer", "executor"):
            await self._transport.send_text(
                ci.chat,
                f"Invalid role {role!r}. Use 'viewer' or 'executor'.",
                reply_to=ci.message,
            )
            return
        cfg = self._load_config_for_users()
        existing = next((u for u in cfg.allowed_users if u.username == username), None)
        if existing:
            existing.role = role
        else:
            cfg.allowed_users.append(AllowedUser(username=username, role=role))
        self._save_config_for_users(cfg)
        restarted = self._restart_running_bots_for_user_mutation()
        await self._transport.send_text(
            ci.chat,
            self._format_users_list(cfg.allowed_users)
            + (f"\n\nRestarted {len(restarted)} running bot(s) to apply." if restarted else ""),
            reply_to=ci.message,
        )

    async def _on_remove_user(self, ci: "CommandInvocation") -> None:
        """/remove_user <username> — drop the user and any locked identities."""
        if not await self._require_executor_or_reply(ci):
            return
        if not ci.args:
            await self._transport.send_text(
                ci.chat, "Usage: /remove_user <username>", reply_to=ci.message,
            )
            return
        username = ci.args[0].lstrip("@").lower()
        cfg = self._load_config_for_users()
        cfg.allowed_users = [u for u in cfg.allowed_users if u.username != username]
        self._save_config_for_users(cfg)
        restarted = self._restart_running_bots_for_user_mutation()
        await self._transport.send_text(
            ci.chat,
            self._format_users_list(cfg.allowed_users)
            + (f"\n\nRestarted {len(restarted)} running bot(s) to apply." if restarted else ""),
            reply_to=ci.message,
        )

    async def _set_role(self, ci: "CommandInvocation", new_role: str) -> None:
        """Shared body for /promote_user and /demote_user.

        ``new_role`` is the role to assign — "executor" or "viewer". The
        command name for the usage hint is derived from new_role:
            executor → "/promote_user"
            viewer   → "/demote_user"
        """
        if not await self._require_executor_or_reply(ci):
            return
        cmd_name = "promote_user" if new_role == "executor" else "demote_user"
        if not ci.args:
            await self._transport.send_text(
                ci.chat, f"Usage: /{cmd_name} <username>", reply_to=ci.message,
            )
            return
        username = ci.args[0].lstrip("@").lower()
        cfg = self._load_config_for_users()
        u = next((x for x in cfg.allowed_users if x.username == username), None)
        if not u:
            await self._transport.send_text(
                ci.chat, f"User {username!r} not found.", reply_to=ci.message,
            )
            return
        u.role = new_role
        self._save_config_for_users(cfg)
        restarted = self._restart_running_bots_for_user_mutation()
        await self._transport.send_text(
            ci.chat,
            self._format_users_list(cfg.allowed_users)
            + (f"\n\nRestarted {len(restarted)} running bot(s) to apply." if restarted else ""),
            reply_to=ci.message,
        )

    async def _on_promote_user(self, ci: "CommandInvocation") -> None:
        """/promote_user <username> — set role to 'executor'."""
        await self._set_role(ci, "executor")

    async def _on_demote_user(self, ci: "CommandInvocation") -> None:
        """/demote_user <username> — set role to 'viewer'."""
        await self._set_role(ci, "viewer")

    async def _on_reset_user_identity(self, ci: "CommandInvocation") -> None:
        """/reset_user_identity <username> [transport_id] — clear locks."""
        if not await self._require_executor_or_reply(ci):
            return
        if not ci.args:
            await self._transport.send_text(
                ci.chat,
                "Usage: /reset_user_identity <username> [transport_id]",
                reply_to=ci.message,
            )
            return
        username = ci.args[0].lstrip("@").lower()
        transport_filter = ci.args[1] if len(ci.args) > 1 else None
        cfg = self._load_config_for_users()
        u = next((x for x in cfg.allowed_users if x.username == username), None)
        if not u:
            await self._transport.send_text(
                ci.chat, f"User {username!r} not found.", reply_to=ci.message,
            )
            return
        if transport_filter:
            u.locked_identities = [
                ident for ident in u.locked_identities
                if not ident.startswith(f"{transport_filter}:")
            ]
        else:
            u.locked_identities = []
        self._save_config_for_users(cfg)
        restarted = self._restart_running_bots_for_user_mutation()
        await self._transport.send_text(
            ci.chat,
            self._format_users_list(cfg.allowed_users)
            + (f"\n\nRestarted {len(restarted)} running bot(s) to apply." if restarted else ""),
            reply_to=ci.message,
        )

    async def _on_setup_from_transport(self, invocation: "CommandInvocation") -> None:
        """Transport-native handler for /setup.

        Setup is a write surface — every button in the keyboard arms one of
        the ``setup_awaiting`` text-input branches that persist API tokens,
        Telethon credentials, etc. Gate the entry point to executor so a
        viewer can't even open the keyboard.
        """
        if not await self._guard_executor_invocation(invocation):
            return
        from ..config import load_config
        path = self._project_config_path or DEFAULT_CONFIG
        config = load_config(path)

        lines = ["Setup status:"]
        lines.append(f"  GitHub PAT: {'configured' if config.github_pat else 'not set'}")
        lines.append(f"  Telegram API ID: {'configured' if config.telegram_api_id else 'not set'}")
        lines.append(f"  Telegram API Hash: {'configured' if config.telegram_api_hash else 'not set'}")
        session_path = path.parent / "telethon.session"
        lines.append(f"  Telethon session: {'exists' if session_path.exists() else 'not authenticated'}")
        lines.append(f"  Voice STT: {config.stt_backend or 'disabled'}")

        rows: list[list[Button]] = []
        rows.append([Button(label="Set GitHub Token", value="setup_gh")])
        rows.append([Button(label="Set Telegram API", value="setup_api")])
        if config.telegram_api_id and config.telegram_api_hash:
            rows.append([Button(label="Authenticate Telethon", value="setup_telethon")])
        rows.append([Button(label="Set Voice STT", value="setup_voice")])
        rows.append([Button(label="Done", value="setup_done")])

        # Stash the config path on PTB's per-user storage so the subsequent
        # setup_* callback handlers (still PTB-native in this task) can read it.
        # Moving the follow-up handlers off PTB is out of scope for spec #0c
        # Task 9; until then we reuse the existing ctx.user_data slot via the
        # Update/ctx pair the Telegram transport stashes in invocation.native.
        native = invocation.native
        if isinstance(native, tuple) and len(native) >= 2:
            ctx = native[1]
            user_data = getattr(ctx, "user_data", None)
            if user_data is not None:
                user_data["setup_config_path"] = str(path)

        await self._transport.send_text(
            invocation.chat, "\n".join(lines), buttons=Buttons(rows=rows)
        )

    async def _on_edit_project(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard_executor(update):
            return
        incoming = self._incoming_from_update(update)
        if not ctx.args or len(ctx.args) < 3:
            await self._transport.send_text(
                incoming.chat,
                f"Usage: /edit_project <name> <field> <value>\nFields: {', '.join(_EDITABLE_FIELDS)}",
            )
            return
        name, field, value = ctx.args[0], ctx.args[1], " ".join(ctx.args[2:])
        await self._apply_edit(incoming.chat, name, field, value)

    async def _apply_edit(self, chat: ChatRef, name: str, field: str, value: str) -> None:
        """Apply a field edit and send a confirmation reply."""
        projects = self._load_projects()
        if name not in projects:
            await self._transport.send_text(chat, f"Project '{name}' not found.")
            return

        if field == "path":
            if not Path(value).exists():
                await self._transport.send_text(chat, f"Path does not exist: {value}")
                return
            projects[name]["path"] = value
            self._save_projects(projects)
            await self._transport.send_text(chat, f"Updated '{name}' path to {value}.")
        elif field == "name":
            if value in projects:
                await self._transport.send_text(chat, f"Project '{value}' already exists.")
                return
            projects[value] = projects.pop(name)
            self._save_projects(projects)
            self._pm.rename(name, value)
            await self._transport.send_text(chat, f"Renamed '{name}' to '{value}'.")
        elif field == "token":
            projects[name]["telegram_bot_token"] = value
            self._save_projects(projects)
            await self._transport.send_text(chat, f"Updated '{name}' token.")
        elif field == "username":
            # Translate to the modern ``allowed_users`` shape so the write
            # actually authorizes the new user. The legacy flat ``username``
            # key would silently lose to any pre-existing ``allowed_users``
            # list on next load: the loader's
            # ``effective = explicit_proj or migrated_proj`` precedence only
            # falls back to the legacy migration when the explicit list is
            # empty.
            norm = value.lower().lstrip("@")
            projects[name]["allowed_users"] = [{"username": norm, "role": "executor"}]
            projects[name].pop("username", None)
            self._save_projects(projects)
            await self._transport.send_text(
                chat,
                f"Updated '{name}' allowed_users to [{norm}] (executor). "
                f"For multi-user lists use /add_user / /remove_user.",
            )
        elif field in ("model", "permissions"):
            # Phase 2: route Claude-shaped fields through backend_state so
            # multi-backend configs don't lose state on subsequent saves. The
            # legacy flat key is still mirrored back onto the project entry by
            # patch_backend_state for downgrade safety.
            backend_name = projects[name].get("backend") or "claude"
            patch_backend_state(
                name,
                backend_name,
                {field: value},
                self._project_config_path or DEFAULT_CONFIG,
            )
            await self._transport.send_text(chat, f"Updated '{name}' {field} to {value}.")
        elif field == "respond_in_groups":
            parsed = parse_user_bool(value)
            if parsed is True:
                projects[name]["respond_in_groups"] = True
                self._save_projects(projects)
                await self._transport.send_text(
                    chat, f"Updated '{name}' respond_in_groups to True.",
                )
            elif parsed is False:
                projects[name].pop("respond_in_groups", None)
                self._save_projects(projects)
                await self._transport.send_text(
                    chat, f"Updated '{name}' respond_in_groups to False.",
                )
            else:
                await self._transport.send_text(
                    chat,
                    f"Invalid bool for respond_in_groups: {value!r}. "
                    f"Use one of: true, false, 1, 0, yes, no, on, off.",
                )
        elif field == "safety_prompt":
            if value == "default":
                projects[name].pop("safety_prompt", None)
                self._save_projects(projects)
                await self._transport.send_text(
                    chat, f"Updated '{name}' safety_prompt → default (built-in guardrail).",
                )
            else:
                # Empty string and any non-"default" string are both written verbatim.
                projects[name]["safety_prompt"] = value
                self._save_projects(projects)
                if value == "":
                    await self._transport.send_text(
                        chat, f"Updated '{name}' safety_prompt → disabled.",
                    )
                else:
                    await self._transport.send_text(
                        chat, f"Updated '{name}' safety_prompt to custom text "
                        f"({len(value)} chars).",
                    )
        else:
            await self._transport.send_text(
                chat, f"Unknown field. Use: {', '.join(_EDITABLE_FIELDS)}",
            )

    async def _edit_field_save(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        from ..transport.telegram import identity_from_telegram_user
        try:
            # Task 12: the Add-Google-Chat wizard owns ``gchat_wizard``. Like
            # the ``setup_awaiting`` branch below it's defense-in-depth gated
            # on the executor role: the entry-point button is already gated
            # by _require_executor_button, but a viewer's PTB user_data could
            # carry a leaked ``gchat_wizard`` from a prior executor session.
            gchat_wizard = ctx.user_data.get("gchat_wizard")
            if gchat_wizard:
                user = update.effective_user
                if user is None:
                    ctx.user_data.pop("gchat_wizard", None)
                    return
                identity = identity_from_telegram_user(user)
                if not self._auth_identity(identity):
                    incoming = self._incoming_from_update(update)
                    ctx.user_data.pop("gchat_wizard", None)
                    await self._transport.send_text(incoming.chat, "Unauthorized.")
                    return
                if not self._require_executor(identity):
                    incoming = self._incoming_from_update(update)
                    ctx.user_data.pop("gchat_wizard", None)
                    await self._transport.send_text(
                        incoming.chat,
                        "Read-only access — only executors can configure Google Chat.",
                    )
                    return
                await self._handle_gchat_wizard_input(update, ctx, gchat_wizard)
                return
            # Handle setup text input first (intentionally NOT rate-limited —
            # users paste long tokens during onboarding and shouldn't get
            # throttled mid-wizard). If the manager later wants to throttle
            # setup text, gate it inside _handle_setup_input.
            setup_awaiting = ctx.user_data.get("setup_awaiting")
            if setup_awaiting:
                # Defense-in-depth: nobody should be able to write API tokens
                # or Telethon credentials except an authenticated executor,
                # even if setup_awaiting was somehow armed by PTB state left
                # over from a prior executor session. The /setup entry point
                # is already executor-gated, but text input lands here
                # regardless of who pressed the button — including
                # unauthenticated callers if state leaks across users.
                # Order matters: clear setup_awaiting on any non-executor
                # path so a single bad reply can't keep collecting writes.
                user = update.effective_user
                if user is None:
                    ctx.user_data.pop("setup_awaiting", None)
                    return
                identity = identity_from_telegram_user(user)
                if not self._auth_identity(identity):
                    incoming = self._incoming_from_update(update)
                    ctx.user_data.pop("setup_awaiting", None)
                    await self._transport.send_text(incoming.chat, "Unauthorized.")
                    return
                if not self._require_executor(identity):
                    incoming = self._incoming_from_update(update)
                    ctx.user_data.pop("setup_awaiting", None)
                    await self._transport.send_text(
                        incoming.chat,
                        "Read-only access — only executors can complete setup.",
                    )
                    return
                await self._handle_setup_input(update, ctx, setup_awaiting)
                return
            # Existing edit logic
            pending = ctx.user_data.get("pending_edit")
            if not pending:
                return
            user = update.effective_user
            if not user:
                return
            identity = identity_from_telegram_user(user)
            if not self._auth_identity(identity):
                return
            if self._rate_limited(self._identity_key(identity)):
                return
            # Defense-in-depth executor gate for project-field writes. The
            # `proj_efld_*` button entry point is also gated; this check
            # covers the path where a viewer's PTB session still carries
            # ``pending_edit`` from a prior executor click.
            if not self._require_executor(identity):
                incoming = self._incoming_from_update(update)
                ctx.user_data.pop("pending_edit", None)
                await self._transport.send_text(
                    incoming.chat,
                    "Read-only access — only executors can edit projects.",
                )
                return
            ctx.user_data.pop("pending_edit")
            incoming = self._incoming_from_update(update)
            await self._apply_edit(incoming.chat, pending["name"], pending["field"], incoming.text.strip())
        finally:
            # First-contact identity locks from _auth_identity must survive the
            # wizard path the same way transport-native commands persist them.
            await self._persist_auth_if_dirty()

    async def _handle_setup_input(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, awaiting: str) -> None:
        from ..config import load_config, save_config
        incoming = self._incoming_from_update(update)
        chat = incoming.chat
        text = incoming.text.strip()
        path = Path(ctx.user_data.get("setup_config_path", str(DEFAULT_CONFIG)))

        if awaiting == "github_pat":
            ctx.user_data.pop("setup_awaiting")
            config = load_config(path)
            config.github_pat = text
            save_config(config, path)
            await self._transport.send_text(chat, "GitHub PAT saved. Use /setup to continue.")

        elif awaiting == "api_id":
            try:
                api_id = int(text)
            except ValueError:
                await self._transport.send_text(chat, "Invalid. Enter a numeric API ID:")
                return
            ctx.user_data["setup_api_id"] = api_id
            ctx.user_data["setup_awaiting"] = "api_hash"
            await self._transport.send_text(chat, "Enter your Telegram API Hash:")

        elif awaiting == "api_hash":
            api_id = ctx.user_data.pop("setup_api_id", 0)
            ctx.user_data.pop("setup_awaiting")
            config = load_config(path)
            config.telegram_api_id = api_id
            config.telegram_api_hash = text
            save_config(config, path)
            await self._transport.send_text(
                chat, "Telegram API credentials saved. Use /setup to authenticate Telethon.",
            )

        elif awaiting == "phone":
            ctx.user_data["setup_phone"] = text
            ctx.user_data["setup_awaiting"] = "code"
            try:
                from ..botfather import BotFatherClient
                config = load_config(path)
                session_path = path.parent / "telethon.session"
                bf = BotFatherClient(config.telegram_api_id, config.telegram_api_hash, session_path)
                ctx.user_data["setup_bf_client"] = bf
                client = await bf._ensure_client()
                await client.send_code_request(text)
                # Telegram invalidates login codes typed verbatim into any chat
                # (security feature). Ask the user to obfuscate so the official
                # client doesn't pattern-match it; we strip non-digits below.
                await self._transport.send_text(
                    chat,
                    "Code sent to your Telegram. Enter the code with spaces "
                    "between digits (e.g. 1 2 3 4 5) — Telegram auto-expires "
                    "codes typed as plain digits.",
                )
            except Exception as e:
                ctx.user_data.pop("setup_awaiting", None)
                await self._transport.send_text(chat, f"Error: {e}")

        elif awaiting == "code":
            bf = ctx.user_data.get("setup_bf_client")
            phone = ctx.user_data.get("setup_phone")
            if not bf or not phone:
                ctx.user_data.pop("setup_awaiting", None)
                await self._transport.send_text(chat, "Session lost. Use /setup again.")
                return
            try:
                client = await bf._ensure_client()
                code = "".join(ch for ch in text if ch.isdigit())
                await client.sign_in(phone, code)
                self._adopt_setup_client(bf, client)
                ctx.user_data.pop("setup_awaiting")
                ctx.user_data.pop("setup_bf_client", None)
                ctx.user_data.pop("setup_phone", None)
                await self._transport.send_text(
                    chat, "Authenticated! You can now use /create_project.",
                )
            except Exception as e:
                if "Two-steps verification" in str(e) or "password" in str(e).lower():
                    ctx.user_data["setup_awaiting"] = "2fa"
                    await self._transport.send_text(chat, "2FA is enabled. Enter your password:")
                else:
                    ctx.user_data.pop("setup_awaiting", None)
                    await self._transport.send_text(chat, f"Auth failed: {e}")

        elif awaiting == "2fa":
            bf = ctx.user_data.get("setup_bf_client")
            if not bf:
                ctx.user_data.pop("setup_awaiting", None)
                await self._transport.send_text(chat, "Session lost. Use /setup again.")
                return
            try:
                client = await bf._ensure_client()
                await client.sign_in(password=text)
                self._adopt_setup_client(bf, client)
                ctx.user_data.pop("setup_awaiting")
                ctx.user_data.pop("setup_bf_client", None)
                ctx.user_data.pop("setup_phone", None)
                await self._transport.send_text(
                    chat, "Authenticated with 2FA! You can now use /create_project.",
                )
            except Exception as e:
                ctx.user_data.pop("setup_awaiting", None)
                await self._transport.send_text(chat, f"2FA auth failed: {e}")

        elif awaiting == "stt_backend":
            choice = text.strip().lower()
            if choice == "off":
                config = load_config(path)
                config.stt_backend = ""
                save_config(config, path)
                ctx.user_data.pop("setup_awaiting")
                await self._transport.send_text(chat, "Voice disabled. Use /setup to continue.")
            elif choice in ("whisper-api", "whisper-cli"):
                config = load_config(path)
                config.stt_backend = choice
                save_config(config, path)
                if choice == "whisper-api":
                    ctx.user_data["setup_awaiting"] = "openai_api_key"
                    await self._transport.send_text(chat, "Enter your OpenAI API key:")
                else:
                    ctx.user_data.pop("setup_awaiting")
                    await self._transport.send_text(
                        chat,
                        "whisper-cli configured. Make sure `whisper` is on PATH.\n"
                        "Use /setup to continue.",
                    )
            else:
                await self._transport.send_text(
                    chat, "Invalid. Type: whisper-api, whisper-cli, or off",
                )

        elif awaiting == "openai_api_key":
            ctx.user_data.pop("setup_awaiting")
            config = load_config(path)
            config.openai_api_key = text.strip()
            save_config(config, path)
            await self._transport.send_text(chat, "OpenAI API key saved. Use /setup to continue.")

    # ------------------------------------------------------------------
    # Task 12: Add-Google-Chat wizard.
    #
    # Stateful 4-step text-input flow analogous to ``setup_awaiting`` /
    # ``pending_edit`` — driven from the per-user PTB ``ctx.user_data``
    # dict via the existing ``_edit_field_save`` MessageHandler. The state
    # is stored under the key ``gchat_wizard`` and has the shape:
    #     {"name": <project>, "step": <step-id>, "data": {<collected>}}
    # Steps: "sa_file" → "port" → "public_url" → "root_command_id" →
    # finalize (persist + start subprocess + print nginx snippet).
    # ------------------------------------------------------------------

    async def _start_add_google_chat_wizard(
        self, click: "ButtonClick", ctx_user_data: dict | None, name: str,
    ) -> None:
        """Arm the wizard for ``name`` and prompt for the SA-file path.

        Caller (``_dispatch_button_click``) is responsible for the executor
        gate; this just initializes per-user state and renders the first
        prompt. The wizard state is keyed in PTB's ``ctx.user_data`` because
        every follow-up step arrives as a text message and is dispatched
        through ``_edit_field_save`` — which only has ``ctx.user_data`` as a
        per-user side channel. Transports without PTB state (e.g.
        FakeTransport) still get a working wizard because the test seeds the
        same dict and threads it across calls (see the wizard tests).
        """
        if ctx_user_data is None:
            await self._transport.edit_text(
                click.message,
                "Cannot start the Google Chat wizard — no per-user state slot.",
            )
            return
        ctx_user_data["gchat_wizard"] = {
            "name": name,
            "step": "sa_file",
            "data": {},
        }
        await self._transport.edit_text(
            click.message,
            (
                f"Add Google Chat for '{name}' — step 1 of 4.\n\n"
                "Enter the absolute path to the service-account JSON file:\n"
                "(/cancel to abort)"
            ),
        )

    async def _handle_gchat_wizard_input(
        self,
        update: Update,
        ctx: ContextTypes.DEFAULT_TYPE,
        wizard: dict,
    ) -> None:
        """Dispatch the wizard's current step on the inbound text.

        Auth + executor gating is enforced by ``_edit_field_save`` before it
        reaches us. We dispatch on ``wizard["step"]`` rather than a state
        enum because the wizard only has 4 states and the dispatch table
        would dominate the file footprint.
        """
        incoming = self._incoming_from_update(update)
        chat = incoming.chat
        text = incoming.text.strip()

        if text == "/cancel":
            ctx.user_data.pop("gchat_wizard", None)
            await self._transport.send_text(chat, "Google Chat wizard cancelled.")
            return

        step = wizard.get("step")
        if step == "sa_file":
            await self._gchat_step_sa_file(wizard, chat, text)
        elif step == "port":
            await self._gchat_step_port(wizard, chat, text)
        elif step == "public_url":
            await self._gchat_step_public_url(wizard, chat, text)
        elif step == "root_command_id":
            await self._gchat_step_root_command_id(ctx, wizard, chat, text)
        else:
            # Defensive: unknown step — drop the wizard so a stale shape
            # can't keep collecting writes.
            ctx.user_data.pop("gchat_wizard", None)
            await self._transport.send_text(
                chat,
                "Google Chat wizard is in an unknown state. Start over via the project view.",
            )

    def _gchat_is_keep(self, wizard: dict, text: str) -> bool:
        """``/keep`` preserves the prefilled value in edit mode (Task 13).

        Only meaningful when the wizard was opened from the Edit button: the
        starter prepopulates ``wizard["data"]`` with the current override
        values, so ``/keep`` just advances without overwriting. The Add
        wizard ignores ``/keep`` and falls through to normal validation
        (which will reject ``/keep`` as an invalid path/port/URL/int).
        """
        return wizard.get("kind") == "edit" and text == "/keep"

    async def _gchat_step_sa_file(self, wizard: dict, chat, text: str) -> None:
        if self._gchat_is_keep(wizard, text):
            wizard["step"] = "port"
            await self._gchat_prompt_port(wizard, chat)
            return
        if not text:
            await self._transport.send_text(
                chat, "Enter the absolute path to the service-account JSON file:",
            )
            return
        wizard["data"]["service_account_file"] = text
        wizard["step"] = "port"
        await self._gchat_prompt_port(wizard, chat)

    async def _gchat_prompt_port(self, wizard: dict, chat) -> None:
        if wizard.get("kind") == "edit":
            await self._transport.send_text(
                chat,
                "Step 2 of 4 — enter the port for this Google Chat bot (1..65535),\n"
                f"or `/keep` to keep the current value ({wizard['data'].get('port')}):",
            )
        else:
            await self._transport.send_text(
                chat,
                "Step 2 of 4 — enter the port for this Google Chat bot (1..65535):",
            )

    async def _gchat_step_port(self, wizard: dict, chat, text: str) -> None:
        if self._gchat_is_keep(wizard, text):
            wizard["step"] = "public_url"
            await self._gchat_prompt_public_url(wizard, chat)
            return
        try:
            port = int(text)
        except ValueError:
            await self._transport.send_text(
                chat, "Invalid. Enter a numeric port in 1..65535:",
            )
            return
        if not 1 <= port <= 65535:
            await self._transport.send_text(
                chat, f"Port {port} out of range. Enter a port in 1..65535:",
            )
            return
        wizard["data"]["port"] = port
        wizard["step"] = "public_url"
        await self._gchat_prompt_public_url(wizard, chat)

    async def _gchat_prompt_public_url(self, wizard: dict, chat) -> None:
        if wizard.get("kind") == "edit":
            await self._transport.send_text(
                chat,
                "Step 3 of 4 — enter the public HTTPS URL where Google Chat will reach this bot,\n"
                f"or `/keep` to keep the current value ({wizard['data'].get('public_url')}):",
            )
        else:
            await self._transport.send_text(
                chat,
                "Step 3 of 4 — enter the public HTTPS URL where Google Chat will reach this bot\n"
                "(e.g. https://alpha.example.com):",
            )

    async def _gchat_step_public_url(self, wizard: dict, chat, text: str) -> None:
        if self._gchat_is_keep(wizard, text):
            wizard["step"] = "root_command_id"
            await self._gchat_prompt_command_id(wizard, chat)
            return
        from urllib.parse import urlparse
        try:
            parsed = urlparse(text)
        except ValueError:
            await self._transport.send_text(
                chat,
                "URL is malformed. Send a valid https://... URL:",
            )
            return
        if parsed.scheme != "https" or not parsed.hostname:
            await self._transport.send_text(
                chat,
                "URL must start with https:// and include a hostname — try again:",
            )
            return
        wizard["data"]["public_url"] = text
        wizard["step"] = "root_command_id"
        await self._gchat_prompt_command_id(wizard, chat)

    async def _gchat_prompt_command_id(self, wizard: dict, chat) -> None:
        if wizard.get("kind") == "edit":
            await self._transport.send_text(
                chat,
                "Step 4 of 4 — enter the Cloud Console slash-command ID (integer),\n"
                f"or `/keep` to keep the current value ({wizard['data'].get('root_command_id')}):",
            )
        else:
            await self._transport.send_text(
                chat,
                "Step 4 of 4 — enter the Cloud Console slash-command ID (integer)\n"
                "(numeric ID from the Google Chat app's slash-command settings):",
            )

    async def _gchat_step_root_command_id(
        self, ctx: ContextTypes.DEFAULT_TYPE, wizard: dict, chat, text: str,
    ) -> None:
        if self._gchat_is_keep(wizard, text):
            await self._finalize_gchat_wizard(ctx, wizard, chat)
            return
        try:
            cmd_id = int(text)
        except ValueError:
            await self._transport.send_text(
                chat, "Invalid. Enter a numeric slash-command ID:",
            )
            return
        if cmd_id < 1:
            await self._transport.send_text(
                chat, "Command ID must be a positive integer:",
            )
            return
        wizard["data"]["root_command_id"] = cmd_id
        await self._finalize_gchat_wizard(ctx, wizard, chat)

    async def _finalize_gchat_wizard(
        self, ctx: ContextTypes.DEFAULT_TYPE, wizard: dict, chat,
    ) -> None:
        """Dispatch to the add- or edit-flavored finalizer based on wizard kind."""
        if wizard.get("kind") == "edit":
            await self._finalize_edit_gchat_wizard(ctx, wizard, chat)
        else:
            await self._finalize_add_google_chat_wizard(ctx, wizard, chat)

    async def _finalize_add_google_chat_wizard(
        self, ctx: ContextTypes.DEFAULT_TYPE, wizard: dict, chat,
    ) -> None:
        """Persist the override, start the subprocess, print the nginx snippet."""
        name = wizard["name"]
        data = wizard["data"]
        ctx.user_data.pop("gchat_wizard", None)

        # Persist the override atomically via _patch_json: load → mutate
        # one project entry → save, all under _config_lock. Using the
        # dict-based path matches the rest of the manager (_save_projects,
        # set_project_autostart, etc.) and avoids the heavier dataclass
        # round-trip that _load_config → save_config would do.
        from ..config import _patch_json
        cfg_path = self._project_config_path or DEFAULT_CONFIG
        gchat_block = {
            "port": data["port"],
            "service_account_file": data["service_account_file"],
            "public_url": data["public_url"],
            "root_command_id": data["root_command_id"],
        }

        def _patch(raw: dict) -> None:
            projects = raw.setdefault("projects", {})
            project = projects.get(name)
            if not isinstance(project, dict):
                return
            project["google_chat"] = gchat_block

        _patch_json(_patch, cfg_path)

        # Start the subprocess. Returns False if the project disappeared
        # or the config is unresolvable; report either way.
        started = self._pm.start_google_chat_subprocess(name)

        nginx_snippet = self._render_nginx_snippet(name, data)
        status_line = (
            f"Google Chat configured for '{name}' and bot started."
            if started
            else f"Google Chat configured for '{name}', but the bot did not start "
                 "(check logs and `start_google_chat_subprocess`)."
        )
        await self._transport.send_text(chat, status_line + "\n\n" + nginx_snippet)

    # ------------------------------------------------------------------
    # Task 13: Edit / Remove / Restart Google Chat handlers.
    #
    # The Edit wizard is a variant of the Add wizard (Task 12): same 4
    # steps, but the operator can send ``/keep`` at any step to preserve
    # the current value (prefilled into ``wizard["data"]`` by the starter).
    # On finalize, ``restart_google_chat_subprocess`` is called instead of
    # ``start_*`` because the bot is already running.
    # ------------------------------------------------------------------

    async def _start_edit_google_chat_wizard(
        self, click: "ButtonClick", ctx_user_data: dict | None, name: str,
    ) -> None:
        """Arm the edit wizard for ``name`` and prompt for the SA-file path.

        Loads the current ``google_chat`` block from disk and seeds
        ``wizard["data"]`` so each step's ``/keep`` branch can fall back to
        the stored value. Bails out if no override exists (operator should
        use Add instead).
        """
        if ctx_user_data is None:
            await self._transport.edit_text(
                click.message,
                "Cannot start the Google Chat wizard — no per-user state slot.",
            )
            return
        project = self._load_projects().get(name, {})
        current = project.get("google_chat") or {}
        if not current:
            await self._transport.edit_text(
                click.message,
                f"No Google Chat override to edit for '{name}'. Use [Add Google Chat] instead.",
            )
            return
        ctx_user_data["gchat_wizard"] = {
            "name": name,
            "step": "sa_file",
            "kind": "edit",
            "data": dict(current),
        }
        await self._transport.edit_text(
            click.message,
            (
                f"Edit Google Chat for '{name}' — step 1 of 4.\n\n"
                f"Current service-account JSON: {current.get('service_account_file', '(none)')}\n"
                "Send a new absolute path, or `/keep` to keep the current value.\n"
                "(/cancel to abort)"
            ),
        )

    async def _finalize_edit_gchat_wizard(
        self, ctx: ContextTypes.DEFAULT_TYPE, wizard: dict, chat,
    ) -> None:
        """Persist the (possibly partially updated) override and restart the bot."""
        name = wizard["name"]
        data = wizard["data"]
        ctx.user_data.pop("gchat_wizard", None)

        from ..config import _patch_json
        cfg_path = self._project_config_path or DEFAULT_CONFIG
        gchat_block = {
            "port": data["port"],
            "service_account_file": data["service_account_file"],
            "public_url": data["public_url"],
            "root_command_id": data["root_command_id"],
        }

        def _patch(raw: dict) -> None:
            projects = raw.setdefault("projects", {})
            project = projects.get(name)
            if not isinstance(project, dict):
                return
            project["google_chat"] = gchat_block

        _patch_json(_patch, cfg_path)

        restarted = self._pm.restart_google_chat_subprocess(name)
        status_line = (
            f"Google Chat override updated for '{name}' and bot restarted."
            if restarted
            else f"Google Chat override updated for '{name}', but the bot did not restart "
                 "(check logs and `restart_google_chat_subprocess`)."
        )
        await self._transport.send_text(chat, status_line)

    async def _handle_restart_gchat(self, click: "ButtonClick", name: str) -> None:
        """Operator clicked Restart Google Chat. No config write — just a PM call."""
        restarted = self._pm.restart_google_chat_subprocess(name)
        msg = (
            f"Google Chat subprocess restarted for '{name}'."
            if restarted
            else f"Restart for '{name}' failed (config missing?)."
        )
        await self._transport.edit_text(click.message, msg)

    async def _handle_remove_gchat(self, click: "ButtonClick", name: str) -> None:
        """Operator clicked Remove Google Chat. Clear the override + stop the bot.

        nginx vhost is intentionally left intact — the operator may want to
        keep the subdomain available or clean it up manually. We just print
        a one-liner reminder so they know to do that themselves.
        """
        from ..config import _patch_json

        def _patch(raw: dict) -> None:
            project = raw.get("projects", {}).get(name)
            if not isinstance(project, dict):
                return
            project.pop("google_chat", None)

        cfg_path = self._project_config_path or DEFAULT_CONFIG
        _patch_json(_patch, cfg_path)
        self._pm.stop_google_chat_subprocess(name)
        await self._transport.edit_text(
            click.message,
            f"Google Chat override removed for '{name}'. nginx vhost left intact — "
            "clean it up manually if you no longer need the subdomain.",
        )

    def _render_nginx_snippet(self, name: str, data: dict) -> str:
        """Render an nginx vhost snippet for the given Google Chat override.

        Returns a fenced markdown code block followed by a one-line certbot
        hint. ``public_host`` is the host portion of ``data["public_url"]``
        (stripped of scheme + path). The snippet does NOT include the
        proxy host/port resolution — the bot binds 127.0.0.1:<port> so the
        proxy_pass URL is hard-wired against the loopback address.
        """
        from urllib.parse import urlparse
        parsed = urlparse(data["public_url"])
        host = parsed.hostname or data["public_url"]
        port = data["port"]
        return (
            f"nginx vhost for {name}:\n\n"
            "```nginx\n"
            "server {\n"
            "    listen 80;\n"
            f"    server_name {host};\n"
            "    location /.well-known/acme-challenge/ { root /var/www/letsencrypt; }\n"
            "    location / { return 301 https://$host$request_uri; }\n"
            "}\n"
            "server {\n"
            "    listen 443 ssl http2;\n"
            f"    server_name {host};\n"
            f"    ssl_certificate /etc/letsencrypt/live/{host}/fullchain.pem;\n"
            f"    ssl_certificate_key /etc/letsencrypt/live/{host}/privkey.pem;\n"
            "    location /google-chat/events {\n"
            f"        proxy_pass http://127.0.0.1:{port}/google-chat/events;\n"
            "        proxy_http_version 1.1;\n"
            "        proxy_set_header Host $host;\n"
            "        proxy_set_header X-Forwarded-Proto https;\n"
            "        proxy_read_timeout 10s;\n"
            "        client_max_body_size 10m;\n"
            "    }\n"
            "}\n"
            "```\n\n"
            f"Run `sudo certbot --nginx -d {host}` to issue/renew the TLS certificate.\n"
            "Reload nginx with `sudo systemctl reload nginx` after dropping the\n"
            "vhost into `/etc/nginx/sites-enabled/`."
        )

    async def _on_create_project(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        if not await self._guard_executor(update):
            return ConversationHandler.END
        incoming = self._incoming_from_update(update)
        try:
            from ..github_client import GitHubClient, _gh_available
            from ..botfather import BotFatherClient
        except ImportError:
            await self._transport.send_text(incoming.chat, _CREATE_DEPS_MESSAGE)
            return ConversationHandler.END
        from ..config import load_config
        path = self._project_config_path or DEFAULT_CONFIG
        config = load_config(path)
        if not config.github_pat and not _gh_available():
            await self._transport.send_text(
                incoming.chat,
                "GitHub not configured. Run /setup to set a PAT, or install gh CLI.",
            )
            return ConversationHandler.END
        if not config.telegram_api_id or not config.telegram_api_hash:
            await self._transport.send_text(
                incoming.chat,
                "Telegram API not configured. Run /setup first.",
            )
            return ConversationHandler.END
        session_path = path.parent / "telethon.session"
        if not session_path.exists():
            await self._transport.send_text(
                incoming.chat,
                "Telethon not authenticated. Run /setup first.",
            )
            return ConversationHandler.END

        ctx.user_data["create"] = {"config_path": str(path)}
        return await self._show_provider_pick(update, ctx)

    async def _show_provider_pick(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        """Render the provider picker and return ``CREATE_PROVIDER_PICK``.

        Used as the first step of the /create_project wizard. Accepts either a
        text-driven entry (``update.message`` is set) or a callback-driven
        re-entry (``update.callback_query`` is set), matching the dual entry
        pattern used elsewhere in the wizard.
        """
        buttons = Buttons(rows=[
            [Button(label="GitHub", value="provider:github")],
            [Button(label="GitLab", value="provider:gitlab")],
            [Button(label="Cancel", value="provider:cancel")],
        ])
        if update.callback_query is not None:
            msg_ref = self._msg_ref_from_query(update.callback_query)
            await self._transport.edit_text(
                msg_ref, "Pick a repo provider:", buttons=buttons,
            )
        else:
            incoming = self._incoming_from_update(update)
            await self._transport.send_text(
                incoming.chat, "Pick a repo provider:", buttons=buttons,
            )
        return self.CREATE_PROVIDER_PICK

    async def _create_provider_pick_callback(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Handle the provider-picker button press: store the choice + advance."""
        query = update.callback_query
        await query.answer()
        payload = query.data
        msg_ref = self._msg_ref_from_query(query)
        if payload == "provider:cancel":
            ctx.user_data.pop("create", None)
            await self._transport.edit_text(msg_ref, "Cancelled.")
            return ConversationHandler.END
        if payload not in ("provider:github", "provider:gitlab"):
            # Unknown payload — re-render the picker by staying in the same state.
            return self.CREATE_PROVIDER_PICK
        ctx.user_data.setdefault("create", {})["provider"] = payload.split(":", 1)[1]
        # Advance to the existing repo-source picker (GitHub browse / paste URL).
        buttons = Buttons(rows=[
            [Button(label="From GitHub", value="create_from_gh")],
            [Button(label="Paste URL", value="create_paste_url")],
        ])
        await self._transport.edit_text(
            msg_ref, "Create project — choose repo source:", buttons=buttons,
        )
        return self.CREATE_SOURCE

    async def _create_source_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        data = query.data
        msg_ref = self._msg_ref_from_query(query)
        if data == "create_from_gh":
            return await self._show_repo_page(msg_ref, ctx, page=1)
        elif data == "create_paste_url":
            await self._transport.edit_text(msg_ref, "Paste the GitHub repo URL:")
            return self.CREATE_REPO_URL
        return ConversationHandler.END

    async def _show_repo_page(
        self, msg: MessageRef, ctx, page: int, user_data_key: str = "create",
    ) -> int:
        from ..config import load_config
        path = Path(ctx.user_data[user_data_key]["config_path"])
        config = load_config(path)
        provider = _build_repo_provider(ctx, config)
        try:
            repos, has_next = await provider.list_repos(page=page, per_page=5)
        except Exception as e:
            await self._transport.edit_text(msg, f"Repo provider error: {e}")
            return ConversationHandler.END
        finally:
            await provider.close()
        if not repos:
            await self._transport.edit_text(msg, "No repos found.")
            return ConversationHandler.END
        ctx.user_data[user_data_key]["repos"] = {r.full_name: r.__dict__ for r in repos}
        ctx.user_data[user_data_key]["page"] = page
        rows: list[list[Button]] = [
            [Button(
                label=f"{'🔒 ' if r.private else ''}{r.name}",
                value=f"create_repo_{r.full_name}",
            )]
            for r in repos
        ]
        nav: list[Button] = []
        if page > 1:
            nav.append(Button(label="« Prev", value=f"create_page_{page - 1}"))
        if has_next:
            nav.append(Button(label="Next »", value=f"create_page_{page + 1}"))
        if nav:
            rows.append(nav)
        rows.append([Button(label="Cancel", value="create_cancel")])
        await self._transport.edit_text(msg, "Select a repo:", buttons=Buttons(rows=rows))
        return self.CREATE_TEAM_REPO_LIST if user_data_key == "create_team" else self.CREATE_REPO_LIST

    async def _create_repo_list_callback(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, user_data_key: str = "create"
    ) -> int:
        query = update.callback_query
        await query.answer()
        data = query.data
        msg_ref = self._msg_ref_from_query(query)
        if data.startswith("create_page_"):
            page = int(data.split("_")[-1])
            return await self._show_repo_page(msg_ref, ctx, page, user_data_key=user_data_key)
        elif data.startswith("create_repo_"):
            full_name = data[len("create_repo_"):]
            repos = ctx.user_data[user_data_key].get("repos", {})
            if full_name not in repos:
                await self._transport.edit_text(msg_ref, "Repo not found. Try again.")
                return ConversationHandler.END
            repo_data = repos[full_name]
            ctx.user_data[user_data_key]["repo"] = repo_data
            suggested_name = repo_data["name"]
            ctx.user_data[user_data_key]["suggested_name"] = suggested_name
            if user_data_key == "create_team":
                await self._transport.edit_text(msg_ref, "Short project name?")
                return self.CREATE_TEAM_NAME
            buttons = Buttons(rows=[
                [Button(label=f'Use "{suggested_name}"', value="create_name_use")],
                [Button(label="Custom name", value="create_name_custom")],
            ])
            await self._transport.edit_text(msg_ref, "Project name?", buttons=buttons)
            return self.CREATE_NAME
        elif data == "create_cancel":
            ctx.user_data.pop(user_data_key, None)
            await self._transport.edit_text(msg_ref, "Cancelled.")
            return ConversationHandler.END
        return ConversationHandler.END

    async def _create_repo_url(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE, user_data_key: str = "create"
    ) -> int:
        incoming = self._incoming_from_update(update)
        url = incoming.text.strip()
        from ..config import load_config
        path = Path(ctx.user_data[user_data_key]["config_path"])
        config = load_config(path)
        provider = _build_repo_provider(ctx, config)
        try:
            repo = await provider.validate_repo_url(url)
        except Exception as e:
            await self._transport.send_text(incoming.chat, f"Error: {e}\nTry again or /cancel:")
            return self.CREATE_TEAM_REPO_URL if user_data_key == "create_team" else self.CREATE_REPO_URL
        finally:
            await provider.close()
        if not repo:
            await self._transport.send_text(
                incoming.chat,
                "Invalid or not found. Paste a valid repo URL:",
            )
            return self.CREATE_TEAM_REPO_URL if user_data_key == "create_team" else self.CREATE_REPO_URL
        ctx.user_data[user_data_key]["repo"] = repo.__dict__
        suggested_name = repo.name
        ctx.user_data[user_data_key]["suggested_name"] = suggested_name
        if user_data_key == "create_team":
            await self._transport.send_text(incoming.chat, "Short project name?")
            return self.CREATE_TEAM_NAME
        buttons = Buttons(rows=[
            [Button(label=f'Use "{suggested_name}"', value="create_name_use")],
            [Button(label="Custom name", value="create_name_custom")],
        ])
        await self._transport.send_text(incoming.chat, "Project name?", buttons=buttons)
        return self.CREATE_NAME

    async def _create_name_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        msg_ref = self._msg_ref_from_query(query)
        if query.data == "create_name_use":
            name = ctx.user_data["create"]["suggested_name"]
            projects = self._load_projects()
            if name in projects:
                await self._transport.edit_text(
                    msg_ref, f"'{name}' already exists. Enter a custom name:",
                )
                return self.CREATE_NAME_INPUT
            ctx.user_data["create"]["name"] = name
            return await self._do_create_bot(msg_ref, ctx)
        elif query.data == "create_name_custom":
            await self._transport.edit_text(msg_ref, "Enter the project name:")
            return self.CREATE_NAME_INPUT
        return ConversationHandler.END

    async def _create_name_input(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        incoming = self._incoming_from_update(update)
        name = incoming.text.strip()
        projects = self._load_projects()
        if name in projects:
            await self._transport.send_text(
                incoming.chat,
                f"'{name}' already exists. Try another name:",
            )
            return self.CREATE_NAME_INPUT
        ctx.user_data["create"]["name"] = name
        await self._transport.send_text(incoming.chat, "Creating Telegram bot via BotFather...")
        return await self._do_create_bot_text(update, ctx)

    async def _do_create_bot(self, msg: MessageRef, ctx) -> int:
        await self._transport.edit_text(msg, "Creating Telegram bot via BotFather...")
        name = ctx.user_data["create"]["name"]
        return await self._execute_bot_creation(msg.chat, ctx, name)

    async def _do_create_bot_text(self, update, ctx) -> int:
        name = ctx.user_data["create"]["name"]
        incoming = self._incoming_from_update(update)
        return await self._execute_bot_creation(incoming.chat, ctx, name)

    async def _execute_bot_creation(self, chat: ChatRef, ctx, name: str) -> int:
        from ..botfather import BotFatherClient, sanitize_bot_username
        from ..config import load_config
        path = Path(ctx.user_data["create"]["config_path"])
        config = load_config(path)
        session_path = path.parent / "telethon.session"
        # Reuse the manager's persistent Telethon client so we don't fight it
        # for the same SQLite session file (would surface as "database is locked").
        bf = BotFatherClient(
            config.telegram_api_id,
            config.telegram_api_hash,
            session_path,
            client=getattr(self, "_telethon_client", None),
        )
        bot_username = sanitize_bot_username(name)
        try:
            token = await bf.create_bot(display_name=f"{name} Claude", username=bot_username)
            ctx.user_data["create"]["bot_token"] = token
            ctx.user_data["create"]["bot_username"] = bot_username
            await self._transport.send_text(
                chat, f"Created @{bot_username}. Cloning repository...",
            )
            return await self._execute_clone(chat, ctx)
        except Exception as e:
            buttons = Buttons(rows=[
                [Button(label="Retry", value="create_retry_bot")],
                [Button(label="Enter token manually", value="create_manual_token")],
                [Button(label="Cancel", value="create_cancel")],
            ])
            await self._transport.send_text(
                chat, f"Bot creation failed: {e}", buttons=buttons,
            )
            return self.CREATE_BOT
        finally:
            await bf.disconnect()

    async def _create_bot_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        msg_ref = self._msg_ref_from_query(query)
        if query.data == "create_retry_bot":
            name = ctx.user_data["create"]["name"]
            await self._transport.edit_text(msg_ref, "Retrying bot creation...")
            return await self._execute_bot_creation(msg_ref.chat, ctx, name)
        elif query.data == "create_manual_token":
            await self._transport.edit_text(msg_ref, "Paste the bot token from BotFather:")
            return self.CREATE_BOT
        elif query.data == "create_cancel":
            ctx.user_data.pop("create", None)
            await self._transport.edit_text(msg_ref, "Cancelled.")
            return ConversationHandler.END
        return ConversationHandler.END

    async def _create_bot_token_input(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        incoming = self._incoming_from_update(update)
        token = incoming.text.strip()
        ctx.user_data["create"]["bot_token"] = token
        ctx.user_data["create"]["bot_username"] = "(manual)"
        await self._transport.send_text(incoming.chat, "Token saved. Cloning repository...")
        return await self._execute_clone(incoming.chat, ctx)

    async def _execute_clone(self, chat: ChatRef, ctx) -> int:
        from ..repo_provider import RepoInfo
        from ..config import load_config
        path = Path(ctx.user_data["create"]["config_path"])
        config = load_config(path)
        repo_data = ctx.user_data["create"]["repo"]
        repo = RepoInfo(**repo_data)
        name = ctx.user_data["create"]["name"]
        dest = path.parent / "repos" / name
        provider = _build_repo_provider(ctx, config)
        try:
            await provider.clone_repo(repo, dest)
            ctx.user_data["create"]["clone_path"] = str(dest)
        except Exception as e:
            buttons = Buttons(rows=[
                [Button(label="Retry", value="create_retry_clone")],
                [Button(label="Cancel", value="create_cancel")],
            ])
            await self._transport.send_text(chat, f"Clone failed: {e}", buttons=buttons)
            return self.CREATE_CLONE
        finally:
            await provider.close()
        return await self._finalize_create(chat, ctx)

    async def _create_clone_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        msg_ref = self._msg_ref_from_query(query)
        if query.data == "create_retry_clone":
            await self._transport.edit_text(msg_ref, "Retrying clone...")
            return await self._execute_clone(msg_ref.chat, ctx)
        elif query.data == "create_cancel":
            ctx.user_data.pop("create", None)
            await self._transport.edit_text(msg_ref, "Cancelled.")
            return ConversationHandler.END
        return ConversationHandler.END

    async def _finalize_create(self, chat: ChatRef, ctx) -> int:
        create_data = ctx.user_data.pop("create", {})
        name = create_data["name"]
        repo = create_data["repo"]
        clone_path = create_data["clone_path"]
        bot_token = create_data["bot_token"]
        bot_username = create_data.get("bot_username", "")
        projects = self._load_projects()
        projects[name] = {
            "path": clone_path,
            "telegram_bot_token": bot_token,
            "autostart": False,
            "managed_by_manager": True,
            "managed_repo_path": clone_path,
            "managed_bot_username": bot_username,
        }
        self._save_projects(projects)
        summary = (
            f"Project created!\n\n"
            f"Name: {name}\n"
            f"Repo: {repo['html_url']}\n"
            f"Path: {clone_path}\n"
            f"Bot: @{bot_username}"
        )
        buttons = Buttons(rows=[
            [Button(label="Start Project", value=f"proj_start_{name}")],
            [Button(label="Done", value="proj_back")],
        ])
        await self._transport.send_text(chat, summary, buttons=buttons)
        return ConversationHandler.END

    async def _create_cancel(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        incoming = self._incoming_from_update(update)
        ctx.user_data.pop("create", None)
        await self._transport.send_text(incoming.chat, "Project creation cancelled.")
        return ConversationHandler.END

    async def _on_create_team(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        """Entry point for /create_team — pick repo source (GitHub browse vs paste URL)."""
        if not await self._guard_executor(update):
            return ConversationHandler.END
        incoming = self._incoming_from_update(update)

        # Cred-only pre-flight (prefix isn't known yet; full collision check runs in NAME state).
        cfg_path = self._project_config_path or DEFAULT_CONFIG
        err = _create_team_preflight(cfg_path, prefix=None)
        if err:
            await self._transport.send_text(incoming.chat, err)
            return ConversationHandler.END

        ctx.user_data["create_team"] = {"config_path": str(cfg_path)}
        buttons = Buttons(rows=[
            [Button(label="Browse my GitHub repos", value="ct_source:github")],
            [Button(label="Paste a URL", value="ct_source:url")],
        ])
        await self._transport.send_text(
            incoming.chat,
            "How would you like to pick the repo?",
            buttons=buttons,
        )
        return self.CREATE_TEAM_SOURCE

    async def _create_team_source_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        _, source = query.data.split(":", 1)
        ctx.user_data.setdefault("create_team", {})["source"] = source
        msg_ref = self._msg_ref_from_query(query)
        if source == "github":
            return await self._show_repo_page(msg_ref, ctx, page=1, user_data_key="create_team")
        await self._transport.edit_text(
            msg_ref, "Paste the repo URL (e.g. https://github.com/owner/repo):",
        )
        return self.CREATE_TEAM_REPO_URL

    async def _create_team_name(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        incoming = self._incoming_from_update(update)
        prefix = incoming.text.strip().lower()
        if not prefix.isidentifier() or not prefix.isascii():
            await self._transport.send_text(
                incoming.chat,
                "Prefix must be lowercase ascii word characters only. Try again:",
            )
            return self.CREATE_TEAM_NAME

        cfg_path = self._project_config_path or DEFAULT_CONFIG
        err = _create_team_preflight(cfg_path, prefix)
        if err:
            await self._transport.send_text(incoming.chat, f"✗ {err}")
            return ConversationHandler.END

        ctx.user_data["create_team"]["project_prefix"] = prefix

        # Persona picker — list global personas (no project path yet, since clone hasn't happened).
        fake_path = Path(DEFAULT_CONFIG).parent
        buttons = _build_persona_keyboard(fake_path, callback_prefix="ct_persona_mgr")
        await self._transport.send_text(
            incoming.chat,
            "Pick manager-role persona:",
            buttons=buttons,
        )
        return self.CREATE_TEAM_PERSONA_MGR

    async def _create_team_persona_mgr_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        _, persona = query.data.split(":", 1)
        ctx.user_data["create_team"]["persona_mgr"] = persona

        fake_path = Path(DEFAULT_CONFIG).parent
        buttons = _build_persona_keyboard(fake_path, callback_prefix="ct_persona_dev")
        msg_ref = self._msg_ref_from_query(query)
        await self._transport.edit_text(
            msg_ref,
            f"Manager persona: {persona}\n\nPick dev-role persona:",
            buttons=buttons,
        )
        return self.CREATE_TEAM_PERSONA_DEV

    async def _create_team_persona_dev_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        _, persona = query.data.split(":", 1)
        ctx.user_data["create_team"]["persona_dev"] = persona

        # All inputs captured — kick off orchestrator (F7).
        return await self._create_team_execute(update, ctx)

    async def _create_team_execute(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        """F7 orchestrator: create both bots, clone repo, build group, commit config, spawn."""
        from ..config import load_config, patch_team
        try:
            (
                BotFatherClient,
                GitHubClient,
                RepoInfo,
                add_bot,
                create_supergroup,
                invite_user,
                promote_admin,
                sanitize_bot_username,
            ) = _load_team_create_dependencies()
        except ImportError:
            incoming = self._incoming_from_update(update)
            await self._transport.send_text(incoming.chat, _CREATE_DEPS_MESSAGE)
            return ConversationHandler.END

        data = ctx.user_data["create_team"]
        prefix = data["project_prefix"]
        mgr_persona = data["persona_mgr"]
        dev_persona = data["persona_dev"]
        repo_data = data["repo"]
        # Repo is stored as a dict (RepoInfo.__dict__) by _create_repo_list_callback;
        # reconstitute the dataclass for clone_repo. If it's already a RepoInfo
        # (e.g. from a test), use it directly.
        if isinstance(repo_data, dict):
            repo = RepoInfo(**repo_data)
        else:
            repo = repo_data

        cfg_path = self._project_config_path or DEFAULT_CONFIG
        config = load_config(cfg_path)
        incoming = self._incoming_from_update(update)
        chat = incoming.chat

        status_msg = await self._transport.send_text(chat, "⟳ Creating bot 1...")

        async def edit(text: str) -> None:
            try:
                await self._transport.edit_text(status_msg, text)
            except Exception:
                pass

        # Reuse the manager's persistent Telethon client if available so we
        # don't fight the team relay for the same SQLite session file.
        bfc = BotFatherClient(
            api_id=config.telegram_api_id,
            api_hash=config.telegram_api_hash,
            session_path=cfg_path.parent / "telethon.session",
            client=getattr(self, "_telethon_client", None),
        )

        completed: dict[str, str] = {}
        config_committed = False
        try:
            # --- Bot 1 (manager) ---
            mgr_base = sanitize_bot_username(f"{prefix}_mgr")
            mgr_token, mgr_username = await _create_bot_with_retry(
                bfc, f"{prefix} Manager", mgr_base
            )
            completed["bot1"] = f"@{mgr_username}"
            await edit(f"✓ Bot 1 (@{mgr_username}) | ⟳ Creating bot 2...")

            # --- Bot 2 (dev) ---
            dev_base = sanitize_bot_username(f"{prefix}_dev")
            dev_token, dev_username = await _create_bot_with_retry(
                bfc, f"{prefix} Dev", dev_base
            )
            completed["bot2"] = f"@{dev_username}"
            await edit("✓ Bots | ⟳ Disabling privacy mode...")

            # --- Privacy disable (non-fatal) ---
            for username in (mgr_username, dev_username):
                try:
                    await bfc.disable_privacy(username)
                except Exception as exc:
                    logger.warning("Privacy disable failed for %s: %s", username, exc)
            await edit("✓ Bots ready | ⟳ Cloning repo...")

            # --- Clone ---
            dest = cfg_path.parent / "repos" / prefix
            gh = GitHubClient(pat=config.github_pat)
            try:
                await gh.clone_repo(repo, dest)
            finally:
                await gh.close()
            completed["repo"] = str(dest)
            # Scaffold the dual-agent layout (idempotent — exist_ok=True).
            for sub in ("docs", "src", "tests"):
                (dest / sub).mkdir(parents=True, exist_ok=True)
            await edit(f'✓ Cloned | ⟳ Creating group "{prefix} team"...')

            # --- Group ---
            client = await bfc._ensure_client()  # reuse authenticated Telethon client
            group_id = await create_supergroup(client, f"{prefix} team")
            completed["group"] = str(group_id)
            await edit("✓ Group | ⟳ Adding + promoting bots...")

            await add_bot(client, group_id, mgr_username)
            await add_bot(client, group_id, dev_username)

            # --- COMMIT config (point of no return) ---
            # Team bots run unattended — skip tool-permission prompts. Mirror
            # the legacy ``permissions`` flat key for downgrade safety; the new
            # shape lives under ``backend_state["claude"]``.
            def _unattended_state() -> dict:
                return {"claude": {"permissions": "dangerously-skip-permissions"}}
            patch_team(
                prefix,
                {
                    "path": str(dest),
                    "group_chat_id": group_id,
                    "bots": {
                        "manager": {
                            "telegram_bot_token": mgr_token,
                            "active_persona": mgr_persona,
                            "permissions": "dangerously-skip-permissions",
                            # Store each bot's @handle so the peer role can address it
                            # directly instead of using a persona-placeholder like "@developer".
                            "bot_username": mgr_username,
                            "backend": "claude",
                            "backend_state": _unattended_state(),
                        },
                        "dev": {
                            "telegram_bot_token": dev_token,
                            "active_persona": dev_persona,
                            "permissions": "dangerously-skip-permissions",
                            "bot_username": dev_username,
                            "backend": "claude",
                            "backend_state": _unattended_state(),
                        },
                    },
                },
                cfg_path,
            )
            config_committed = True

            # --- Post-commit (all non-fatal) ---
            for username in (mgr_username, dev_username):
                try:
                    await promote_admin(client, group_id, username)
                except Exception as exc:
                    logger.warning("Promote admin failed for %s: %s", username, exc)

            requester = incoming.sender.handle if incoming.sender else None
            if requester:
                try:
                    await invite_user(client, group_id, requester)
                except Exception as exc:
                    logger.warning("Invite requester %s failed: %s", requester, exc)

            await edit("✓ Group wired | ⟳ Starting both bots...")
            self._pm.start_team(prefix, "manager")
            self._pm.start_team(prefix, "dev")
            # TeamRelay now lives in the project bot subprocess (see #0c).
            # Manager just spawns the bots; each project bot constructs its own
            # TelegramClient from LP2C_TELETHON_SESSION and calls
            # transport.enable_team_relay() in build().
            await edit(f'✓ Team ready. Open the "{prefix} team" group to start chatting.')

        except Exception as exc:
            await self._send_partial_failure_report(
                chat, exc, completed, config_committed=config_committed
            )
        finally:
            try:
                await bfc.disconnect()
            except Exception:
                pass

        return ConversationHandler.END

    async def _send_partial_failure_report(
        self,
        chat: ChatRef,
        exc: Exception,
        completed: dict[str, str],
        config_committed: bool = False,
    ) -> None:
        """Send a human-readable report of what was completed before the failure."""
        lines = [
            f"✗ Team creation failed: {type(exc).__name__}: {exc}",
            "",
        ]
        if completed:
            lines.append("Completed (needs manual cleanup):")
            if "bot1" in completed:
                lines.append(
                    f"  - Bot {completed['bot1']} (delete via BotFather /deletebot)"
                )
            if "bot2" in completed:
                lines.append(
                    f"  - Bot {completed['bot2']} (delete via BotFather /deletebot)"
                )
            if "repo" in completed:
                lines.append(
                    f"  - Directory {completed['repo']} (remove if not needed)"
                )
            if "group" in completed:
                lines.append(
                    f"  - Group {completed['group']} (delete via Telegram)"
                )
            lines.append("")
        if config_committed:
            lines.append(
                "⚠ Team config WAS saved. Use /delete_team to clean up before retrying."
            )
        else:
            lines.append("Config not saved. Safe to retry with a different prefix.")
        await self._transport.send_text(chat, "\n".join(lines))

    # --- /delete_team -------------------------------------------------------

    async def _on_delete_team(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        """Entry: /delete_team [prefix]. Lists teams if no arg, else confirms the target."""
        if not await self._guard_executor(update):
            return ConversationHandler.END

        from ..config import load_config
        incoming = self._incoming_from_update(update)
        cfg_path = self._project_config_path or DEFAULT_CONFIG
        config = load_config(cfg_path)
        if not config.teams:
            await self._transport.send_text(incoming.chat, "No teams configured.")
            return ConversationHandler.END

        target: str | None = None
        if ctx.args:
            target = ctx.args[0].strip().lower()

        if target is None:
            # Show a keyboard of existing teams.
            rows: list[list[Button]] = [
                [Button(label=name, value=f"dt_pick:{name}")]
                for name in sorted(config.teams)
            ]
            rows.append([Button(label="Cancel", value="dt_pick:__cancel__")])
            await self._transport.send_text(
                incoming.chat,
                "Pick a team to delete:",
                buttons=Buttons(rows=rows),
            )
            return self.DELETE_TEAM_CONFIRM

        if target not in config.teams:
            await self._transport.send_text(incoming.chat, f"Team `{target}` not found.")
            return ConversationHandler.END

        return await self._delete_team_show_confirm(incoming.chat, target, ctx)

    async def _delete_team_show_confirm(
        self, chat: ChatRef, target: str, ctx: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Render the irreversible-action confirmation."""
        from ..config import load_config
        cfg_path = self._project_config_path or DEFAULT_CONFIG
        team = load_config(cfg_path).teams[target]
        ctx.user_data["delete_team"] = {"target": target}
        bot_usernames = [b.bot_username or "(no username)" for b in team.bots.values()]
        message = (
            f"⚠ Delete team `{target}`?\n\n"
            f"This will:\n"
            f"  • Stop both team bot subprocesses\n"
            f"  • Delete bots via BotFather: {', '.join('@' + u for u in bot_usernames if u and u != '(no username)')}\n"
            f"  • Delete the Telegram supergroup (chat_id={team.group_chat_id})\n"
            f"  • Remove `{team.path}` from disk\n"
            f"  • Remove the team entry from config.json\n\n"
            f"This cannot be undone. Proceed?"
        )
        buttons = Buttons(rows=[
            [Button(label=f"Delete {target}", value="dt_confirm")],
            [Button(label="Cancel", value="dt_cancel")],
        ])
        await self._transport.send_text(chat, message, buttons=buttons)
        return self.DELETE_TEAM_CONFIRM

    async def _delete_team_confirm_callback(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Callback for the pick / confirm / cancel keyboard."""
        query = update.callback_query
        await query.answer()
        data = query.data or ""
        msg_ref = self._msg_ref_from_query(query)
        if data.startswith("dt_pick:"):
            target = data.split(":", 1)[1]
            if target == "__cancel__":
                await self._transport.edit_text(msg_ref, "Cancelled.")
                return ConversationHandler.END
            await self._transport.edit_text(msg_ref, f"Selected: `{target}`")
            return await self._delete_team_show_confirm(msg_ref.chat, target, ctx)
        if data == "dt_cancel":
            ctx.user_data.pop("delete_team", None)
            await self._transport.edit_text(msg_ref, "Cancelled.")
            return ConversationHandler.END
        if data == "dt_confirm":
            target = ctx.user_data.get("delete_team", {}).get("target")
            if not target:
                await self._transport.edit_text(msg_ref, "No team selected — cancelling.")
                return ConversationHandler.END
            await self._transport.edit_text(msg_ref, f"⟳ Deleting team `{target}`...")
            await self._delete_team_execute(msg_ref.chat, target)
            ctx.user_data.pop("delete_team", None)
            return ConversationHandler.END
        return ConversationHandler.END

    async def _delete_team_execute(self, chat: ChatRef, target: str) -> None:
        """Best-effort cleanup. Partial-failure report lists whatever didn't work."""
        import shutil
        from ..config import load_config
        try:
            BotFatherClient, delete_supergroup = _load_team_delete_dependencies()
        except ImportError:
            await self._transport.send_text(chat, _CREATE_DEPS_MESSAGE)
            return

        cfg_path = self._project_config_path or DEFAULT_CONFIG
        config = load_config(cfg_path)
        team = config.teams.get(target)
        if team is None:
            await self._transport.send_text(
                chat, f"Team `{target}` not found (already deleted?).",
            )
            return

        status_msg = await self._transport.send_text(chat, "⟳ Stopping bots...")

        async def edit(text: str) -> None:
            try:
                await self._transport.edit_text(status_msg, text)
            except Exception:
                pass

        failures: list[str] = []

        # 1. Stop subprocesses (non-fatal).
        for role in team.bots:
            key = f"team:{target}:{role}"
            try:
                self._pm.stop(key)
            except Exception as exc:
                failures.append(f"stop {key}: {exc}")

        # 2. Relay teardown is handled by the project bot subprocesses
        # themselves on shutdown (see #0c) — manager no longer owns relays.

        # 3. Delete bots via BotFather (non-fatal per bot).
        await edit("⟳ Deleting bots via BotFather...")
        bfc = BotFatherClient(
            api_id=config.telegram_api_id,
            api_hash=config.telegram_api_hash,
            session_path=cfg_path.parent / "telethon.session",
            client=getattr(self, "_telethon_client", None),
        )
        try:
            for role, bot in team.bots.items():
                if not bot.bot_username:
                    continue
                try:
                    await bfc.delete_bot(bot.bot_username)
                except Exception as exc:
                    failures.append(f"BotFather /deletebot @{bot.bot_username}: {exc}")
        finally:
            try:
                await bfc.disconnect()
            except Exception:
                pass

        # 4. Delete Telegram supergroup (non-fatal).
        if team.group_chat_id and self._telethon_client is not None:
            await edit("⟳ Deleting Telegram supergroup...")
            try:
                await delete_supergroup(self._telethon_client, team.group_chat_id)
            except Exception as exc:
                failures.append(f"delete supergroup {team.group_chat_id}: {exc}")

        # 5. rm -rf project folder (non-fatal).
        if team.path:
            project_path = Path(team.path)
            if project_path.exists():
                await edit(f"⟳ Removing {project_path}...")
                try:
                    shutil.rmtree(project_path)
                except Exception as exc:
                    failures.append(f"rmtree {project_path}: {exc}")

        # 6. Remove team entry from config (ALWAYS — final step).
        await edit("⟳ Removing team from config...")
        try:
            updated = load_config(cfg_path)
            updated.teams.pop(target, None)
            from ..config import save_config
            save_config(updated, cfg_path)
        except Exception as exc:
            failures.append(f"remove team from config: {exc}")

        # 7. Report.
        if failures:
            report = (
                f"✗ Team `{target}` deleted with issues:\n\n"
                + "\n".join(f"  • {f}" for f in failures)
                + "\n\nThe team config entry was removed; you may need to clean the "
                "listed items manually."
            )
        else:
            report = f"✓ Team `{target}` fully deleted."
        await self._transport.send_text(chat, report)

    async def _edit_cancel(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        ctx.user_data.pop("pending_edit", None)
        incoming = self._incoming_from_update(update)
        await self._transport.send_text(incoming.chat, "Edit cancelled.")

    def _proj_detail_buttons(self, name: str, status: str) -> Buttons:
        """Produce the transport-native project-detail keyboard."""
        rows: list[list[Button]] = []
        if status == "running":
            rows.append([Button(label="Stop", value=f"proj_stop_{name}")])
            rows.append([Button(label="Logs", value=f"proj_logs_{name}")])
        else:
            rows.append([Button(label="Start", value=f"proj_start_{name}")])
        rows.append([Button(label="Plugins", value=f"proj_plugins_{name}")])
        rows.append([Button(label="Edit", value=f"proj_edit_{name}")])
        rows.append([Button(label="Remove", value=f"proj_remove_{name}")])
        rows.extend(self._google_chat_button_rows(name))
        rows.append([Button(label="« Back", value="proj_back")])
        return Buttons(rows=rows)

    def _google_chat_button_rows(self, name: str) -> list[list[Button]]:
        """Per-project Google Chat keyboard rows.

        Returns an 'Add Google Chat' row when the project has no
        ``google_chat`` override, or Edit/Restart/Remove rows when it does.
        The callback values follow the existing ``proj_<verb>_<name>``
        convention (extended with a ``gchat`` discriminator).
        """
        project = self._load_projects().get(name, {})
        if not project.get("google_chat"):
            return [[Button(
                label="Add Google Chat",
                value=f"proj_add_gchat_{name}",
            )]]
        return [
            [
                Button(
                    label="Edit Google Chat",
                    value=f"proj_edit_gchat_{name}",
                ),
                Button(
                    label="Restart Google Chat",
                    value=f"proj_restart_gchat_{name}",
                ),
            ],
            [Button(
                label="Remove Google Chat",
                value=f"proj_remove_gchat_{name}",
            )],
        ]

    def _available_plugins(self) -> list[str]:
        """Return sorted entry-point names registered under ``lptc.plugins``."""
        eps = importlib.metadata.entry_points(group="lptc.plugins")
        return sorted(ep.name for ep in eps)

    def _plugins_buttons(self, name: str) -> Buttons:
        """Plugin-toggle keyboard for the given project."""
        projects = self._load_projects()
        active = {p.get("name") for p in projects.get(name, {}).get("plugins", [])}
        available = self._available_plugins()
        rows: list[list[Button]] = []
        for plugin_name in available:
            label = f"✓ {plugin_name}" if plugin_name in active else f"+ {plugin_name}"
            rows.append([Button(label=label, value=f"proj_ptog_{plugin_name}|{name}")])
        rows.append([Button(label="« Back", value=f"proj_info_{name}")])
        return Buttons(rows=rows)

    def _team_detail_buttons(self, team_name: str, team) -> Buttons:
        """Produce the transport-native team-detail keyboard."""
        running = self._team_running_count(team_name, team)
        total = len(team.bots)
        rows: list[list[Button]] = []
        if running < total:
            rows.append([Button(
                label="Start" if running == 0 else "Start remaining",
                value=f"team_start_{team_name}",
            )])
        if running > 0:
            rows.append([Button(label="Stop", value=f"team_stop_{team_name}")])
        rows.append([Button(label="« Back", value="team_back")])
        return Buttons(rows=rows)

    async def _on_button_from_transport(self, click: "ButtonClick") -> None:
        """Transport-native callback dispatcher.

        Routes inline-button clicks based on click.value prefix. Replaces the
        legacy _on_callback(update, ctx) PTB handler. Wizard-internal callbacks
        (inside ConversationHandler.states) remain PTB-typed; this handler owns
        the GLOBAL ladder previously served by app.add_handler(CallbackQueryHandler).

        The outer try/finally fires ``_persist_auth_if_dirty`` on every exit
        path. ``_auth_identity`` → ``_get_user_role`` may append a first-contact
        identity to ``AllowedUser.locked_identities`` and flip ``_auth_dirty``;
        missing the persist tail would lose that lock on restart — letting a
        spoofer who lands first after restart bind their own native_id.
        """
        try:
            await self._dispatch_button_click(click)
        finally:
            await self._persist_auth_if_dirty()

    async def _dispatch_button_click(self, click: "ButtonClick") -> None:
        """Inner body of _on_button_from_transport. Extracted so the public
        entry point can wrap the dispatch ladder with the persist-tail
        try/finally without indenting the whole 300-line body."""
        if not self._auth_identity(click.sender):
            return  # silent for unauthorized callbacks (don't reveal handler structure)

        # Any button press cancels a pending inline edit. The pending_edit
        # state lives in PTB's per-user storage because the follow-up text
        # input still flows through PTB's MessageHandler (_edit_field_save).
        # Reach through native=(update, ctx) to clear it; falls back to a
        # no-op on transports that don't carry PTB state (FakeTransport).
        native = click.native
        ctx_user_data = None
        if isinstance(native, tuple) and len(native) >= 2:
            ctx = native[1]
            ctx_user_data = getattr(ctx, "user_data", None)
        if ctx_user_data is not None:
            ctx_user_data.pop("pending_edit", None)
            # Mirror pending_edit's cancel-on-any-button semantics for the
            # Add-Google-Chat wizard. The wizard's entry branch re-arms it
            # below; every other click drops it so a half-completed wizard
            # doesn't leak across unrelated flows.
            ctx_user_data.pop("gchat_wizard", None)

        value = click.value

        # Tasks 12/13: the proj_<verb>_gchat_<name> family must be matched
        # before the generic ``proj_edit_``/``proj_remove_`` branches below,
        # otherwise prefix matching routes ``proj_edit_gchat_alpha`` to the
        # legacy edit flow against a phantom project named ``gchat_alpha``.
        # Task 12 added the Add wizard; Task 13 wired up Edit / Restart /
        # Remove with their own dedicated handlers.
        if value.startswith("proj_add_gchat_"):
            if not await self._require_executor_button(click):
                return
            name = value[len("proj_add_gchat_"):]
            await self._start_add_google_chat_wizard(click, ctx_user_data, name)
            return

        if value.startswith("proj_edit_gchat_"):
            if not await self._require_executor_button(click):
                return
            name = value[len("proj_edit_gchat_"):]
            await self._start_edit_google_chat_wizard(click, ctx_user_data, name)
            return

        if value.startswith("proj_restart_gchat_"):
            if not await self._require_executor_button(click):
                return
            name = value[len("proj_restart_gchat_"):]
            await self._handle_restart_gchat(click, name)
            return

        if value.startswith("proj_remove_gchat_"):
            if not await self._require_executor_button(click):
                return
            name = value[len("proj_remove_gchat_"):]
            await self._handle_remove_gchat(click, name)
            return

        if value.startswith("proj_info_"):
            name = value[len("proj_info_"):]
            status = self._pm.status(name)
            await self._transport.edit_text(
                click.message,
                f"{name}: {status}",
                buttons=self._proj_detail_buttons(name, status),
            )

        elif value == "proj_back":
            buttons = self._list_buttons()
            await self._transport.edit_text(
                click.message,
                self._projects_text() if buttons else "No projects configured.",
                buttons=buttons,
            )

        elif value.startswith("proj_start_"):
            if not await self._require_executor_button(click):
                return
            name = value[len("proj_start_"):]
            self._pm.start(name)
            status = self._pm.status(name)
            await self._transport.edit_text(
                click.message,
                f"{name}: {status}",
                buttons=self._proj_detail_buttons(name, status),
            )

        elif value.startswith("proj_stop_"):
            if not await self._require_executor_button(click):
                return
            name = value[len("proj_stop_"):]
            self._pm.stop(name)
            status = self._pm.status(name)
            await self._transport.edit_text(
                click.message,
                f"{name}: {status}",
                buttons=self._proj_detail_buttons(name, status),
            )

        elif value.startswith("proj_logs_"):
            name = value[len("proj_logs_"):]
            output = self._pm.logs(name)
            if len(output) > 3500:
                output = output[-3500:]
            escaped = output.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            await self._transport.edit_text(
                click.message,
                f"<pre>{escaped}</pre>",
                buttons=Buttons(rows=[[Button(label="« Back", value=f"proj_info_{name}")]]),
                html=True,
            )

        elif value.startswith("proj_edit_"):
            if not await self._require_executor_button(click):
                return
            name = value[len("proj_edit_"):]
            rows: list[list[Button]] = [
                [Button(
                    label=field.capitalize().replace("_", " "),
                    value=f"proj_efld_{field}_{name}",
                )]
                for field in _BUTTON_EDIT_FIELDS
            ]
            rows.append([Button(label="« Back", value=f"proj_info_{name}")])
            await self._transport.edit_text(
                click.message,
                f"Edit '{name}' — choose field:",
                buttons=Buttons(rows=rows),
            )

        elif value.startswith("proj_efld_"):
            if not await self._require_executor_button(click):
                return
            parsed = _parse_edit_callback(value)
            if parsed:
                field, name = parsed
                if field == "model":
                    projects = self._load_projects()
                    current = projects.get(name, {}).get("model", "")
                    rows = []
                    for model_id, label in MODEL_OPTIONS:
                        prefix = "● " if current == model_id else ""
                        rows.append([Button(
                            label=f"{prefix}{label}",
                            value=f"proj_model_{model_id}_{name}",
                        )])
                    rows.append([Button(label="« Back", value=f"proj_edit_{name}")])
                    await self._transport.edit_text(
                        click.message,
                        f"Select model for '{name}':\nCurrent: {current or 'default'}",
                        buttons=Buttons(rows=rows),
                    )
                elif field == "respond_in_groups":
                    # Bool field — render an explicit On/Off picker rather than
                    # asking for free-form text. Same UX as the model picker:
                    # show current state with the ● glyph, single tap toggles.
                    projects = self._load_projects()
                    current = bool(projects.get(name, {}).get("respond_in_groups", False))
                    rows = [
                        [Button(
                            label=("● On" if current else "On"),
                            value=f"proj_rig_on_{name}",
                        )],
                        [Button(
                            label=("● Off" if not current else "Off"),
                            value=f"proj_rig_off_{name}",
                        )],
                        [Button(label="« Back", value=f"proj_edit_{name}")],
                    ]
                    await self._transport.edit_text(
                        click.message,
                        f"Respond in groups for '{name}':\n"
                        f"Current: {'On' if current else 'Off'}\n\n"
                        "When On, this bot responds in Telegram groups to "
                        "@mentions or replies to its own messages.",
                        buttons=Buttons(rows=rows),
                    )
                else:
                    if ctx_user_data is not None:
                        ctx_user_data["pending_edit"] = {"name": name, "field": field}
                    await self._transport.edit_text(
                        click.message,
                        f"Enter new value for {field} of '{name}':\n(/cancel to abort)",
                    )

        elif value.startswith("proj_model_"):
            if not await self._require_executor_button(click):
                return
            rest = value[len("proj_model_"):]
            valid_ids = {m[0] for m in MODEL_OPTIONS}
            model_id = None
            name = None
            for mid in valid_ids:
                if rest.startswith(mid + "_"):
                    model_id = mid
                    name = rest[len(mid) + 1:]
                    break
            if model_id and name:
                projects = self._load_projects()
                if name in projects:
                    backend_name = projects[name].get("backend") or "claude"
                    patch_backend_state(
                        name,
                        backend_name,
                        {"model": model_id},
                        self._project_config_path or DEFAULT_CONFIG,
                    )
                current = model_id
                rows = []
                for mid, label in MODEL_OPTIONS:
                    prefix = "● " if current == mid else ""
                    rows.append([Button(
                        label=f"{prefix}{label}",
                        value=f"proj_model_{mid}_{name}",
                    )])
                rows.append([Button(label="« Back", value=f"proj_edit_{name}")])
                label = next((l for m, l in MODEL_OPTIONS if m == model_id), model_id)
                await self._transport.edit_text(
                    click.message,
                    f"Model for '{name}' set to: {label}\nRestart the project to apply.",
                    buttons=Buttons(rows=rows),
                )

        elif value.startswith("proj_rig_on_") or value.startswith("proj_rig_off_"):
            if not await self._require_executor_button(click):
                return
            if value.startswith("proj_rig_on_"):
                new_state = True
                name = value[len("proj_rig_on_"):]
            else:
                new_state = False
                name = value[len("proj_rig_off_"):]
            projects = self._load_projects()
            if name in projects:
                if new_state:
                    projects[name]["respond_in_groups"] = True
                else:
                    # Emit-only-when-True policy: drop the key on Off so
                    # configs round-trip cleanly through save_config.
                    projects[name].pop("respond_in_groups", None)
                self._save_projects(projects)
                rows = [
                    [Button(
                        label=("● On" if new_state else "On"),
                        value=f"proj_rig_on_{name}",
                    )],
                    [Button(
                        label=("● Off" if not new_state else "Off"),
                        value=f"proj_rig_off_{name}",
                    )],
                    [Button(label="« Back", value=f"proj_edit_{name}")],
                ]
                await self._transport.edit_text(
                    click.message,
                    f"Respond in groups for '{name}' set to: "
                    f"{'On' if new_state else 'Off'}\n"
                    "Restart the project to apply.",
                    buttons=Buttons(rows=rows),
                )

        elif value.startswith("global_model_"):
            if not await self._require_executor_button(click):
                return
            model_id = value[len("global_model_"):]
            valid_ids = {m[0] for m in MODEL_OPTIONS}
            if model_id in valid_ids:
                from ..config import load_config, save_config
                cfg_path = self._project_config_path or DEFAULT_CONFIG
                cfg = load_config(cfg_path)
                # Phase 2: write the new ``default_model_claude`` field as the
                # source of truth. ``cfg.default_model`` is kept consistent so
                # the in-memory dataclass stays coherent for the rest of this
                # request; save_config also emits the legacy mirror on disk
                # for downgrade safety.
                cfg.default_model_claude = model_id
                cfg.default_model = model_id
                save_config(cfg, cfg_path)
                label = next((l for m, l in MODEL_OPTIONS if m == model_id), model_id)
                await self._transport.edit_text(
                    click.message,
                    f"Default model set to: {label}\nRestart projects to apply.",
                    buttons=self._global_model_buttons(),
                )

        elif value.startswith("proj_plugins_"):
            name = value[len("proj_plugins_"):]
            available = self._available_plugins()
            assert self._transport is not None
            if not available:
                await self._transport.edit_text(
                    click.message,
                    "No plugins installed.\n\n"
                    "Install the link-project-to-chat-plugins package to add plugins.",
                    buttons=Buttons(rows=[[Button(label="« Back", value=f"proj_info_{name}")]]),
                )
            else:
                await self._transport.edit_text(
                    click.message,
                    f"Plugins for '{name}':\n✓ = active, + = available\n\nRestart required after changes.",
                    buttons=self._plugins_buttons(name),
                )

        elif value.startswith("proj_ptog_"):
            # Plugin toggle changes config state — gate to executor role.
            # Without this, a viewer with manager-bot access could enable
            # arbitrary plugin code on any project. The surrounding
            # _on_button_from_transport try/finally persists any first-contact
            # lock from _auth_identity regardless of allow/deny branch.
            if not await self._require_executor_button(click):
                return
            suffix = value[len("proj_ptog_"):]
            if "|" not in suffix:
                return
            plugin_name, name = suffix.rsplit("|", 1)
            projects = self._load_projects()
            if name not in projects:
                return
            plugins = projects[name].get("plugins", [])
            active_names = [p.get("name") for p in plugins]
            if plugin_name in active_names:
                plugins = [p for p in plugins if p.get("name") != plugin_name]
            else:
                plugins = plugins + [{"name": plugin_name}]
            projects[name]["plugins"] = plugins
            self._save_projects(projects)
            assert self._transport is not None
            # Synchronize the running project bot with the freshly-saved
            # plugin set. Without this, a 'disabled' plugin keeps serving
            # commands until the operator notices and restarts manually —
            # the security policy and the runtime state silently disagree.
            footer = "Will take effect on next start (project bot is not running)."
            if self._pm.status(name) == "running":
                self._pm.stop(name)
                if self._pm.start(name):
                    footer = "Restart applied — bot is now running with the new plugin set."
                else:
                    footer = (
                        "Plugin set saved, but the project bot failed to "
                        "restart. Start it manually to apply changes."
                    )
            await self._transport.edit_text(
                click.message,
                (
                    f"Plugins for '{name}':\n✓ = active, + = available\n\n"
                    f"{footer}"
                ),
                buttons=self._plugins_buttons(name),
            )

        elif value.startswith("proj_remove_"):
            if not await self._require_executor_button(click):
                return
            name = value[len("proj_remove_"):]
            projects = self._load_projects()
            removal_text = None
            if name in projects:
                self._pm.stop(name)
                notes, failures = await self._cleanup_managed_project_resources(projects[name])
                del projects[name]
                self._save_projects(projects)
                if failures:
                    removal_text = (
                        f"Removed '{name}', but cleanup had issues:\n- "
                        + "\n- ".join(failures)
                    )
                    if notes:
                        removal_text += "\n\nCompleted:\n- " + "\n- ".join(notes)
                elif notes:
                    removal_text = (
                        f"Removed '{name}' and cleaned up manager-owned resources:\n- "
                        + "\n- ".join(notes)
                    )
            buttons = self._list_buttons()
            await self._transport.edit_text(
                click.message,
                removal_text or (self._projects_text() if buttons else "No projects configured."),
                buttons=buttons,
            )

        elif value == "team_back":
            buttons = self._teams_list_buttons()
            if buttons is None:
                await self._transport.edit_text(click.message, "No teams configured.")
            else:
                teams = self._load_teams()
                await self._transport.edit_text(
                    click.message, f"Teams ({len(teams)}):", buttons=buttons,
                )

        elif value.startswith("team_info_"):
            team_name = value[len("team_info_"):]
            teams = self._load_teams()
            team = teams.get(team_name)
            if team is None:
                await self._transport.edit_text(
                    click.message, f"Team '{team_name}' not found.",
                )
            else:
                await self._transport.edit_text(
                    click.message,
                    self._team_detail_text(team_name, team),
                    buttons=self._team_detail_buttons(team_name, team),
                )

        elif value.startswith("team_start_"):
            if not await self._require_executor_button(click):
                return
            team_name = value[len("team_start_"):]
            teams = self._load_teams()
            team = teams.get(team_name)
            if team is None:
                await self._transport.edit_text(
                    click.message, f"Team '{team_name}' not found.",
                )
            else:
                for role in team.bots:
                    self._pm.start_team(team_name, role)
                await self._transport.edit_text(
                    click.message,
                    self._team_detail_text(team_name, team),
                    buttons=self._team_detail_buttons(team_name, team),
                )

        elif value.startswith("team_stop_"):
            if not await self._require_executor_button(click):
                return
            team_name = value[len("team_stop_"):]
            teams = self._load_teams()
            team = teams.get(team_name)
            if team is None:
                await self._transport.edit_text(
                    click.message, f"Team '{team_name}' not found.",
                )
            else:
                for role in team.bots:
                    self._pm.stop(f"team:{team_name}:{role}")
                await self._transport.edit_text(
                    click.message,
                    self._team_detail_text(team_name, team),
                    buttons=self._team_detail_buttons(team_name, team),
                )

        elif value == "setup_gh":
            if not await self._require_executor_button(click):
                return
            if ctx_user_data is not None:
                ctx_user_data["setup_awaiting"] = "github_pat"
            await self._transport.edit_text(
                click.message, "Paste your GitHub Personal Access Token:",
            )

        elif value == "setup_api":
            if not await self._require_executor_button(click):
                return
            if ctx_user_data is not None:
                ctx_user_data["setup_awaiting"] = "api_id"
            await self._transport.edit_text(
                click.message, "Enter your Telegram API ID (from my.telegram.org):",
            )

        elif value == "setup_telethon":
            if not await self._require_executor_button(click):
                return
            if ctx_user_data is not None:
                ctx_user_data["setup_awaiting"] = "phone"
            await self._transport.edit_text(
                click.message,
                "Enter your phone number (with country code, e.g. +1234567890):",
            )

        elif value == "setup_voice":
            if not await self._require_executor_button(click):
                return
            if ctx_user_data is not None:
                ctx_user_data["setup_awaiting"] = "stt_backend"
            await self._transport.edit_text(
                click.message,
                "Choose STT backend:\n"
                "• whisper-api — OpenAI Whisper API (recommended)\n"
                "• whisper-cli — Local whisper.cpp\n"
                "• off — Disable voice\n\n"
                "Type your choice:",
            )

        elif value == "setup_done":
            if not await self._require_executor_button(click):
                return
            await self._transport.edit_text(click.message, "Setup complete.")

    def _register_transport_commands(self, app=None) -> None:
        """Register the manager's transport-native command handlers.

        Wraps every handler with a try/finally that calls
        ``_persist_auth_if_dirty`` on every exit path (success, deny,
        rate-limited, exception). Without this, first-contact identity
        locks appended by ``_auth_identity`` → ``_get_user_role`` inside
        the handler would be lost on restart — letting a spoofer who
        lands first after restart bind their own native_id to the
        username. Mirrors ProjectBot.build()'s ``_wrap_with_persist``
        pattern (commit b396b1e).

        ``app`` is optional: ``build()`` passes the PTB Application so the
        bridge handlers also register, but tests can call this method
        with the Application omitted to exercise the wrapping in
        isolation against a ``FakeTransport``.

        TODO(spec #1): Underscore-method access needed because the manager
        can't use attach_telegram_routing (conflicts with
        ConversationHandler CallbackQueryHandlers). Consider elevating
        _dispatch_{command,button} to public API in the
        Conversation-primitive spec.
        """
        ported_commands = {
            "projects": self._on_projects_from_transport,
            "teams": self._on_teams_from_transport,
            "version": self._on_version_from_transport,
            "help": self._on_help_from_transport,
            "start_all": self._on_start_all_from_transport,
            "stop_all": self._on_stop_all_from_transport,
            "model": self._on_model_from_transport,
            "setup": self._on_setup_from_transport,
        }

        def _wrap_with_persist(handler):
            async def _wrapped(arg):
                try:
                    await handler(arg)
                finally:
                    await self._persist_auth_if_dirty()
            return _wrapped

        for name, handler in ported_commands.items():
            self._transport.on_command(name, _wrap_with_persist(handler))
            if app is not None:
                app.add_handler(CommandHandler(name, self._transport.bridge_command(name)))

        # Task 6: user-management commands. Replace the pre-v1.0 legacy
        # handlers (_on_users_from_transport / _on_add_user_from_transport /
        # _on_remove_user_from_transport, deleted from this file) with role-
        # aware variants that operate on Config.allowed_users exclusively.
        # /users is viewer-allowed; all writes require executor role.
        _new_manager_commands = {
            "users": self._on_users,
            "add_user": self._on_add_user,
            "remove_user": self._on_remove_user,
            "promote_user": self._on_promote_user,
            "demote_user": self._on_demote_user,
            "reset_user_identity": self._on_reset_user_identity,
        }
        for _name, _handler in _new_manager_commands.items():
            self._transport.on_command(_name, _wrap_with_persist(_handler))
            if app is not None:
                app.add_handler(CommandHandler(_name, self._transport.bridge_command(_name)))

    async def _post_init(self, app) -> None:
        await app.bot.delete_webhook(drop_pending_updates=True)
        await app.bot.set_my_commands(COMMANDS)
        # Bring up the shared Telethon client used by /create_team's
        # BotFatherClient and supergroup deletion. Project bots own their own
        # TeamRelay instances (see #0c) — manager no longer starts relays.
        try:
            await self._start_telethon_client()
        except Exception:
            logger.exception("Starting Telethon client failed (team bots will still run)")

    def _adopt_setup_client(self, bf, client) -> None:
        """Promote a freshly-authenticated Telethon client from the /setup
        wizard to the shared ``self._telethon_client`` slot.

        Without this, the wizard leaves a connected ``TelegramClient`` against
        ``telethon.session`` after sign_in, and the next /create_project opens
        a second client against the same SQLite file — surfacing as
        ``database is locked`` during disconnect. Mirrors the same adoption
        pattern used by /create_team (commit 93c2048).
        """
        if getattr(self, "_telethon_client", None) is None:
            self._telethon_client = client
            bf._owns_client = False

    async def _start_telethon_client(self) -> None:
        """Initialize the shared Telethon client used by /create_team and
        supergroup deletion.

        Skipped silently when Telegram API creds are missing, telethon is not
        installed, or the telethon.session file is absent — a solo-project
        deployment never needs this and shouldn't pay a startup cost for it.
        """
        from ..config import load_config
        cfg_path = self._project_config_path or DEFAULT_CONFIG
        try:
            config = load_config(cfg_path)
        except Exception:
            logger.exception("Could not load config for Telethon client")
            return
        if not config.telegram_api_id or not config.telegram_api_hash:
            logger.info("Telethon client skipped — Telegram API credentials unset")
            return
        session_path = cfg_path.parent / "telethon.session"
        if not session_path.exists():
            logger.info("Telethon client skipped — session at %s not found", session_path)
            return
        try:
            from telethon import TelegramClient
        except ImportError:
            logger.info("Telethon client skipped — telethon not installed")
            return

        self._telethon_client = TelegramClient(
            str(session_path), config.telegram_api_id, config.telegram_api_hash,
        )
        try:
            await self._telethon_client.connect()
            if not await self._telethon_client.is_user_authorized():
                logger.warning("Telethon session not authorized; disconnecting client")
                await self._telethon_client.disconnect()
                self._telethon_client = None
                return
        except Exception:
            logger.exception("Telethon client connect failed")
            self._telethon_client = None
            return

    async def _post_stop(self, app) -> None:
        """Called by python-telegram-bot on shutdown — terminate spawned
        project bot subprocesses and disconnect Telethon client.

        Without stop_all() here, project bots get orphaned on Ctrl+C and keep
        polling Telegram with their tokens; the next manager start hits
        "Conflict: terminated by other getUpdates request".

        Project bots own their own TeamRelay instances (see #0c), so the manager
        no longer has relays to stop here.
        """
        try:
            self._pm.stop_all()
        except Exception:
            logger.exception("Failed to stop project bot subprocesses on shutdown")
        if self._telethon_client is not None:
            try:
                await self._telethon_client.disconnect()
            except Exception:
                logger.exception("Failed to disconnect Telethon client on shutdown")

    def build(self):
        from ..transport.telegram import TelegramTransport
        # concurrent_updates=False matches prior ApplicationBuilder default; the
        # manager has not opted into concurrent updates. post_init is attached
        # below because the manager's lifecycle is run_polling() (which runs
        # the Application's post_init) rather than transport.start() (which
        # runs the transport's own post-init logic).
        self._transport = TelegramTransport.build(self._token, concurrent_updates=False)
        self._app = self._transport.app  # alias preserves existing add_handler call sites
        self._app.post_init = self._post_init
        self._app.post_stop = self._post_stop
        app = self._app

        # Fully-ported commands (spec #0c Tasks 8-9). Registered via the
        # helper below so the persist-tail wrap fires on every exit path.
        self._register_transport_commands(app)

        # Legacy commands — still use Update/ctx internals; bridged to PTB
        # directly until their respective tasks port them.
        for name, handler in {
            "edit_project": self._on_edit_project,
        }.items():
            app.add_handler(CommandHandler(name, handler))

        app.add_handler(ConversationHandler(
            entry_points=[CommandHandler("add_project", self._on_add_project)],
            states={
                self.ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, self._add_name)],
                self.ADD_PATH: [MessageHandler(filters.TEXT & ~filters.COMMAND, self._add_path)],
                self.ADD_TOKEN: [
                    CommandHandler("skip", self._add_token),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self._add_token),
                ],
                self.ADD_USERNAME: [
                    CommandHandler("skip", self._add_username),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self._add_username),
                ],
                self.ADD_MODEL: [
                    CommandHandler("skip", self._add_model),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self._add_model),
                ],
            },
            fallbacks=[],
        ))

        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="If 'per_message=False'", category=UserWarning)
            app.add_handler(ConversationHandler(
                entry_points=[CommandHandler("create_project", self._on_create_project)],
                states={
                    self.CREATE_PROVIDER_PICK: [
                        CallbackQueryHandler(
                            self._create_provider_pick_callback, pattern=r"^provider:",
                        ),
                    ],
                    self.CREATE_SOURCE: [CallbackQueryHandler(self._create_source_callback)],
                    self.CREATE_REPO_LIST: [CallbackQueryHandler(self._create_repo_list_callback)],
                    self.CREATE_REPO_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, self._create_repo_url)],
                    self.CREATE_NAME: [CallbackQueryHandler(self._create_name_callback)],
                    self.CREATE_NAME_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self._create_name_input)],
                    self.CREATE_BOT: [
                        CallbackQueryHandler(self._create_bot_callback),
                        MessageHandler(filters.TEXT & ~filters.COMMAND, self._create_bot_token_input),
                    ],
                    self.CREATE_CLONE: [CallbackQueryHandler(self._create_clone_callback)],
                },
                fallbacks=[CommandHandler("cancel", self._create_cancel)],
            ))

            app.add_handler(ConversationHandler(
                entry_points=[CommandHandler("create_team", self._on_create_team)],
                states={
                    self.CREATE_TEAM_SOURCE: [
                        CallbackQueryHandler(self._create_team_source_callback, pattern=r"^ct_source:"),
                    ],
                    self.CREATE_TEAM_REPO_LIST: [
                        CallbackQueryHandler(
                            lambda u, c: self._create_repo_list_callback(u, c, user_data_key="create_team"),
                        ),
                    ],
                    self.CREATE_TEAM_REPO_URL: [
                        MessageHandler(
                            filters.TEXT & ~filters.COMMAND,
                            lambda u, c: self._create_repo_url(u, c, user_data_key="create_team"),
                        ),
                    ],
                    self.CREATE_TEAM_NAME: [
                        MessageHandler(filters.TEXT & ~filters.COMMAND, self._create_team_name),
                    ],
                    self.CREATE_TEAM_PERSONA_MGR: [
                        CallbackQueryHandler(self._create_team_persona_mgr_callback, pattern=r"^ct_persona_mgr:"),
                    ],
                    self.CREATE_TEAM_PERSONA_DEV: [
                        CallbackQueryHandler(self._create_team_persona_dev_callback, pattern=r"^ct_persona_dev:"),
                    ],
                },
                fallbacks=[CommandHandler("cancel", self._create_cancel)],
            ))

            app.add_handler(ConversationHandler(
                entry_points=[CommandHandler("delete_team", self._on_delete_team)],
                states={
                    self.DELETE_TEAM_CONFIRM: [
                        CallbackQueryHandler(
                            self._delete_team_confirm_callback,
                            pattern=r"^dt_(pick:|confirm$|cancel$)",
                        ),
                    ],
                },
                fallbacks=[CommandHandler("cancel", self._create_cancel)],
            ))

        app.add_handler(CommandHandler("cancel", self._edit_cancel))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._edit_field_save))
        # Spec #0c Task 10: global inline-button menus consume ButtonClick via the
        # transport. PTB is bridged so wizard-internal CallbackQueryHandlers (which
        # ConversationHandler routes by state) keep ownership of their clicks; this
        # last-position handler picks up everything else.
        self._transport.on_button(self._on_button_from_transport)
        app.add_handler(CallbackQueryHandler(self._transport.bridge_button()))
        return app
