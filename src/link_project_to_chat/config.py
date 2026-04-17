from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG = Path.home() / ".link-project-to-chat" / "config.json"


@dataclass
class AllowedUser:
    username: str
    user_id: int | None = None
    role: str = "viewer"  # "viewer" | "executor"


@dataclass
class ProjectConfig:
    path: str
    telegram_bot_token: str
    allowed_users: list[AllowedUser] = field(default_factory=list)
    model: str | None = None
    effort: str | None = None
    permissions: str | None = None  # one of PERMISSION_MODES or "dangerously-skip-permissions"
    session_id: str | None = None
    autostart: bool = False
    plugins: list[dict] = field(default_factory=list)


@dataclass
class Config:
    allowed_users: list[AllowedUser] = field(default_factory=list)
    manager_telegram_bot_token: str = ""
    projects: dict[str, ProjectConfig] = field(default_factory=dict)


def _parse_allowed_users(data: list[dict]) -> list[AllowedUser]:
    return [
        AllowedUser(
            username=u["username"].lower().lstrip("@"),
            user_id=u.get("user_id"),
            role=u.get("role", "viewer"),
        )
        for u in data
    ]


def _serialize_allowed_users(users: list[AllowedUser]) -> list[dict]:
    result = []
    for u in users:
        entry: dict = {"username": u.username}
        if u.user_id is not None:
            entry["user_id"] = u.user_id
        entry["role"] = u.role
        result.append(entry)
    return result


def _load_permissions(proj: dict) -> str | None:
    if "permissions" in proj:
        return proj["permissions"] or None
    if proj.get("dangerously_skip_permissions"):
        return "dangerously-skip-permissions"
    return proj.get("permission_mode") or None


def resolve_permissions(permissions: str | None) -> tuple[bool, str | None]:
    if permissions == "dangerously-skip-permissions":
        return True, None
    if permissions and permissions != "default":
        return False, permissions
    return False, None


def load_config(path: Path = DEFAULT_CONFIG) -> Config:
    config = Config()
    if not path.exists():
        return config
    raw = json.loads(path.read_text())
    config.allowed_users = _parse_allowed_users(raw.get("allowed_users", []))
    config.manager_telegram_bot_token = raw.get(
        "manager_telegram_bot_token", raw.get("manager_bot_token", "")
    )
    for name, proj in raw.get("projects", {}).items():
        config.projects[name] = ProjectConfig(
            path=proj["path"],
            telegram_bot_token=proj.get("telegram_bot_token", ""),
            allowed_users=_parse_allowed_users(proj.get("allowed_users", [])),
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
    raw["allowed_users"] = _serialize_allowed_users(config.allowed_users)
    raw["manager_telegram_bot_token"] = config.manager_telegram_bot_token
    # Remove old keys
    for old in ("allowed_username", "trusted_user_id", "manager_bot_token"):
        raw.pop(old, None)
    existing_projects: dict = raw.get("projects", {})
    for name, p in config.projects.items():
        proj = existing_projects.get(name, {})
        proj["path"] = p.path
        proj["telegram_bot_token"] = p.telegram_bot_token
        proj["allowed_users"] = _serialize_allowed_users(p.allowed_users)
        for old in ("username", "trusted_user_id", "permission_mode", "dangerously_skip_permissions"):
            proj.pop(old, None)
        if p.model:
            proj["model"] = p.model
        if p.effort:
            proj["effort"] = p.effort
        if p.permissions:
            proj["permissions"] = p.permissions
        else:
            proj.pop("permissions", None)
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
    raw["projects"] = {k: v for k, v in existing_projects.items() if k in config.projects}
    path.write_text(json.dumps(raw, indent=2) + "\n")
    path.chmod(0o600)


def _patch_json(patch_fn, path: Path) -> None:
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
