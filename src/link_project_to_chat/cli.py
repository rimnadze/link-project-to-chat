from __future__ import annotations

import logging
from pathlib import Path

import click

from .backends.claude import PERMISSION_MODES
from .config import (
    DEFAULT_CONFIG,
    load_config,
    parse_user_bool,
    patch_backend_state,
    resolve_permissions,
    resolve_start_model,
    save_config,
)


_PERMISSION_VALUES = set(PERMISSION_MODES) | {"dangerously-skip-permissions"}


def _normalize_permissions_edit(field: str, value: str) -> str:
    if field == "dangerously_skip_permissions":
        parsed = parse_user_bool(value)
        if parsed is True:
            return "dangerously-skip-permissions"
        if parsed is False:
            return "default"
        raise SystemExit(
            "dangerously_skip_permissions expects one of: true, false, yes, no, on, off, 1, 0."
        )

    if value not in _PERMISSION_VALUES:
        allowed = ", ".join(PERMISSION_MODES)
        raise SystemExit(
            "Invalid permissions value. Use one of: "
            f"{allowed}, dangerously-skip-permissions"
        )
    return value


@click.group()
@click.option(
    "--config",
    "config_path",
    type=click.Path(),
    default=None,
    help="Config file path (default: ~/.link-project-to-chat/config.json)",
)
@click.pass_context
def main(ctx, config_path: str | None):
    """link-project-to-chat: Chat with Claude about a project via Telegram."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = Path(config_path) if config_path else DEFAULT_CONFIG


@main.group(invoke_without_command=True)
@click.pass_context
def projects(ctx):
    """Manage linked projects."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@projects.command("list")
@click.pass_context
def projects_list(ctx):
    """List all linked projects."""
    config = load_config(ctx.obj["config_path"])
    if not config.projects:
        return click.echo("No projects linked.")
    for name, proj in config.projects.items():
        usernames = [u.username for u in proj.allowed_users]
        users = ", ".join(usernames) if usernames else "(global)"
        click.echo(f"  {name}: {proj.path}  [{users}]")


@projects.command("add")
@click.option("--name", required=True, help="Project name")
@click.option(
    "--path",
    "project_path",
    required=True,
    type=click.Path(exists=True, file_okay=False, resolve_path=True),
    help="Project directory",
)
@click.option("--token", required=True, help="Telegram bot token from BotFather")
@click.option("--username", default=None, help="Allowed Telegram username")
@click.option("--model", default=None, help="Claude model (haiku/sonnet/opus)")
@click.option(
    "--permission-mode",
    type=click.Choice(["default", "acceptEdits", "bypassPermissions", "dontAsk", "plan", "auto"]),
    default=None,
    help="Claude permission mode",
)
@click.option(
    "--dangerously-skip-permissions",
    "skip_permissions",
    is_flag=True,
    default=False,
    help="Allow Claude to skip all permission checks",
)
@click.option(
    "--respond-in-groups/--no-respond-in-groups",
    "respond_in_groups",
    default=False,
    help="Respond in Telegram groups when @mentioned or replied to (default off)",
)
@click.option(
    "--safety-prompt", "safety_prompt", default=None,
    help='Custom safety guardrail text. Empty string disables safety. '
         'Default (omit flag) uses the built-in GitLab guardrail.',
)
@click.pass_context
def projects_add(
    ctx,
    name: str,
    project_path: str,
    token: str,
    username: str | None,
    model: str | None,
    permission_mode: str | None,
    skip_permissions: bool,
    respond_in_groups: bool,
    safety_prompt: str | None,
):
    """Add a project."""
    from .manager.config import load_project_configs, save_project_configs

    cfg_path = ctx.obj["config_path"]
    projects = load_project_configs(cfg_path)
    if name in projects:
        raise SystemExit(f"Project '{name}' already exists.")
    entry: dict = {"path": str(Path(project_path).resolve()), "telegram_bot_token": token}
    if username:
        # Translate ``--username X`` to the modern ``allowed_users`` shape.
        # Writing the legacy flat ``username`` key would seem to succeed but
        # collide with any pre-existing ``allowed_users`` on the next load -
        # the explicit list wins and the new user is silently dropped. The
        # migration helper only re-synthesizes when allowed_users is empty.
        norm = username.lower().lstrip("@")
        entry["allowed_users"] = [{"username": norm, "role": "executor"}]
    # Canonical post-v1.0 shape: backend + backend_state["claude"][*]. The
    # pre-v1.0 top-level legacy mirror (model / permissions) was kept one
    # release for downgrade safety; v1.0.0 stopped emitting it.
    entry["backend"] = "claude"
    claude_state: dict[str, object] = {}
    if model:
        claude_state["model"] = model
    if skip_permissions:
        claude_state["permissions"] = "dangerously-skip-permissions"
    elif permission_mode:
        claude_state["permissions"] = permission_mode
    entry["backend_state"] = {"claude": claude_state} if claude_state else {}
    # Emit-only-when-True policy: matches save_config, keeps the default-off
    # case from polluting on-disk entries with a redundant `false`.
    if respond_in_groups:
        entry["respond_in_groups"] = True
    if safety_prompt is not None:
        entry["safety_prompt"] = safety_prompt
    save_project_configs(projects | {name: entry}, cfg_path)
    click.echo(f"Added '{name}' -> {project_path}")


@projects.command("remove")
@click.argument("name")
@click.pass_context
def projects_remove(ctx, name: str):
    """Remove a project."""
    from .manager.config import load_project_configs, save_project_configs

    cfg_path = ctx.obj["config_path"]
    projects = load_project_configs(cfg_path)
    if name not in projects:
        raise SystemExit(f"Project '{name}' not found.")
    del projects[name]
    save_project_configs(projects, cfg_path)
    click.echo(f"Removed '{name}'.")


@projects.command("edit")
@click.argument("name")
@click.argument("field")
@click.argument("value")
@click.pass_context
def projects_edit(ctx, name: str, field: str, value: str):
    """Edit a project field.

    Modern permissions use the `permissions` field. Legacy `permission_mode`
    and `dangerously_skip_permissions` aliases are accepted and normalized.
    """
    from .manager.config import load_project_configs, save_project_configs

    _EDITABLE = (
        "name",
        "path",
        "token",
        "username",
        "model",
        "permissions",
        "permission_mode",
        "dangerously_skip_permissions",
        "respond_in_groups",
        "safety_prompt",
    )
    cfg_path = ctx.obj["config_path"]
    projects = load_project_configs(cfg_path)
    if name not in projects:
        raise SystemExit(f"Project '{name}' not found.")

    if field == "name":
        if value in projects:
            raise SystemExit(f"Project '{value}' already exists.")
        projects[value] = projects.pop(name)
        save_project_configs(projects, cfg_path)
        click.echo(f"Renamed '{name}' to '{value}'.")
    elif field == "path":
        if not Path(value).exists():
            raise SystemExit(f"Path does not exist: {value}")
        projects[name]["path"] = value
        save_project_configs(projects, cfg_path)
        click.echo(f"Updated '{name}' path to {value}.")
    elif field == "token":
        projects[name]["telegram_bot_token"] = value
        save_project_configs(projects, cfg_path)
        click.echo(f"Updated '{name}' token.")
    elif field == "username":
        # Translate ``username`` to ``allowed_users`` so the write actually
        # authorizes the new user. Writing the legacy flat key directly would
        # collide with an existing ``allowed_users`` list on next load - the
        # explicit list wins and the new user is silently dropped (the loader
        # only re-synthesizes legacy fields when allowed_users is empty).
        norm = value.lower().lstrip("@")
        projects[name]["allowed_users"] = [{"username": norm, "role": "executor"}]
        # Drop any stale legacy ``username`` key so the source of truth is
        # the explicit allowed_users list going forward.
        projects[name].pop("username", None)
        save_project_configs(projects, cfg_path)
        click.echo(
            f"Updated '{name}' allowed_users to [{norm}] (executor). "
            f"For multi-user lists use `configure --add-user`/`--remove-user` "
            f"or the manager bot.",
            err=True,
        )
    elif field == "model":
        # Phase 2: write the new shape into backend_state[<active_backend>]
        # and mirror the legacy flat key for downgrade safety.
        backend_name = projects[name].get("backend") or "claude"
        patch_backend_state(name, backend_name, {"model": value}, cfg_path)
        click.echo(f"Updated '{name}' model to {value}.")
    elif field in ("permissions", "permission_mode", "dangerously_skip_permissions"):
        normalized = _normalize_permissions_edit(field, value)
        projects = load_project_configs(cfg_path)
        backend_name = projects[name].get("backend") or "claude"
        patch_backend_state(name, backend_name, {"permissions": normalized}, cfg_path)
        # Drop legacy alias keys so the source of truth is the canonical key.
        projects = load_project_configs(cfg_path)
        if "permission_mode" in projects[name] or "dangerously_skip_permissions" in projects[name]:
            projects[name].pop("permission_mode", None)
            projects[name].pop("dangerously_skip_permissions", None)
            save_project_configs(projects, cfg_path)
        click.echo(f"Updated '{name}' permissions to {normalized}.")
    elif field == "respond_in_groups":
        parsed = parse_user_bool(value)
        if parsed is True:
            projects[name]["respond_in_groups"] = True
        elif parsed is False:
            # Emit-only-when-True policy: strip the key on false rather than
            # writing an explicit `false`, so on-disk shape stays minimal and
            # round-trips through save_config without noise.
            projects[name].pop("respond_in_groups", None)
        else:
            raise SystemExit(
                f"Invalid bool for respond_in_groups: {value!r}. "
                f"Use one of: true, false, 1, 0, yes, no, on, off."
            )
        save_project_configs(projects, cfg_path)
        click.echo(f"Updated '{name}' respond_in_groups to {value}.")
    elif field == "safety_prompt":
        # "default" is the operator-friendly way to strip the key (back to
        # DEFAULT_SAFETY_SYSTEM_PROMPT on next bot start). Empty string is
        # the explicit-disable signal. Any other value is a custom prompt.
        if value == "default":
            projects[name].pop("safety_prompt", None)
        else:
            projects[name]["safety_prompt"] = value
        save_project_configs(projects, cfg_path)
        click.echo(f"Updated '{name}' safety_prompt.")
    else:
        raise SystemExit(f"Unknown field. Use: {', '.join(_EDITABLE)}")


@main.command()
@click.option("--username", default=None, help="(DEPRECATED - use --add-user) Allowed Telegram username.")
@click.option("--remove-username", default=None, help="(DEPRECATED - use --remove-user) Remove an allowed username.")
@click.option(
    "--add-user", "add_user", default=None,
    help="Add an AllowedUser. Format: 'username' or 'username:role' (role = viewer|executor; default executor).",
)
@click.option("--remove-user", "remove_user", default=None, help="Remove an AllowedUser by username.")
@click.option(
    "--reset-user-identity", "reset_user_identity", default=None,
    help=(
        "Clear the locked_identities for a user (re-locks on next contact). "
        "Use 'username:transport' to clear only one transport."
    ),
)
@click.option("--manager-token", default=None, help="Telegram bot token for the manager bot")
@click.option(
    "--gitlab-pat",
    "gitlab_pat",
    default=None,
    help="GitLab personal access token (stored in config.json with 0o600 perms).",
)
@click.option(
    "--gitlab-host",
    "gitlab_host",
    default=None,
    help="GitLab host (default: gitlab.com). Bare hostname only -- no scheme, no path.",
)
@click.pass_context
def configure(
    ctx,
    username: str | None,
    remove_username: str | None,
    add_user: str | None,
    remove_user: str | None,
    reset_user_identity: str | None,
    manager_token: str | None,
    gitlab_pat: str | None,
    gitlab_host: str | None,
):
    """Configure allowed users and/or manager bot token."""
    from .config import AllowedUser

    if not any([
        username, remove_username, add_user, remove_user, reset_user_identity,
        manager_token, gitlab_pat, gitlab_host,
    ]):
        raise SystemExit(
            "Provide at least one of --add-user, --remove-user, --reset-user-identity, "
            "--username, --remove-username, --manager-token, --gitlab-pat, "
            "or --gitlab-host."
        )

    cfg_path = ctx.obj["config_path"]
    config = load_config(cfg_path)

    # Legacy alias handling: route deprecated flags to the new ones with a warning.
    if username is not None:
        click.echo("--username is deprecated; use --add-user instead.", err=True)
        if add_user is None:
            add_user = username
    if remove_username is not None:
        click.echo("--remove-username is deprecated; use --remove-user instead.", err=True)
        if remove_user is None:
            remove_user = remove_username

    def _find(uname: str):
        norm = uname.lstrip("@").lower()
        for u in config.allowed_users:
            if u.username == norm:
                return u
        return None

    if add_user:
        if ":" in add_user:
            uname, role = add_user.split(":", 1)
        else:
            uname, role = add_user, "executor"
        uname = uname.lstrip("@").lower()
        if role not in ("viewer", "executor"):
            raise SystemExit(f"Invalid role {role!r}; must be viewer or executor.")
        existing = _find(uname)
        if existing:
            existing.role = role
        else:
            config.allowed_users.append(AllowedUser(username=uname, role=role))
        save_config(config, cfg_path)
        click.echo(f"Added {uname} ({role}).")

    if remove_user:
        norm = remove_user.lstrip("@").lower()
        config.allowed_users = [u for u in config.allowed_users if u.username != norm]
        save_config(config, cfg_path)
        click.echo(f"Removed {norm}.")

    if reset_user_identity:
        # Parse `USERNAME[:TRANSPORT]` FIRST. Normalizing the entire string
        # (including `:web`) before the split would corrupt the colon-
        # separated form, so split, then normalize each piece.
        if ":" in reset_user_identity:
            uname_part, transport = reset_user_identity.split(":", 1)
        else:
            uname_part, transport = reset_user_identity, None
        norm = uname_part.lstrip("@").lower()
        u = _find(norm)
        if not u:
            raise SystemExit(f"User {norm!r} not in allow-list.")
        if transport is None:
            u.locked_identities = []
        else:
            u.locked_identities = [
                ident for ident in u.locked_identities
                if not ident.startswith(f"{transport}:")
            ]
        save_config(config, cfg_path)
        if transport is None:
            click.echo(f"Cleared all locked identities for {norm}.")
        else:
            click.echo(f"Cleared {transport!r} identities for {norm}.")

    # Accumulate the remaining flag mutations and save once at the end.
    # The user-management blocks above already saved per-action; the flags
    # below (manager token, gitlab credentials) batch into a single write.
    changed = False

    if manager_token:
        config.manager_telegram_bot_token = manager_token
        click.echo(f"Configured manager token: ***{manager_token[-4:]}")
        changed = True

    if gitlab_pat is not None:
        config.gitlab_pat = gitlab_pat
        changed = True
        click.echo("GitLab PAT saved.")

    if gitlab_host is not None:
        if "://" in gitlab_host or "/" in gitlab_host.rstrip("/"):
            raise click.UsageError(
                "--gitlab-host must be a bare hostname (e.g. gitlab.example.com); "
                "no scheme, no path."
            )
        config.gitlab_host = gitlab_host.rstrip("/")
        changed = True
        click.echo(f"GitLab host set to {config.gitlab_host}.")

    if changed:
        save_config(config, cfg_path)


@main.command()
@click.option(
    "--project", default=None, help="Project name (if multiple are configured)"
)
@click.option(
    "--team", default=None, help="Start a team bot (mutually exclusive with --project)"
)
@click.option(
    "--role", default=None, type=click.Choice(["manager", "dev"]), help="Which team bot role to start"
)
@click.option(
    "--path",
    "project_path",
    type=click.Path(exists=True, file_okay=False, resolve_path=True),
    default=None,
    help="Project directory (use instead of config)",
)
@click.option(
    "--token", default=None, help="Telegram bot token (use instead of config)"
)
@click.option(
    "--username", default=None, help="Allowed Telegram username (overrides config)"
)
@click.option("--session-id", default=None, help="Resume a Claude session by ID")
@click.option("--model", default=None, help="Claude model (haiku/sonnet/opus)")
@click.option(
    "--dangerously-skip-permissions",
    "skip_permissions",
    is_flag=True,
    default=False,
    help="Allow Claude to skip all permission checks (use with caution)",
)
@click.option(
    "--permission-mode",
    type=click.Choice(["default", "acceptEdits", "bypassPermissions", "dontAsk", "plan", "auto"]),
    default=None,
    help="Claude permission mode",
)
@click.option(
    "--allowed-tools",
    default=None,
    help='Comma-separated list of allowed tools (e.g. "Bash(git:*),Edit,Read")',
)
@click.option(
    "--disallowed-tools",
    default=None,
    help='Comma-separated list of disallowed tools (e.g. "Bash(rm:*),Write")',
)
@click.option(
    "--transport",
    "transport_kind",
    type=click.Choice(["telegram", "web", "google_chat"]),
    default="telegram",
    show_default=True,
    help="Which transport to run the bot on.",
)
@click.option(
    "--port",
    "web_port",
    type=int,
    default=8080,
    help="Listen port (web transport only).",
)
@click.option("--google-chat-host", default=None, help="Host override for Google Chat transport.")
@click.option("--google-chat-port", type=int, default=None, help="Port override for Google Chat transport.")
@click.option("--google-chat-public-url", default=None, help="Public URL override for Google Chat transport.")
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
@click.pass_context
def start(
    ctx,
    project: str | None,
    team: str | None,
    role: str | None,
    project_path: str | None,
    token: str | None,
    username: str | None,
    session_id: str | None,
    model: str | None,
    skip_permissions: bool,
    permission_mode: str | None,
    allowed_tools: str | None,
    disallowed_tools: str | None,
    transport_kind: str,
    web_port: int,
    google_chat_host: str | None,
    google_chat_port: int | None,
    google_chat_public_url: str | None,
    google_chat_config_json: str | None,
):
    """Start the Telegram bot.

    Use --path and --token to run without a config file, or use config.
    """
    from .bot import run_bot, run_bots

    allowed = [t.strip() for t in allowed_tools.split(",") if t.strip()] if allowed_tools else None
    disallowed = [t.strip() for t in disallowed_tools.split(",") if t.strip()] if disallowed_tools else None

    cfg_path = ctx.obj["config_path"]

    if project_path and token:
        p = Path(project_path).resolve()
        # Ad-hoc --path/--token runs bypass config loading entirely, so voice
        # STT is unavailable on this path. Users who want voice should either
        # configure a project in the global config and use --project, or rely
        # on the default (no --path, no --token) startup flow.
        from .config import AllowedUser
        adhoc_uname = username.lower().lstrip("@") if username else None
        adhoc_allowed_users = (
            [AllowedUser(username=adhoc_uname, role="executor")] if adhoc_uname else None
        )
        run_bot(
            name=p.name,
            path=p,
            token=token,
            allowed_usernames=[adhoc_uname] if adhoc_uname else [],
            allowed_users=adhoc_allowed_users,
            auth_source="project",
            session_id=session_id,
            model=model,
            skip_permissions=skip_permissions,
            permission_mode=permission_mode,
            allowed_tools=allowed,
            disallowed_tools=disallowed,
            transport_kind=transport_kind,
            web_port=web_port,
        )
        return

    config = load_config(cfg_path)

    if google_chat_config_json:
        # ProcessManager (Task 7) spawns google_chat subprocesses with the
        # per-project resolved GoogleChatConfig serialized into this flag, so
        # the child doesn't re-resolve the merge from disk. Every field is
        # already merged upstream, so this is a direct construct that replaces
        # config.google_chat wholesale.
        import json as _json
        from dataclasses import fields as _fields
        from .config import GoogleChatConfig
        try:
            raw = _json.loads(google_chat_config_json)
        except _json.JSONDecodeError as exc:
            raise click.BadParameter(
                f"--google-chat-config-json: invalid JSON ({exc})",
                param_hint="--google-chat-config-json",
            ) from exc
        if not isinstance(raw, dict):
            raise click.BadParameter(
                "--google-chat-config-json must be a JSON object",
                param_hint="--google-chat-config-json",
            )
        allowed = {f.name for f in _fields(GoogleChatConfig)}
        unknown = set(raw) - allowed
        if unknown:
            import logging
            logging.getLogger(__name__).warning(
                "ignoring unknown --google-chat-config-json keys: %s",
                sorted(unknown),
            )
        config.google_chat = GoogleChatConfig(
            **{k: v for k, v in raw.items() if k in allowed}
        )

    if google_chat_host is not None:
        config.google_chat.host = google_chat_host
    if google_chat_port is not None:
        config.google_chat.port = google_chat_port
    if google_chat_public_url is not None:
        config.google_chat.public_url = google_chat_public_url

    from .config import resolve_project_allowed_users

    if config.migration_pending:
        click.echo("Migrating config.json from legacy auth fields to allowed_users...", err=True)
        save_config(config, cfg_path)
        click.echo("Migration complete.", err=True)

    # Aggregate projects where BOTH project AND global allow-lists are empty.
    # resolve_project_allowed_users returns (users, source); when source is
    # "global" and the global list is also empty, the project will fail-closed.
    empty: list[str] = []
    for name, proj in config.projects.items():
        users, _source = resolve_project_allowed_users(proj, config)
        if not users:
            empty.append(name)
    if empty:
        import logging as _logging
        _logging.getLogger(__name__).critical(
            "Projects with no users authorized at either project or global scope "
            "(will reject all messages): %s. "
            "Add users via `configure --add-user` or the manager bot.",
            ", ".join(empty),
        )

    from .transcriber import create_transcriber, create_synthesizer

    transcriber = None
    if config.stt_backend:
        try:
            transcriber = create_transcriber(
                config.stt_backend,
                openai_api_key=config.openai_api_key,
                whisper_model=config.whisper_model,
                whisper_language=config.whisper_language,
            )
        except (ImportError, ValueError) as e:
            click.echo(f"Warning: Voice disabled - {e}", err=True)

    synthesizer = None
    if config.tts_backend:
        try:
            synthesizer = create_synthesizer(
                config.tts_backend,
                openai_api_key=config.openai_api_key,
                tts_model=config.tts_model,
                tts_voice=config.tts_voice,
            )
        except (ImportError, ValueError) as e:
            click.echo(f"Warning: TTS disabled - {e}", err=True)

    if team:
        if not role:
            raise SystemExit("--role is required when --team is given")
        if team not in config.teams:
            raise SystemExit(f"Team '{team}' not found in config.")
        t = config.teams[team]
        if role not in t.bots:
            raise SystemExit(f"Role '{role}' not in team '{team}'. Known roles: {list(t.bots)}")
        bot_cfg = t.bots[role]
        # Team bots run unattended in a group - a Claude tool-permission prompt
        # would block forever. Default to dangerously-skip-permissions unless
        # the team config explicitly overrides.
        team_skip, team_pm = resolve_permissions(
            bot_cfg.permissions if bot_cfg.permissions is not None else "dangerously-skip-permissions"
        )
        # Look up peer's @username so the bot can address the other role directly.
        peer_username = ""
        for other_role, other_bot in t.bots.items():
            if other_role != role and other_bot.bot_username:
                peer_username = other_bot.bot_username
                break
        bot_state = bot_cfg.backend_state.get(bot_cfg.backend, {})
        run_bot(
            f"{team}_{role}",
            Path(t.path),
            bot_cfg.telegram_bot_token,
            allowed_users=config.allowed_users or None,
            auth_source="global",
            session_id=session_id or bot_state.get("session_id") or bot_cfg.session_id,
            transcriber=transcriber,
            synthesizer=synthesizer,
            team_name=team,
            group_chat_id=t.group_chat_id,
            room=t.room,
            role=role,
            active_persona=bot_cfg.active_persona,
            model=resolve_start_model(
                bot_cfg.backend,
                explicit_model=model,
                backend_model=bot_state.get("model"),
                legacy_claude_model=bot_cfg.model,
                default_model_claude=config.default_model_claude,
                default_model=config.default_model,
            ),
            skip_permissions=team_skip,
            permission_mode=team_pm,
            peer_bot_username=peer_username,
            config_path=cfg_path,
            transport_kind=transport_kind,
            web_port=web_port,
            backend_name=bot_cfg.backend,
            backend_state=bot_cfg.backend_state,
            context_enabled=bot_cfg.context_enabled,
            context_history_limit=bot_cfg.context_history_limit,
            config=config,
        )
        return

    if not config.projects:
        raise SystemExit(
            "No projects. Use --path/--token params or 'projects add' command first."
        )

    if project:
        if project not in config.projects:
            raise SystemExit(f"Project '{project}' not found.")
        proj = config.projects[project]
        effective_allowed_users, project_auth_source = resolve_project_allowed_users(proj, config)
        # ``--username`` CLI override: synthesize a one-user allow-list for
        # this run that wins over the resolved scope. Matches the legacy
        # ``username_override`` arm of the removed ``resolve_project_auth_scope``.
        if username:
            from .config import AllowedUser
            uname_norm = username.lower().lstrip("@")
            effective_allowed_users = [AllowedUser(username=uname_norm, role="executor")]
            project_auth_source = "project"
        proj_skip, proj_pm = resolve_permissions(proj.permissions)
        project_state = proj.backend_state.get(proj.backend, {})
        run_bot(
            project,
            Path(proj.path),
            proj.telegram_bot_token,
            allowed_users=effective_allowed_users or None,
            auth_source=project_auth_source,
            session_id=session_id,
            model=resolve_start_model(
                proj.backend,
                explicit_model=model,
                backend_model=project_state.get("model"),
                legacy_claude_model=proj.model,
            ),
            effort=project_state.get("effort") or proj.effort,
            skip_permissions=skip_permissions or proj_skip,
            permission_mode=permission_mode or proj_pm,
            allowed_tools=allowed,
            disallowed_tools=disallowed,
            transcriber=transcriber,
            synthesizer=synthesizer,
            active_persona=proj.active_persona,
            show_thinking=bool(project_state.get("show_thinking", proj.show_thinking)),
            config_path=cfg_path,
            transport_kind=transport_kind,
            web_port=web_port,
            backend_name=proj.backend,
            backend_state=proj.backend_state,
            context_enabled=proj.context_enabled,
            context_history_limit=proj.context_history_limit,
            respond_in_groups=proj.respond_in_groups,
            config=config,
        )
    else:
        run_bots(
            config,
            model=model,
            skip_permissions=skip_permissions,
            permission_mode=permission_mode,
            allowed_tools=allowed,
            disallowed_tools=disallowed,
            config_path=cfg_path,
            transcriber=transcriber,
            synthesizer=synthesizer,
            transport_kind=transport_kind,
            web_port=web_port,
        )


@main.command()
@click.option("--github-pat", default=None, help="GitHub Personal Access Token")
@click.option("--telegram-api-id", default=None, type=int, help="Telegram API ID (from my.telegram.org)")
@click.option("--telegram-api-hash", default=None, help="Telegram API Hash")
@click.option("--phone", default=None, help="Phone number for Telethon auth (e.g. +995511166693)")
@click.option("--stt-backend", default=None, type=click.Choice(["whisper-api", "whisper-cli", "off"]),
              help="Speech-to-text backend")
@click.option("--openai-api-key", default=None, help="OpenAI API key (for whisper-api)")
@click.option("--whisper-model", default=None, help="Whisper model (default: whisper-1)")
@click.option("--whisper-language", default=None,
              help="Language code (e.g. en, ka). Pass '' to reset to auto-detect.")
@click.option("--tts-backend", default=None, type=click.Choice(["openai", "off"]),
              help="Text-to-speech backend for voice responses")
@click.option("--tts-voice", default=None, help="TTS voice (alloy, ash, ballad, coral, echo, fable, nova, onyx, sage, shimmer)")
@click.pass_context
def setup(ctx, github_pat: str | None, telegram_api_id: int | None, telegram_api_hash: str | None, phone: str | None, stt_backend: str | None, openai_api_key: str | None, whisper_model: str | None, whisper_language: str | None, tts_backend: str | None, tts_voice: str | None):
    """Set up GitHub PAT, Telegram API credentials, and Telethon authentication.

    Run without arguments for interactive setup. Or pass individual options.
    """
    cfg_path = ctx.obj["config_path"]
    config = load_config(cfg_path)
    changed = False

    # Interactive mode ONLY when no flags are provided at all.
    # Passing --stt-backend alone must not trigger GitHub/Telegram prompts.
    # Use `is not None` rather than truthiness so `--whisper-language ""` still
    # counts as an explicit flag (user requesting auto-detect reset).
    interactive = all(v is None for v in [
        github_pat, telegram_api_id, telegram_api_hash, phone,
        stt_backend, openai_api_key, whisper_model, whisper_language,
        tts_backend, tts_voice,
    ])

    # GitHub PAT
    if github_pat or (interactive and click.confirm("Configure GitHub PAT?", default=not config.github_pat)):
        if not github_pat:
            github_pat = click.prompt("GitHub Personal Access Token")
        config.github_pat = github_pat
        changed = True
        click.echo("GitHub PAT saved.")

    # Telegram API credentials
    if telegram_api_id or telegram_api_hash or (interactive and click.confirm("Configure Telegram API credentials?", default=not config.telegram_api_id)):
        if not telegram_api_id:
            telegram_api_id = click.prompt("Telegram API ID (from my.telegram.org)", type=int)
        if not telegram_api_hash:
            telegram_api_hash = click.prompt("Telegram API Hash")
        config.telegram_api_id = telegram_api_id
        config.telegram_api_hash = telegram_api_hash
        changed = True
        click.echo("Telegram API credentials saved.")

    if changed:
        save_config(config, cfg_path)

    # --- Voice STT ---
    config = load_config(cfg_path)  # reload in case previous blocks saved
    voice_changed = False

    # If voice-related flags are passed without --stt-backend, reuse the already
    # configured backend. This supports workflows like rotating an API key
    # (`setup --openai-api-key sk-new`) without re-selecting the backend.
    if stt_backend is None and any(v is not None for v in [openai_api_key, whisper_model, whisper_language]):
        if not config.stt_backend:
            raise click.UsageError(
                "--openai-api-key, --whisper-model, and --whisper-language "
                "require --stt-backend or a previously configured backend."
            )
        stt_backend = config.stt_backend

    if stt_backend is not None or (interactive and click.confirm(
        "Configure voice transcription?",
        default=not config.stt_backend,
    )):
        if stt_backend is None:
            stt_backend = click.prompt(
                "STT backend",
                type=click.Choice(["whisper-api", "whisper-cli", "off"]),
                default=config.stt_backend or "whisper-api",
            )
        if stt_backend == "off":
            config.stt_backend = ""
            voice_changed = True
            click.echo("Voice transcription disabled.")
        else:
            config.stt_backend = stt_backend
            voice_changed = True
            if stt_backend == "whisper-api":
                if not openai_api_key:
                    openai_api_key = click.prompt(
                        "OpenAI API key",
                        default=config.openai_api_key or "",
                    )
                if openai_api_key:
                    config.openai_api_key = openai_api_key
            if whisper_model:
                config.whisper_model = whisper_model
            elif interactive:
                default_model = "whisper-1" if stt_backend == "whisper-api" else "base"
                config.whisper_model = click.prompt(
                    "Whisper model",
                    default=config.whisper_model or default_model,
                )
            # Semantics: `--whisper-language ""` explicitly resets to auto-detect.
            #            `--whisper-language en` sets "en".
            #            omitted flag + interactive = prompt.
            #            omitted flag + non-interactive = leave unchanged.
            if whisper_language is not None:
                config.whisper_language = whisper_language
            elif interactive:
                config.whisper_language = click.prompt(
                    "Language (ISO code, empty = auto-detect)",
                    default=config.whisper_language or "",
                )
            click.echo(f"Voice: {stt_backend} configured.")

    # TTS (text-to-speech) - reuses the OpenAI API key from STT config
    tts_changed = False
    if tts_voice is not None and tts_backend is None:
        if not config.tts_backend:
            raise click.UsageError("--tts-voice requires --tts-backend or a previously configured backend.")
        tts_backend = config.tts_backend

    if tts_backend is not None or (interactive and click.confirm(
        "Configure voice responses (TTS)?",
        default=not config.tts_backend,
    )):
        if tts_backend is None:
            tts_backend = click.prompt(
                "TTS backend",
                type=click.Choice(["openai", "off"]),
                default=config.tts_backend or "openai",
            )
        if tts_backend == "off":
            config.tts_backend = ""
            tts_changed = True
            click.echo("Voice responses disabled.")
        else:
            config.tts_backend = tts_backend
            tts_changed = True
            if not config.openai_api_key:
                key = click.prompt("OpenAI API key (shared with STT)", default="")
                if key:
                    config.openai_api_key = key
            if tts_voice:
                config.tts_voice = tts_voice
            elif interactive:
                from .transcriber import TTS_VOICES
                config.tts_voice = click.prompt(
                    f"TTS voice ({', '.join(TTS_VOICES)})",
                    default=config.tts_voice or "alloy",
                )
            click.echo(f"TTS: {tts_backend} configured (voice: {config.tts_voice}).")

    if voice_changed or tts_changed:
        save_config(config, cfg_path)

    # Telethon authentication
    api_id = config.telegram_api_id
    api_hash = config.telegram_api_hash
    if not api_id or not api_hash:
        if phone or (interactive and click.confirm("Authenticate Telethon?")):
            raise SystemExit("Telegram API ID and Hash must be configured first.")
        return

    session_path = cfg_path.parent / "telethon.session"
    if phone or (interactive and click.confirm(
        "Authenticate Telethon?" if not session_path.exists() else "Re-authenticate Telethon?",
        default=not session_path.exists(),
    )):
        try:
            from .botfather import BotFatherClient  # noqa: F811
        except ImportError:
            raise SystemExit("telethon not installed. Run: pip install link-project-to-chat[create]")

        if not phone:
            phone = click.prompt("Phone number (with country code, e.g. +995511166693)")

        if not session_path.exists():
            session_path.touch(mode=0o600)
        else:
            session_path.chmod(0o600)

        from telethon.sync import TelegramClient
        client = TelegramClient(
            str(session_path), api_id, api_hash,
            device_model="Desktop", system_version="macOS", app_version="1.0",
        )
        try:
            client.start(phone=phone)
            session_path.chmod(0o600)
            click.echo("Telethon authenticated successfully!")
        except Exception as e:
            raise SystemExit(f"Authentication failed: {e}")
        finally:
            client.disconnect()

    # Show status
    config = load_config(cfg_path)
    click.echo("\nSetup status:")
    click.echo(f"  GitHub PAT: {'configured' if config.github_pat else 'not set'}")
    click.echo(f"  Telegram API ID: {'configured' if config.telegram_api_id else 'not set'}")
    click.echo(f"  Telegram API Hash: {'configured' if config.telegram_api_hash else 'not set'}")
    session_path = cfg_path.parent / "telethon.session"
    click.echo(f"  Telethon session: {'authenticated' if session_path.exists() else 'not authenticated'}")
    click.echo(f"  Voice STT: {config.stt_backend or 'disabled'}")
    click.echo(f"  Voice TTS: {config.tts_backend or 'disabled'}{f' ({config.tts_voice})' if config.tts_backend else ''}")


@main.command("start-manager")
@click.pass_context
def start_manager(ctx):
    """Start the manager bot."""
    from .manager.bot import ManagerBot
    from .manager.process import ProcessManager

    cfg_path = ctx.obj["config_path"]
    main_config = load_config(cfg_path)

    if main_config.migration_pending:
        click.echo("Migrating config.json from legacy auth fields to allowed_users...", err=True)
        save_config(main_config, cfg_path)
        click.echo("Migration complete.", err=True)

    token = main_config.manager_telegram_bot_token
    if not token:
        raise SystemExit("No manager token configured. Run 'configure --manager-token TOKEN' first.")
    # Post-migration the only auth source is allowed_users (global allow-list).
    # Empty -> fail-closed (every message rejected). The manager bot has no
    # project-scoped fallback (unlike project bots via resolve_project_allowed_users).
    if not main_config.allowed_users:
        raise SystemExit(
            "No users authorized for the manager bot. "
            "Run `configure --add-user USER[:ROLE]` or edit `allowed_users` in config.json."
        )

    pm = ProcessManager(project_config_path=cfg_path)
    # Adopt or clean up project-bot subprocesses surviving a prior manager
    # crash BEFORE start_autostart, so a reaped orphan isn't duplicated
    # into a second running bot polling the same Telegram token.
    adopted = pm.reap_orphans()
    if adopted:
        click.echo(f"Adopted {len(adopted)} orphan project bot(s): {', '.join(adopted)}")
    restored = pm.start_autostart()
    if restored:
        click.echo(f"Autostarted {restored} project(s).")

    bot = ManagerBot(
        token, pm,
        allowed_users=main_config.allowed_users,
        project_config_path=cfg_path,
    )
    click.echo("Manager bot started.")
    bot.build().run_polling()


@main.command("plugin-call")
@click.argument("project")
@click.argument("plugin_name")
@click.argument("tool_name")
@click.argument("args_json")
@click.pass_context
def plugin_call(ctx, project: str, plugin_name: str, tool_name: str, args_json: str):
    """Call a plugin tool from the command line (used by Claude via Bash)."""
    import asyncio
    import json as _json
    from pathlib import Path

    from .plugin import PluginContext, load_plugin

    try:
        args = _json.loads(args_json)
    except _json.JSONDecodeError as e:
        raise SystemExit(f"Invalid args_json: {e}")

    cfg_path = ctx.obj["config_path"]
    config = load_config(cfg_path)
    if project not in config.projects:
        raise SystemExit(f"Project {project!r} not found in config.")
    proj_path = Path(config.projects[project].path)
    data_dir = Path.home() / ".link-project-to-chat" / "meta" / project

    plugin_ctx = PluginContext(
        bot_name=project,
        project_path=proj_path,
        data_dir=data_dir,
    )
    plugin = load_plugin(plugin_name, plugin_ctx, {})
    if not plugin:
        raise SystemExit(f"Plugin {plugin_name!r} not found.")

    result = asyncio.run(plugin.call_tool(tool_name, args))
    click.echo(result)


@main.command("migrate-config")
@click.option("--dry-run", is_flag=True, help="Print the migration without modifying config.json.")
@click.option("--project", "project_filter", default=None, help="Limit project output to this name.")
@click.pass_context
def migrate_config(ctx, dry_run: bool, project_filter: str | None):
    """Apply the legacy -> AllowedUser migration on config.json.

    Exit code 0 on success; non-zero when any project ends up with empty
    `allowed_users` (operators must populate them before exposing the bot).
    """
    cfg_path = ctx.obj["config_path"]
    config = load_config(cfg_path)

    # Print the resulting state for the user to inspect.
    click.echo(f"Global allow-list: {len(config.allowed_users)} users")
    for u in config.allowed_users:
        locked = f" [identities: {', '.join(u.locked_identities)}]" if u.locked_identities else ""
        click.echo(f"  - {u.username} ({u.role}){locked}")

    empty_projects: list[str] = []
    for name, proj in config.projects.items():
        if project_filter and name != project_filter:
            continue
        click.echo(f"\nProject {name!r}: {len(proj.allowed_users)} users")
        for u in proj.allowed_users:
            locked = f" [identities: {', '.join(u.locked_identities)}]" if u.locked_identities else ""
            click.echo(f"  - {u.username} ({u.role}){locked}")
        if not proj.allowed_users and not config.allowed_users:
            empty_projects.append(name)

    if not config.migration_pending:
        click.echo("\nNo migration needed (config.json already in the new shape).")
        # Still exit non-zero if there are empty allowlists - operator should know.
        if empty_projects:
            click.echo(
                f"\nERROR: projects with no users authorized: {', '.join(empty_projects)}",
                err=True,
            )
            raise SystemExit(2)
        return

    if dry_run:
        click.echo("\n(dry-run) Migration NOT applied. Re-run without --dry-run to write.")
        if empty_projects:
            click.echo(
                f"\nWARNING: after migration, projects with empty allow-lists: "
                f"{', '.join(empty_projects)}", err=True,
            )
            raise SystemExit(2)
        return

    save_config(config, cfg_path)
    click.echo("\nMigration applied. Legacy keys stripped; allowed_users written.")
    if empty_projects:
        click.echo(
            f"\nERROR: projects with no users authorized: {', '.join(empty_projects)}.\n"
            "Run `configure --add-user <username>` or edit the manager bot to fix.",
            err=True,
        )
        raise SystemExit(2)


if __name__ == "__main__":
    main()
