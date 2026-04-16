from __future__ import annotations

import logging
from pathlib import Path

import click

from .config import (
    DEFAULT_CONFIG,
    clear_trusted_user_id,
    load_config,
    load_trusted_user_id,
    resolve_permissions,
    save_config,
    save_project_trusted_user_id,
)


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
        click.echo(f"  {name}: {proj.path}")


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
@click.pass_context
def projects_add(ctx, name: str, project_path: str, token: str, username: str | None, model: str | None, permission_mode: str | None, skip_permissions: bool):
    """Add a project."""
    from .manager.config import load_project_configs, save_project_configs

    cfg_path = ctx.obj["config_path"]
    projects = load_project_configs(cfg_path)
    if name in projects:
        raise SystemExit(f"Project '{name}' already exists.")
    entry: dict = {"path": str(Path(project_path).resolve()), "telegram_bot_token": token}
    if username:
        entry["username"] = username.lower().lstrip("@")
    if model:
        entry["model"] = model
    if skip_permissions:
        entry["permissions"] = "dangerously-skip-permissions"
    elif permission_mode:
        entry["permissions"] = permission_mode
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
    """Edit a project field (name, path, token, username, model, permission_mode, dangerously_skip_permissions)."""
    from .manager.config import load_project_configs, save_project_configs

    _EDITABLE = ("name", "path", "token", "username", "model", "permission_mode", "dangerously_skip_permissions")
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
    elif field in ("username", "model", "permission_mode", "dangerously_skip_permissions"):
        projects[name][field] = value
        save_project_configs(projects, cfg_path)
        click.echo(f"Updated '{name}' {field} to {value}.")
    else:
        raise SystemExit(f"Unknown field. Use: {', '.join(_EDITABLE)}")


@main.command()
@click.option("--username", default=None, help="Allowed Telegram username")
@click.option("--manager-token", default=None, help="Telegram bot token for the manager bot")
@click.pass_context
def configure(ctx, username: str | None, manager_token: str | None):
    """Configure username and/or manager bot token."""
    if not username and not manager_token:
        raise SystemExit("Provide at least one of --username or --manager-token.")
    cfg_path = ctx.obj["config_path"]
    config = load_config(cfg_path)
    if username:
        new_username = username.lower().lstrip("@")
        if new_username != config.allowed_username:
            clear_trusted_user_id(cfg_path)
            click.echo("Trusted user ID cleared (username changed).")
        config.allowed_username = new_username
        click.echo(f"Configured username: @{new_username}")
    if manager_token:
        config.manager_telegram_bot_token = manager_token
        click.echo(f"Configured manager token: ***{manager_token[-4:]}")
    save_config(config, cfg_path)


@main.command()
@click.option(
    "--project", default=None, help="Project name (if multiple are configured)"
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
@click.pass_context
def start(
    ctx,
    project: str | None,
    project_path: str | None,
    token: str | None,
    username: str | None,
    session_id: str | None,
    model: str | None,
    skip_permissions: bool,
    permission_mode: str | None,
    allowed_tools: str | None,
    disallowed_tools: str | None,
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
        run_bot(
            name=p.name,
            path=p,
            token=token,
            username=(username or "").lower().lstrip("@"),
            session_id=session_id,
            model=model,
            skip_permissions=skip_permissions,
            permission_mode=permission_mode,
            allowed_tools=allowed,
            disallowed_tools=disallowed,
            trusted_user_id=load_trusted_user_id(cfg_path),
        )
        return

    config = load_config(cfg_path)
    if username:
        config.allowed_username = username.lower().lstrip("@")

    if not config.projects:
        raise SystemExit(
            "No projects. Use --path/--token params or 'projects add' command first."
        )

    if project:
        if project not in config.projects:
            raise SystemExit(f"Project '{project}' not found.")
        proj = config.projects[project]
        effective_username = proj.allowed_username or config.allowed_username
        if proj.allowed_username:
            effective_trusted_id = proj.trusted_user_id
        else:
            effective_trusted_id = proj.trusted_user_id if proj.trusted_user_id is not None else config.trusted_user_id
        proj_skip, proj_pm = resolve_permissions(proj.permissions)
        run_bot(
            project,
            Path(proj.path),
            proj.telegram_bot_token,
            effective_username,
            session_id=session_id,
            model=model or proj.model,
            effort=proj.effort,
            skip_permissions=skip_permissions or proj_skip,
            permission_mode=permission_mode or proj_pm,
            allowed_tools=allowed,
            disallowed_tools=disallowed,
            trusted_user_id=effective_trusted_id,
            on_trust=lambda uid: save_project_trusted_user_id(project, uid, cfg_path),
            plugins=proj.plugins or None,
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
        )


@main.command("start-manager")
@click.pass_context
def start_manager(ctx):
    """Start the manager bot."""
    from .manager.bot import ManagerBot
    from .manager.process import ProcessManager

    cfg_path = ctx.obj["config_path"]
    main_config = load_config(cfg_path)

    token = main_config.manager_telegram_bot_token
    if not token:
        raise SystemExit("No manager token configured. Run 'configure --manager-token TOKEN' first.")
    if not main_config.allowed_username:
        raise SystemExit("No username configured. Run 'configure --username USER' first.")

    pm = ProcessManager(project_config_path=cfg_path)
    restored = pm.start_autostart()
    if restored:
        click.echo(f"Autostarted {restored} project(s).")

    bot = ManagerBot(token, pm, allowed_username=main_config.allowed_username, trusted_user_id=main_config.trusted_user_id, project_config_path=cfg_path)
    click.echo("Manager bot started.")
    bot.build().run_polling()
