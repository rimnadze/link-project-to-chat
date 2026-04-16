from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG = Path.home() / ".link-project-to-chat" / "config.json"


@dataclass
class ProjectConfig:
    path: str
    telegram_bot_token: str
    allowed_username: str = ""  # per-project override; falls back to Config.allowed_username
    trusted_user_id: int | None = None  # per-project; falls back to Config.trusted_user_id
    model: str | None = None
    effort: str | None = None
    permissions: str | None = None  # one of PERMISSION_MODES or "dangerously-skip-permissions"
    session_id: str | None = None
    autostart: bool = False
    plugins: list[dict] = field(default_factory=list)  # [{"name": "bore-tunnel", ...}, ...]


@dataclass
class Config:
    allowed_username: str = ""
    trusted_user_id: int | None = None  # global fallback (also used by manager bot)
    manager_telegram_bot_token: str = ""
    projects: dict[str, ProjectConfig] = field(default_factory=dict)


def _load_permissions(proj: dict) -> str | None:
    """Read permissions from a project dict, with backward compat for old keys."""
    if "permissions" in proj:
        return proj["permissions"] or None
    if proj.get("dangerously_skip_permissions"):
        return "dangerously-skip-permissions"
    return proj.get("permission_mode") or None


def resolve_permissions(permissions: str | None) -> tuple[bool, str | None]:
    """Convert permissions string to (skip_permissions, permission_mode) for CLI/bot use."""
    if permissions == "dangerously-skip-permissions":
        return True, None
    if permissions and permissions != "default":
        return False, permissions
    return False, None


def load_config(path: Path = DEFAULT_CONFIG) -> Config:
    config = Config()
    if path.exists():
        raw = json.loads(path.read_text())
        config.allowed_username = raw.get("allowed_username", "").lower().lstrip("@")
        config.trusted_user_id = raw.get("trusted_user_id")
        # Support old name for backward compatibility
        config.manager_telegram_bot_token = raw.get(
            "manager_telegram_bot_token", raw.get("manager_bot_token", "")
        )
        for name, proj in raw.get("projects", {}).items():
            config.projects[name] = ProjectConfig(
                path=proj["path"],
                telegram_bot_token=proj.get("telegram_bot_token", ""),
                allowed_username=proj.get("username", "").lower().lstrip("@"),
                trusted_user_id=proj.get("trusted_user_id"),
                model=proj.get("model"),
                effort=proj.get("effort"),
                permissions=_load_permissions(proj),
                session_id=proj.get("session_id"),
                autostart=proj.get("autostart", False),
                plugins=proj.get("plugins", []),
            )
    return config


def save_config(config: Config, path: Path = DEFAULT_CONFIG) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    raw: dict = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    raw["allowed_username"] = config.allowed_username
    raw["manager_telegram_bot_token"] = config.manager_telegram_bot_token
    raw.pop("manager_bot_token", None)  # remove old name if present
    if config.trusted_user_id is not None:
        raw["trusted_user_id"] = config.trusted_user_id
    else:
        raw.pop("trusted_user_id", None)
    # Merge per-project data, preserving unknown keys already in the file
    existing_projects: dict = raw.get("projects", {})
    for name, p in config.projects.items():
        proj = existing_projects.get(name, {})
        proj["path"] = p.path
        proj["telegram_bot_token"] = p.telegram_bot_token
        if p.allowed_username:
            proj["username"] = p.allowed_username
        else:
            proj.pop("username", None)
        if p.trusted_user_id is not None:
            proj["trusted_user_id"] = p.trusted_user_id
        else:
            proj.pop("trusted_user_id", None)
        if p.model:
            proj["model"] = p.model
        if p.effort:
            proj["effort"] = p.effort
        if p.permissions:
            proj["permissions"] = p.permissions
        else:
            proj.pop("permissions", None)
        proj.pop("permission_mode", None)
        proj.pop("dangerously_skip_permissions", None)
        if p.session_id:
            proj["session_id"] = p.session_id
        else:
            proj.pop("session_id", None)
        proj["autostart"] = p.autostart
        if p.plugins:
            proj["plugins"] = p.plugins
        else:
            proj.pop("plugins", None)
        existing_projects[name] = proj
    # Remove projects that no longer exist in config
    raw["projects"] = {k: v for k, v in existing_projects.items() if k in config.projects}
    path.write_text(json.dumps(raw, indent=2) + "\n")
    path.chmod(0o600)


def _patch_json(patch_fn, path: Path) -> None:
    """Read-modify-write config JSON via a mutating function."""
    path.parent.mkdir(parents=True, exist_ok=True)
    raw: dict = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    patch_fn(raw)
    path.write_text(json.dumps(raw, indent=2) + "\n")
    path.chmod(0o600)


def load_sessions(path: Path = DEFAULT_CONFIG) -> dict[str, str]:
    """Load all session IDs from config.json per-project entries."""
    if path.exists():
        try:
            raw = json.loads(path.read_text())
            return {
                name: proj["session_id"]
                for name, proj in raw.get("projects", {}).items()
                if proj.get("session_id")
            }
        except (json.JSONDecodeError, OSError):
            pass
    return {}


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


def save_session(project_name: str, session_id: str, path: Path = DEFAULT_CONFIG) -> None:
    def _patch(raw: dict) -> None:
        raw.setdefault("projects", {}).setdefault(project_name, {})["session_id"] = session_id
    _patch_json(_patch, path)


def clear_session(project_name: str, path: Path = DEFAULT_CONFIG) -> None:
    def _patch(raw: dict) -> None:
        raw.setdefault("projects", {}).setdefault(project_name, {}).pop("session_id", None)
    _patch_json(_patch, path)


def load_trusted_user_id(path: Path = DEFAULT_CONFIG) -> int | None:
    """Load the global trusted_user_id from config.json."""
    if path.exists():
        try:
            return json.loads(path.read_text()).get("trusted_user_id")
        except (json.JSONDecodeError, OSError):
            pass
    return None


def save_trusted_user_id(user_id: int, path: Path = DEFAULT_CONFIG) -> None:
    """Save the global trusted_user_id into config.json."""
    _patch_json(lambda raw: raw.update({"trusted_user_id": user_id}), path)


def save_project_trusted_user_id(
    project_name: str, user_id: int, path: Path = DEFAULT_CONFIG
) -> None:
    """Save a per-project trusted_user_id into config.json."""
    def _patch(raw: dict) -> None:
        raw.setdefault("projects", {}).setdefault(project_name, {})["trusted_user_id"] = user_id
    _patch_json(_patch, path)


def clear_trusted_user_id(path: Path = DEFAULT_CONFIG) -> None:
    """Remove the global trusted_user_id from config.json."""
    def _patch(raw: dict) -> None:
        raw.pop("trusted_user_id", None)
    _patch_json(_patch, path)
