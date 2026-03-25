from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG = Path.home() / ".link-project-to-chat" / "config.json"


@dataclass
class ProjectConfig:
    path: str
    telegram_bot_token: str


@dataclass
class Config:
    allowed_username: str = ""
    projects: dict[str, ProjectConfig] = field(default_factory=dict)


def load_config(path: Path = DEFAULT_CONFIG) -> Config:
    config = Config()
    if path.exists():
        raw = json.loads(path.read_text())
        config.allowed_username = raw.get("allowed_username", "").lower().lstrip("@")
        for name, proj in raw.get("projects", {}).items():
            config.projects[name] = ProjectConfig(
                path=proj["path"],
                telegram_bot_token=proj["telegram_bot_token"],
            )
    return config


SESSIONS_FILE = Path.home() / ".link-project-to-chat" / "sessions.json"


def load_sessions(path: Path = SESSIONS_FILE) -> dict[str, str]:
    """Load project_name -> session_id mapping."""
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_session(project_name: str, session_id: str, path: Path = SESSIONS_FILE) -> None:
    """Save a session ID for a project."""
    sessions = load_sessions(path)
    sessions[project_name] = session_id
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sessions, indent=2) + "\n")


def clear_session(project_name: str, path: Path = SESSIONS_FILE) -> None:
    """Remove a saved session for a project."""
    sessions = load_sessions(path)
    if project_name in sessions:
        del sessions[project_name]
        path.write_text(json.dumps(sessions, indent=2) + "\n")


TRUSTED_USER_ID_FILE = Path.home() / ".link-project-to-chat" / "trusted_user_id.json"


def load_trusted_user_id(path: Path = TRUSTED_USER_ID_FILE) -> int | None:
    """Load the globally trusted Telegram user_id."""
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None
    return None


def save_trusted_user_id(user_id: int, path: Path = TRUSTED_USER_ID_FILE) -> None:
    """Persist the trusted Telegram user_id."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(user_id) + "\n")


def clear_trusted_user_id(path: Path = TRUSTED_USER_ID_FILE) -> None:
    """Remove the saved trusted user_id."""
    if path.exists():
        path.unlink()


def save_config(config: Config, path: Path = DEFAULT_CONFIG) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    raw = {
        "allowed_username": config.allowed_username,
        "projects": {
            name: {"path": p.path, "telegram_bot_token": p.telegram_bot_token}
            for name, p in config.projects.items()
        },
    }
    path.write_text(json.dumps(raw, indent=2) + "\n")
    path.chmod(0o600)
