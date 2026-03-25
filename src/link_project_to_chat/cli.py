from __future__ import annotations

import logging
from pathlib import Path

import click

from .config import DEFAULT_CONFIG, Config, ProjectConfig, clear_trusted_user_id, load_config, save_config


@click.group()
@click.option("--config", "config_path", type=click.Path(), default=None,
              help="Config file path (default: ~/.link-project-to-chat/config.json)")
@click.pass_context
def main(ctx, config_path: str | None):
    """link-project-to-chat: Chat with Claude about a project via Telegram."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = Path(config_path) if config_path else DEFAULT_CONFIG


@main.command()
@click.argument("path", type=click.Path(exists=True, file_okay=False, resolve_path=True))
@click.option("--name", default=None, help="Project name (defaults to directory name)")
@click.option("--token", prompt="Telegram bot token", help="Bot token from BotFather")
@click.pass_context
def link(ctx, path: str, name: str | None, token: str):
    """Link a project directory to a Telegram bot."""
    cfg_path = ctx.obj["config_path"]
    config = load_config(cfg_path)
    project_path = Path(path).resolve()
    project_name = name or project_path.name

    config.projects[project_name] = ProjectConfig(path=str(project_path), telegram_bot_token=token)
    save_config(config, cfg_path)
    click.echo(f"Linked '{project_name}' -> {project_path}")


@main.command()
@click.argument("name")
@click.pass_context
def unlink(ctx, name: str):
    """Remove the linked project."""
    cfg_path = ctx.obj["config_path"]
    config = load_config(cfg_path)
    if name not in config.projects:
        raise SystemExit(f"Project '{name}' not found.")
    del config.projects[name]
    save_config(config, cfg_path)
    click.echo(f"Unlinked '{name}'")


@main.command("list")
@click.pass_context
def list_projects(ctx):
    """Show the linked project."""
    config = load_config(ctx.obj["config_path"])
    if not config.projects:
        return click.echo("No projects linked.")
    for name, proj in config.projects.items():
        click.echo(f"  {name}: {proj.path}")


@main.command()
@click.option("--project", default=None, help="Project name (if multiple are configured)")
@click.option("--path", "project_path", type=click.Path(exists=True, file_okay=False, resolve_path=True),
              default=None, help="Project directory (use instead of config)")
@click.option("--token", default=None, help="Telegram bot token (use instead of config)")
@click.option("--username", default=None, help="Allowed Telegram username (overrides config)")
@click.option("--session-id", default=None, help="Resume a Claude session by ID")
@click.pass_context
def start(ctx, project: str | None, project_path: str | None, token: str | None,
          username: str | None, session_id: str | None):
    """Start the Telegram bot.

    Use --path and --token to run without a config file, or use config.
    """
    from .bot import run_bot, run_bots

    # Direct params mode: no config needed
    if project_path and token:
        p = Path(project_path).resolve()
        run_bot(name=p.name, path=p, token=token,
                username=(username or "").lower().lstrip("@"),
                session_id=session_id)
        return

    # Config mode
    config = load_config(ctx.obj["config_path"])
    if username:
        config.allowed_username = username.lower().lstrip("@")

    if not config.projects:
        raise SystemExit("No projects. Use --path/--token params or 'link' command first.")

    if project:
        if project not in config.projects:
            raise SystemExit(f"Project '{project}' not found.")
        proj = config.projects[project]
        run_bot(project, Path(proj.path), proj.telegram_bot_token,
                config.allowed_username, session_id=session_id)
    else:
        run_bots(config)


@main.command()
@click.option("--username", required=True, prompt="Telegram username", help="Allowed Telegram username")
@click.pass_context
def configure(ctx, username: str):
    """Set the allowed Telegram username."""
    cfg_path = ctx.obj["config_path"]
    config = load_config(cfg_path)
    new_username = username.lower().lstrip("@")
    if new_username != config.allowed_username:
        clear_trusted_user_id()
        click.echo("Trusted user ID cleared (username changed).")
    config.allowed_username = new_username
    save_config(config, cfg_path)
    click.echo(f"Configured username: @{new_username}")
