from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest
from click.testing import CliRunner

from link_project_to_chat.cli import main


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def cfg(tmp_path: Path):
    """A temp config.json with one project pre-seeded."""
    p = tmp_path / "config.json"
    p.write_text(json.dumps({
        "allowed_username": "alice",
        "projects": {"existing": {"path": str(tmp_path), "telegram_bot_token": "TOK"}},
    }))
    return p, tmp_path


def test_setup_authenticates_telethon_with_secure_session_before_start(tmp_path, monkeypatch):
    from link_project_to_chat.config import Config, save_config

    cfg_path = tmp_path / "config.json"
    save_config(Config(telegram_api_id=12345, telegram_api_hash="hash"), cfg_path)
    events: list[tuple[str, bool, int | None] | tuple[str]] = []

    class FakeTelegramClient:
        def __init__(
            self,
            session_path: str,
            api_id: int,
            api_hash: str,
            *,
            device_model: str,
            system_version: str,
            app_version: str,
        ) -> None:
            self.session_path = Path(session_path)
            mode = (
                self.session_path.stat().st_mode & 0o777
                if self.session_path.exists()
                else None
            )
            events.append(("init", self.session_path.exists(), mode))
            self.api_id = api_id
            self.api_hash = api_hash
            self.device_model = device_model
            self.system_version = system_version
            self.app_version = app_version

        def start(self, phone: str) -> None:
            mode = (
                self.session_path.stat().st_mode & 0o777
                if self.session_path.exists()
                else None
            )
            events.append(("start", self.session_path.exists(), mode))

        def disconnect(self) -> None:
            events.append(("disconnect",))

    fake_telethon = types.ModuleType("telethon")
    fake_sync = types.ModuleType("telethon.sync")
    fake_sync.TelegramClient = FakeTelegramClient
    fake_telethon.sync = fake_sync
    monkeypatch.setitem(sys.modules, "telethon", fake_telethon)
    monkeypatch.setitem(sys.modules, "telethon.sync", fake_sync)

    result = CliRunner().invoke(
        main,
        ["--config", str(cfg_path), "setup", "--phone", "+995511166693"],
    )

    assert result.exit_code == 0, result.output
    assert events[0][0] == "init"
    assert events[0][1] is True
    if sys.platform != "win32":
        assert events[0][2] == 0o600
    assert events[1][0] == "start"
    assert events[1][1] is True
    if sys.platform != "win32":
        assert events[1][2] == 0o600
    assert events[-1] == ("disconnect",)
    assert "Telethon authenticated successfully!" in result.output


@pytest.mark.parametrize("args", [["--help"], ["configure", "--help"], ["migrate-config", "--help"]])
def test_cli_help_is_ascii_safe_for_windows_console(runner, args):
    result = runner.invoke(main, args)

    assert result.exit_code == 0, result.output
    result.output.encode("ascii")


# --- projects add ---

def test_add_project_success(runner, cfg):
    p, tmp_path = cfg
    proj_dir = tmp_path / "newproj"
    proj_dir.mkdir()
    result = runner.invoke(main, [
        "--config", str(p),
        "projects", "add",
        "--name", "newproj",
        "--path", str(proj_dir),
        "--token", "NEW_TOKEN",
    ])
    assert result.exit_code == 0
    assert "Added" in result.output
    projects = json.loads(p.read_text())["projects"]
    assert "newproj" in projects
    assert projects["newproj"]["telegram_bot_token"] == "NEW_TOKEN"


def test_add_project_name_required(runner, cfg):
    p, tmp_path = cfg
    proj_dir = tmp_path / "mydir"
    proj_dir.mkdir()
    result = runner.invoke(main, [
        "--config", str(p),
        "projects", "add",
        "--path", str(proj_dir),
        "--token", "T",
    ])
    assert result.exit_code != 0


def test_add_project_token_required(runner, cfg):
    p, tmp_path = cfg
    proj_dir = tmp_path / "notokenproj"
    proj_dir.mkdir()
    result = runner.invoke(main, [
        "--config", str(p),
        "projects", "add",
        "--name", "notokenproj",
        "--path", str(proj_dir),
    ])
    assert result.exit_code != 0


def test_add_project_duplicate_fails(runner, cfg):
    p, tmp_path = cfg
    result = runner.invoke(main, [
        "--config", str(p),
        "projects", "add",
        "--name", "existing",
        "--path", str(tmp_path),
        "--token", "T",
    ])
    assert result.exit_code != 0
    assert "already exists" in result.output


def test_add_project_optional_fields(runner, cfg):
    p, tmp_path = cfg
    proj_dir = tmp_path / "optproj"
    proj_dir.mkdir()
    result = runner.invoke(main, [
        "--config", str(p),
        "projects", "add",
        "--name", "optproj",
        "--path", str(proj_dir),
        "--token", "T",
        "--username", "bob",
        "--model", "sonnet",
        "--permission-mode", "default",
    ])
    assert result.exit_code == 0
    proj = json.loads(p.read_text())["projects"]["optproj"]
    # P1 #2: --username now writes the modern allowed_users shape rather than
    # the legacy flat ``username`` key (which would lose to a pre-existing
    # allowed_users list on next load).
    assert "username" not in proj
    assert proj["allowed_users"] == [{"username": "bob", "role": "executor"}]
    # v1.0.0 dropped the legacy top-level mirror; canonical home is backend_state.
    assert proj["backend_state"]["claude"]["model"] == "sonnet"
    assert proj["backend_state"]["claude"]["permissions"] == "default"
    assert "model" not in proj
    assert "permissions" not in proj


# --- projects remove ---

def test_remove_project_success(runner, cfg):
    p, _ = cfg
    result = runner.invoke(main, ["--config", str(p), "projects", "remove", "existing"])
    assert result.exit_code == 0
    assert "Removed" in result.output
    assert "existing" not in json.loads(p.read_text())["projects"]


def test_remove_project_not_found(runner, cfg):
    p, _ = cfg
    result = runner.invoke(main, ["--config", str(p), "projects", "remove", "nope"])
    assert result.exit_code != 0
    assert "not found" in result.output


# --- projects edit ---

def test_edit_project_rename(runner, cfg):
    p, _ = cfg
    result = runner.invoke(main, ["--config", str(p), "projects", "edit", "existing", "name", "renamed"])
    assert result.exit_code == 0
    assert "Renamed" in result.output
    projects = json.loads(p.read_text())["projects"]
    assert "renamed" in projects and "existing" not in projects


def test_edit_project_token(runner, cfg):
    p, _ = cfg
    result = runner.invoke(main, ["--config", str(p), "projects", "edit", "existing", "token", "NEWTOKEN"])
    assert result.exit_code == 0
    assert json.loads(p.read_text())["projects"]["existing"]["telegram_bot_token"] == "NEWTOKEN"


def test_edit_project_path(runner, cfg, tmp_path):
    p, tmp_path = cfg
    new_dir = tmp_path / "newdir"
    new_dir.mkdir()
    result = runner.invoke(main, ["--config", str(p), "projects", "edit", "existing", "path", str(new_dir)])
    assert result.exit_code == 0
    assert json.loads(p.read_text())["projects"]["existing"]["path"] == str(new_dir)


def test_edit_project_invalid_path(runner, cfg):
    p, _ = cfg
    result = runner.invoke(main, ["--config", str(p), "projects", "edit", "existing", "path", "/nonexistent/xyz"])
    assert result.exit_code != 0
    assert "not exist" in result.output


def test_edit_project_rename_conflict(runner, cfg, tmp_path):
    p, tmp_path = cfg
    data = json.loads(p.read_text())
    data["projects"]["other"] = {"path": str(tmp_path), "telegram_bot_token": "T"}
    p.write_text(json.dumps(data))
    result = runner.invoke(main, ["--config", str(p), "projects", "edit", "existing", "name", "other"])
    assert result.exit_code != 0
    assert "already exists" in result.output


def test_edit_project_unknown_field(runner, cfg):
    p, _ = cfg
    result = runner.invoke(main, ["--config", str(p), "projects", "edit", "existing", "color", "blue"])
    assert result.exit_code != 0
    assert "Unknown field" in result.output


def test_edit_project_not_found(runner, cfg):
    p, _ = cfg
    result = runner.invoke(main, ["--config", str(p), "projects", "edit", "nope", "token", "X"])
    assert result.exit_code != 0
    assert "not found" in result.output


def test_edit_project_permission_mode_updates_unified_permissions_field(runner, cfg):
    p, _ = cfg
    data = json.loads(p.read_text())
    data["projects"]["existing"]["permissions"] = "default"
    p.write_text(json.dumps(data))

    result = runner.invoke(
        main,
        ["--config", str(p), "projects", "edit", "existing", "permission_mode", "plan"],
    )

    assert result.exit_code == 0
    proj = json.loads(p.read_text())["projects"]["existing"]
    # v1.0.0 dropped the legacy top-level mirror.
    assert proj["backend_state"]["claude"]["permissions"] == "plan"
    assert "permissions" not in proj
    assert "permission_mode" not in proj
    assert "dangerously_skip_permissions" not in proj


def test_edit_project_permissions_field_supported(runner, cfg):
    p, _ = cfg
    result = runner.invoke(
        main,
        ["--config", str(p), "projects", "edit", "existing", "permissions", "dangerously-skip-permissions"],
    )

    assert result.exit_code == 0
    proj = json.loads(p.read_text())["projects"]["existing"]
    assert proj["backend_state"]["claude"]["permissions"] == "dangerously-skip-permissions"
    assert "permissions" not in proj


def test_edit_project_dangerously_skip_permissions_boolean_alias(runner, cfg):
    p, _ = cfg
    result = runner.invoke(
        main,
        ["--config", str(p), "projects", "edit", "existing", "dangerously_skip_permissions", "true"],
    )

    assert result.exit_code == 0
    proj = json.loads(p.read_text())["projects"]["existing"]
    assert proj["backend_state"]["claude"]["permissions"] == "dangerously-skip-permissions"
    assert "permissions" not in proj


def test_edit_project_dangerously_skip_permissions_false_resets_to_default(runner, cfg):
    p, _ = cfg
    data = json.loads(p.read_text())
    data["projects"]["existing"]["permissions"] = "dangerously-skip-permissions"
    p.write_text(json.dumps(data))

    result = runner.invoke(
        main,
        ["--config", str(p), "projects", "edit", "existing", "dangerously_skip_permissions", "false"],
    )

    assert result.exit_code == 0
    proj = json.loads(p.read_text())["projects"]["existing"]
    assert proj["backend_state"]["claude"]["permissions"] == "default"
    assert "permissions" not in proj


def test_edit_project_invalid_permissions_value_fails(runner, cfg):
    p, _ = cfg
    result = runner.invoke(
        main,
        ["--config", str(p), "projects", "edit", "existing", "permissions", "wildwest"],
    )

    assert result.exit_code != 0
    assert "Invalid permissions value" in result.output


# --- P1 #2: legacy `username` writes must translate to allowed_users ---

def test_projects_add_with_username_writes_allowed_users(tmp_path):
    """Regression for P1 #2: `projects add --username X` must produce an
    actually-authorizing config rather than a legacy `username` flat key
    that loses to any pre-existing allowed_users on next load.
    """
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"projects": {}}))
    proj_dir = tmp_path / "myproj"
    proj_dir.mkdir()
    result = CliRunner().invoke(main, [
        "--config", str(cfg_file),
        "projects", "add",
        "--name", "myproj",
        "--path", str(proj_dir),
        "--token", "t",
        "--username", "alice",
    ])
    assert result.exit_code == 0, result.output

    on_disk = json.loads(cfg_file.read_text())
    proj = on_disk["projects"]["myproj"]
    # Legacy flat key must not be present.
    assert "username" not in proj
    # Modern shape with role=executor.
    assert proj["allowed_users"] == [{"username": "alice", "role": "executor"}]


def test_projects_add_with_username_uppercase_and_at_normalized(tmp_path):
    """Symmetry with the legacy normalization: lowercase + strip @."""
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"projects": {}}))
    proj_dir = tmp_path / "p"
    proj_dir.mkdir()
    result = CliRunner().invoke(main, [
        "--config", str(cfg_file),
        "projects", "add",
        "--name", "p", "--path", str(proj_dir), "--token", "t",
        "--username", "@AliceUpper",
    ])
    assert result.exit_code == 0, result.output
    on_disk = json.loads(cfg_file.read_text())
    assert on_disk["projects"]["p"]["allowed_users"] == [
        {"username": "aliceupper", "role": "executor"}
    ]


def test_projects_edit_username_replaces_allowed_users(tmp_path):
    """Regression for P1 #2: `projects edit NAME username X` must actually
    authorize X — historically it wrote a legacy flat key that lost to a
    pre-existing allowed_users on next load.
    """
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({
        "projects": {
            "myproj": {
                "path": str(tmp_path),
                "telegram_bot_token": "t",
                "allowed_users": [{"username": "alice", "role": "executor"}],
            }
        }
    }))
    result = CliRunner().invoke(main, [
        "--config", str(cfg_file),
        "projects", "edit", "myproj", "username", "bob",
    ])
    assert result.exit_code == 0, result.output

    on_disk = json.loads(cfg_file.read_text())
    proj = on_disk["projects"]["myproj"]
    # Bob fully replaces alice in the modern shape (matches the legacy
    # ``username`` semantics of "the allowed user").
    assert proj["allowed_users"] == [{"username": "bob", "role": "executor"}]
    # Legacy flat key gone.
    assert "username" not in proj
    # Deprecation hint surfaced to stderr.
    assert "allowed_users" in result.stderr or "/add_user" in result.stderr


def test_projects_edit_username_normalizes_handle(tmp_path):
    """Edit semantics match add: lowercase + strip leading @."""
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({
        "projects": {
            "myproj": {
                "path": str(tmp_path),
                "telegram_bot_token": "t",
            }
        }
    }))
    result = CliRunner().invoke(main, [
        "--config", str(cfg_file),
        "projects", "edit", "myproj", "username", "@BobUpper",
    ])
    assert result.exit_code == 0, result.output
    on_disk = json.loads(cfg_file.read_text())
    assert on_disk["projects"]["myproj"]["allowed_users"] == [
        {"username": "bobupper", "role": "executor"}
    ]


# --- configure --manager-token ---

def test_configure_manager_token(runner, cfg):
    p, _ = cfg
    result = runner.invoke(main, ["--config", str(p), "configure", "--manager-token", "MGR_TOKEN"])
    assert result.exit_code == 0
    assert "OKEN" in result.output
    data = json.loads(p.read_text())
    assert data["manager_telegram_bot_token"] == "MGR_TOKEN"
    # existing auth carried forward via the new identity-keyed shape (Task 3
    # strips legacy ``allowed_usernames`` from disk).
    assert [u["username"] for u in data["allowed_users"]] == ["alice"]
    assert "projects" in data


def test_configure_remove_username_revokes_trusted_binding(runner, tmp_path):
    p = tmp_path / "config.json"
    p.write_text(
        json.dumps(
            {
                "allowed_usernames": ["alice"],
                "trusted_users": {"alice": 42},
                "projects": {},
            }
        )
    )

    result = runner.invoke(main, ["--config", str(p), "configure", "--remove-username", "alice"])

    assert result.exit_code == 0
    data = json.loads(p.read_text())
    # Task 3: legacy keys are stripped from disk; the new shape carries the
    # data forward. After removing alice, neither legacy nor new shape
    # contains an entry for her.
    assert "allowed_usernames" not in data
    assert "trusted_users" not in data
    assert data.get("allowed_users", []) == []


def test_configure_no_args_fails(runner, cfg):
    p, _ = cfg
    result = runner.invoke(main, ["--config", str(p), "configure"])
    assert result.exit_code != 0


# --- start --team / --role ---


def test_start_team_and_role_invokes_run_bot_with_team_primitives(tmp_path, monkeypatch):
    import link_project_to_chat.cli as cli
    from link_project_to_chat.config import Config, TeamBotConfig, TeamConfig, save_config

    cfg_path = tmp_path / "config.json"
    config = Config(
        teams={
            "acme": TeamConfig(
                path=str(tmp_path),
                group_chat_id=-1001,
                bots={
                    "manager": TeamBotConfig(telegram_bot_token="t1", active_persona="developer"),
                    "dev":     TeamBotConfig(telegram_bot_token="t2", active_persona="tester"),
                },
            )
        }
    )
    save_config(config, cfg_path)

    calls = []
    def fake_run_bot(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr("link_project_to_chat.bot.run_bot", fake_run_bot)

    from click.testing import CliRunner
    runner = CliRunner()
    result = runner.invoke(cli.main, ["--config", str(cfg_path), "start", "--team", "acme", "--role", "manager"])
    assert result.exit_code == 0, result.output
    assert calls, "run_bot should have been called"
    args, kwargs = calls[0]
    # Token could be positional (3rd) or keyword — accept either shape
    token = kwargs.get("token") or (args[2] if len(args) > 2 else None)
    assert token == "t1"
    assert kwargs.get("team_name") == "acme"
    assert kwargs.get("active_persona") == "developer"
    assert kwargs.get("group_chat_id") == -1001
    assert kwargs.get("role") == "manager"


def test_start_team_passes_structured_room_binding(tmp_path, monkeypatch):
    import link_project_to_chat.cli as cli
    from link_project_to_chat.config import Config, RoomBinding, TeamBotConfig, TeamConfig, save_config

    cfg_path = tmp_path / "config.json"
    room = RoomBinding(transport_id="google_chat", native_id="spaces/AAAA1234")
    config = Config(
        teams={
            "acme": TeamConfig(
                path=str(tmp_path),
                room=room,
                bots={"manager": TeamBotConfig(telegram_bot_token="t1")},
            )
        }
    )
    save_config(config, cfg_path)

    calls = []
    monkeypatch.setattr("link_project_to_chat.bot.run_bot", lambda *a, **k: calls.append((a, k)))

    result = CliRunner().invoke(
        cli.main,
        ["--config", str(cfg_path), "start", "--team", "acme", "--role", "manager"],
    )

    assert result.exit_code == 0, result.output
    assert calls[0][1]["room"] == room


def test_run_bot_accepts_structured_room_binding(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import link_project_to_chat.bot as bot_mod
    from link_project_to_chat.config import RoomBinding

    room = RoomBinding(transport_id="telegram", native_id="-100123")
    captured: dict[str, object] = {}

    class FakeProjectBot:
        def __init__(self, *args, **kwargs):
            captured["kwargs"] = kwargs
            self.task_manager = SimpleNamespace(
                backend=SimpleNamespace(session_id=None, model=None, effort=None)
            )

        def build(self):
            pass

        def run(self):
            pass

    monkeypatch.setattr(bot_mod, "ProjectBot", FakeProjectBot)

    bot_mod.run_bot(
        "acme_dev",
        tmp_path,
        "token",
        allowed_usernames=["alice"],
        team_name="acme",
        role="dev",
        room=room,
    )

    assert captured["kwargs"]["room"] == room


def test_start_team_applies_default_model(tmp_path, monkeypatch):
    """When no --model is given, team bots should fall back to config.default_model."""
    import link_project_to_chat.cli as cli
    from link_project_to_chat.config import Config, TeamBotConfig, TeamConfig, save_config

    cfg_path = tmp_path / "config.json"
    config = Config(
        default_model="opus[1m]",
        teams={
            "acme": TeamConfig(
                path=str(tmp_path),
                group_chat_id=-1001,
                bots={"manager": TeamBotConfig(telegram_bot_token="t1")},
            )
        },
    )
    save_config(config, cfg_path)

    calls = []
    def fake_run_bot(*args, **kwargs):
        calls.append((args, kwargs))
    monkeypatch.setattr("link_project_to_chat.bot.run_bot", fake_run_bot)

    from click.testing import CliRunner
    result = CliRunner().invoke(
        cli.main, ["--config", str(cfg_path), "start", "--team", "acme", "--role", "manager"]
    )
    assert result.exit_code == 0, result.output
    _, kwargs = calls[0]
    assert kwargs.get("model") == "opus[1m]"


def test_start_team_passes_persisted_team_session_id(tmp_path, monkeypatch):
    import json

    import link_project_to_chat.cli as cli

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "teams": {
                    "acme": {
                        "path": str(tmp_path),
                        "group_chat_id": -1001,
                        "bots": {
                            "manager": {
                                "telegram_bot_token": "t1",
                                "session_id": "sess-123",
                            }
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    calls = []
    monkeypatch.setattr(
        "link_project_to_chat.bot.run_bot",
        lambda *a, **k: calls.append((a, k)),
    )

    from click.testing import CliRunner

    result = CliRunner().invoke(
        cli.main,
        ["--config", str(cfg_path), "start", "--team", "acme", "--role", "manager"],
    )
    assert result.exit_code == 0, result.output
    assert calls[0][1].get("session_id") == "sess-123"


def test_start_team_explicit_model_overrides_default(tmp_path, monkeypatch):
    import link_project_to_chat.cli as cli
    from link_project_to_chat.config import Config, TeamBotConfig, TeamConfig, save_config

    cfg_path = tmp_path / "config.json"
    config = Config(
        default_model="opus[1m]",
        teams={
            "acme": TeamConfig(
                path=str(tmp_path),
                group_chat_id=-1001,
                bots={"manager": TeamBotConfig(telegram_bot_token="t1")},
            )
        },
    )
    save_config(config, cfg_path)

    calls = []
    monkeypatch.setattr(
        "link_project_to_chat.bot.run_bot",
        lambda *a, **k: calls.append((a, k)),
    )

    from click.testing import CliRunner
    result = CliRunner().invoke(
        cli.main,
        ["--config", str(cfg_path), "start", "--team", "acme", "--role", "manager", "--model", "haiku"],
    )
    assert result.exit_code == 0, result.output
    assert calls[0][1].get("model") == "haiku"


def test_start_team_prefers_default_model_claude_over_legacy(tmp_path, monkeypatch):
    """When ``default_model_claude`` is set but the legacy ``default_model``
    isn't (e.g. a config that's only ever been touched by post-migration code),
    the team-bot fallback must still pick up ``default_model_claude``. Reads
    cannot depend on the legacy mirror being present."""
    import json

    import link_project_to_chat.cli as cli

    cfg_path = tmp_path / "config.json"
    # NOTE: only ``default_model_claude`` is written — no legacy mirror. This
    # asserts that the read path actually consults the new field.
    cfg_path.write_text(
        json.dumps(
            {
                "default_model_claude": "haiku",
                "teams": {
                    "acme": {
                        "path": str(tmp_path),
                        "group_chat_id": -1001,
                        "bots": {"manager": {"telegram_bot_token": "t1"}},
                    }
                },
            }
        )
    )

    calls = []
    monkeypatch.setattr(
        "link_project_to_chat.bot.run_bot",
        lambda *a, **k: calls.append((a, k)),
    )

    from click.testing import CliRunner
    result = CliRunner().invoke(
        cli.main, ["--config", str(cfg_path), "start", "--team", "acme", "--role", "manager"]
    )
    assert result.exit_code == 0, result.output
    assert calls[0][1].get("model") == "haiku"


def test_start_team_codex_ignores_claude_default_model(tmp_path, monkeypatch):
    import json

    import link_project_to_chat.cli as cli

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "default_model_claude": "opus[1m]",
                "teams": {
                    "acme": {
                        "path": str(tmp_path),
                        "group_chat_id": -1001,
                        "bots": {
                            "manager": {
                                "telegram_bot_token": "t1",
                                "backend": "codex",
                            }
                        },
                    }
                },
            }
        )
    )

    calls = []
    monkeypatch.setattr(
        "link_project_to_chat.bot.run_bot",
        lambda *a, **k: calls.append((a, k)),
    )

    from click.testing import CliRunner
    result = CliRunner().invoke(
        cli.main, ["--config", str(cfg_path), "start", "--team", "acme", "--role", "manager"]
    )
    assert result.exit_code == 0, result.output
    assert calls[0][1].get("backend_name") == "codex"
    assert calls[0][1].get("model") is None


def test_start_team_codex_prefers_codex_backend_state_model(tmp_path, monkeypatch):
    import json

    import link_project_to_chat.cli as cli

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "default_model_claude": "opus[1m]",
                "teams": {
                    "acme": {
                        "path": str(tmp_path),
                        "group_chat_id": -1001,
                        "bots": {
                            "manager": {
                                "telegram_bot_token": "t1",
                                "backend": "codex",
                                "backend_state": {"codex": {"model": "gpt-5.5"}},
                            }
                        },
                    }
                },
            }
        )
    )

    calls = []
    monkeypatch.setattr(
        "link_project_to_chat.bot.run_bot",
        lambda *a, **k: calls.append((a, k)),
    )

    from click.testing import CliRunner
    result = CliRunner().invoke(
        cli.main, ["--config", str(cfg_path), "start", "--team", "acme", "--role", "manager"]
    )
    assert result.exit_code == 0, result.output
    assert calls[0][1].get("model") == "gpt-5.5"


def test_start_project_prefers_backend_state_model_over_legacy(tmp_path, monkeypatch):
    """When backend_state.claude.model is set, ``cli start --project`` uses
    that for the model fallback even if the legacy flat ``model`` key disagrees.
    backend_state is the source of truth post-Phase 2."""
    import json

    import link_project_to_chat.cli as cli

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "projects": {
                    "myproj": {
                        "path": str(tmp_path),
                        "telegram_bot_token": "TOK",
                        "backend": "claude",
                        "backend_state": {"claude": {"model": "opus"}},
                        "model": "sonnet",  # stale legacy mirror
                    }
                }
            }
        )
    )

    calls = []
    monkeypatch.setattr(
        "link_project_to_chat.bot.run_bot",
        lambda *a, **k: calls.append((a, k)),
    )

    from click.testing import CliRunner
    result = CliRunner().invoke(
        cli.main, ["--config", str(cfg_path), "start", "--project", "myproj"]
    )
    assert result.exit_code == 0, result.output
    assert calls[0][1].get("model") == "opus"


def test_start_project_codex_ignores_legacy_claude_model(tmp_path, monkeypatch):
    import json

    import link_project_to_chat.cli as cli

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "projects": {
                    "myproj": {
                        "path": str(tmp_path),
                        "telegram_bot_token": "TOK",
                        "backend": "codex",
                        "model": "opus[1m]",
                    }
                }
            }
        )
    )

    calls = []
    monkeypatch.setattr(
        "link_project_to_chat.bot.run_bot",
        lambda *a, **k: calls.append((a, k)),
    )

    from click.testing import CliRunner
    result = CliRunner().invoke(
        cli.main, ["--config", str(cfg_path), "start", "--project", "myproj"]
    )
    assert result.exit_code == 0, result.output
    assert calls[0][1].get("backend_name") == "codex"
    assert calls[0][1].get("model") is None


def test_start_single_project_codex_ignores_legacy_claude_model(tmp_path, monkeypatch):
    import json

    import link_project_to_chat.cli as cli

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "projects": {
                    "myproj": {
                        "path": str(tmp_path),
                        "telegram_bot_token": "TOK",
                        "backend": "codex",
                        "model": "opus[1m]",
                    }
                }
            }
        )
    )

    calls = []
    monkeypatch.setattr(
        "link_project_to_chat.bot.run_bot",
        lambda *a, **k: calls.append((a, k)),
    )

    from click.testing import CliRunner
    result = CliRunner().invoke(cli.main, ["--config", str(cfg_path), "start"])
    assert result.exit_code == 0, result.output
    assert calls[0][1].get("backend_name") == "codex"
    assert calls[0][1].get("model") is None


def test_start_team_missing_role_errors(tmp_path, monkeypatch):
    import link_project_to_chat.cli as cli
    from link_project_to_chat.config import Config, TeamBotConfig, TeamConfig, save_config
    cfg_path = tmp_path / "config.json"
    config = Config(teams={"acme": TeamConfig(path=str(tmp_path), group_chat_id=-1,
        bots={"manager": TeamBotConfig(telegram_bot_token="t1")})})
    save_config(config, cfg_path)
    from click.testing import CliRunner
    result = CliRunner().invoke(cli.main, ["--config", str(cfg_path), "start", "--team", "acme"])
    assert result.exit_code != 0
    assert "--role is required" in (result.output or str(result.exception))


def test_start_team_unknown_team_errors(tmp_path, monkeypatch):
    import link_project_to_chat.cli as cli
    from link_project_to_chat.config import Config, save_config
    cfg_path = tmp_path / "config.json"
    save_config(Config(), cfg_path)
    from click.testing import CliRunner
    result = CliRunner().invoke(cli.main, ["--config", str(cfg_path), "start", "--team", "ghost", "--role", "manager"])
    assert result.exit_code != 0
    assert "not found" in (result.output or str(result.exception))


def test_start_team_wrong_role_errors(tmp_path, monkeypatch):
    import link_project_to_chat.cli as cli
    from link_project_to_chat.config import Config, TeamBotConfig, TeamConfig, save_config
    cfg_path = tmp_path / "config.json"
    config = Config(teams={"acme": TeamConfig(path=str(tmp_path), group_chat_id=-1,
        bots={"manager": TeamBotConfig(telegram_bot_token="t1")})})
    save_config(config, cfg_path)
    from click.testing import CliRunner
    result = CliRunner().invoke(cli.main, ["--config", str(cfg_path), "start", "--team", "acme", "--role", "dev"])
    assert result.exit_code != 0
    # dev is a valid click.Choice value, so the error is from our "Role not in team" guard
    assert "not in team" in (result.output or str(result.exception)) or "dev" in (result.output or str(result.exception))


def test_start_project_with_project_usernames_uses_project_allowed_users(tmp_path, monkeypatch):
    import link_project_to_chat.cli as cli
    from link_project_to_chat.config import AllowedUser, Config, ProjectConfig, save_config

    cfg_path = tmp_path / "config.json"
    save_config(
        Config(
            allowed_users=[AllowedUser(username="alice", role="executor")],
            projects={
                "demo": ProjectConfig(
                    path=str(tmp_path),
                    telegram_bot_token="tok",
                    allowed_users=[AllowedUser(username="bob", role="executor")],
                )
            },
        ),
        cfg_path,
    )

    calls = []
    monkeypatch.setattr(
        "link_project_to_chat.bot.run_bot",
        lambda *a, **k: calls.append((a, k)),
    )

    result = CliRunner().invoke(
        cli.main,
        ["--config", str(cfg_path), "start", "--project", "demo"],
    )

    assert result.exit_code == 0, result.output
    _, kwargs = calls[0]
    assert [u.username for u in kwargs["allowed_users"]] == ["bob"]
    assert kwargs["auth_source"] == "project"


def test_start_project_username_override_uses_one_user_allow_list(tmp_path, monkeypatch):
    import link_project_to_chat.cli as cli
    from link_project_to_chat.config import AllowedUser, Config, ProjectConfig, save_config

    cfg_path = tmp_path / "config.json"
    save_config(
        Config(
            allowed_users=[AllowedUser(username="alice", role="executor")],
            projects={
                "demo": ProjectConfig(
                    path=str(tmp_path),
                    telegram_bot_token="tok",
                    allowed_users=[AllowedUser(username="bob", role="executor")],
                )
            },
        ),
        cfg_path,
    )

    calls = []
    monkeypatch.setattr(
        "link_project_to_chat.bot.run_bot",
        lambda *a, **k: calls.append((a, k)),
    )

    result = CliRunner().invoke(
        cli.main,
        ["--config", str(cfg_path), "start", "--project", "demo", "--username", "carol"],
    )

    assert result.exit_code == 0, result.output
    _, kwargs = calls[0]
    assert [u.username for u in kwargs["allowed_users"]] == ["carol"]


def test_start_single_project_without_project_flag_uses_project_allowed_users(tmp_path, monkeypatch):
    import link_project_to_chat.cli as cli
    from link_project_to_chat.config import AllowedUser, Config, ProjectConfig, save_config

    cfg_path = tmp_path / "config.json"
    save_config(
        Config(
            allowed_users=[AllowedUser(username="alice", role="executor")],
            projects={
                "demo": ProjectConfig(
                    path=str(tmp_path),
                    telegram_bot_token="tok",
                    allowed_users=[AllowedUser(username="bob", role="executor")],
                )
            },
        ),
        cfg_path,
    )

    calls = []
    monkeypatch.setattr(
        "link_project_to_chat.bot.run_bot",
        lambda *a, **k: calls.append((a, k)),
    )

    result = CliRunner().invoke(cli.main, ["--config", str(cfg_path), "start"])

    assert result.exit_code == 0, result.output
    _, kwargs = calls[0]
    assert [u.username for u in kwargs["allowed_users"]] == ["bob"]


def test_start_ad_hoc_does_not_attach_persistent_trust_callback(tmp_path, monkeypatch):
    import link_project_to_chat.cli as cli

    project_path = tmp_path / "adhoc"
    project_path.mkdir()
    calls = []
    monkeypatch.setattr(
        "link_project_to_chat.bot.run_bot",
        lambda *a, **k: calls.append((a, k)),
    )

    result = CliRunner().invoke(
        cli.main,
        [
            "start",
            "--path",
            str(project_path),
            "--token",
            "tok",
            "--username",
            "alice",
        ],
    )

    assert result.exit_code == 0, result.output
    _, kwargs = calls[0]
    assert [u.username for u in kwargs["allowed_users"]] == ["alice"]
    # Legacy ``on_trust`` callback was removed in v1.0; verify the call site
    # no longer passes it.
    assert "on_trust" not in kwargs


def test_plugin_call_unknown_plugin_exits_nonzero(tmp_path):
    from click.testing import CliRunner

    from link_project_to_chat.cli import main

    runner = CliRunner()
    cfg = tmp_path / "config.json"
    cfg.write_text(
        '{"projects": {"p": {"path": "/tmp", "telegram_bot_token": "t"}}}'
    )

    result = runner.invoke(
        main,
        ["--config", str(cfg), "plugin-call", "p", "does-not-exist", "tool", "{}"],
    )
    assert result.exit_code != 0


def test_migrate_config_dry_run_does_not_write(tmp_path):
    """`migrate-config --dry-run` shows the migration without modifying the file."""
    import json
    from click.testing import CliRunner

    from link_project_to_chat.cli import main

    runner = CliRunner()
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "projects": {
            "p": {
                "path": "/tmp",
                "telegram_bot_token": "t",
                "allowed_usernames": ["alice"],
                "trusted_users": {"alice": 12345},
            }
        }
    }))
    before = cfg.read_text()
    result = runner.invoke(main, ["--config", str(cfg), "migrate-config", "--dry-run"])
    assert result.exit_code == 0
    assert "alice" in result.output
    assert "executor" in result.output
    # File is unchanged.
    assert cfg.read_text() == before


def test_migrate_config_applies_migration(tmp_path):
    """`migrate-config` (no --dry-run) writes the new shape to disk."""
    import json
    from click.testing import CliRunner

    from link_project_to_chat.cli import main

    runner = CliRunner()
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "projects": {
            "p": {
                "path": "/tmp",
                "telegram_bot_token": "t",
                "allowed_usernames": ["alice"],
                "trusted_users": {"alice": 12345},
            }
        }
    }))
    result = runner.invoke(main, ["--config", str(cfg), "migrate-config"])
    assert result.exit_code == 0
    written = json.loads(cfg.read_text())
    assert "allowed_usernames" not in written["projects"]["p"]
    assert written["projects"]["p"]["allowed_users"] == [
        {"username": "alice", "role": "executor", "locked_identities": ["telegram:12345"]},
    ]


def test_migrate_config_nonzero_exit_on_empty_allowlist(tmp_path):
    """Migration that leaves any project with empty allowed_users exits non-zero."""
    import json
    from click.testing import CliRunner

    from link_project_to_chat.cli import main

    runner = CliRunner()
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "projects": {
            "p": {"path": "/tmp", "telegram_bot_token": "t"}
        }
    }))
    result = runner.invoke(main, ["--config", str(cfg), "migrate-config"])
    assert result.exit_code != 0


def test_configure_add_user_persists(tmp_path):
    """`configure --add-user alice:executor` writes an AllowedUser to the global allow-list."""
    import json
    from click.testing import CliRunner

    from link_project_to_chat.cli import main

    runner = CliRunner()
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"projects": {}}))
    result = runner.invoke(main, ["--config", str(cfg), "configure", "--add-user", "alice:executor"])
    assert result.exit_code == 0
    written = json.loads(cfg.read_text())
    assert written["allowed_users"] == [
        {"username": "alice", "role": "executor"},
    ]


def test_configure_reset_user_identity_per_transport(tmp_path):
    """`configure --reset-user-identity alice:web` clears web entries only,
    leaving other transports' locks intact. Regression test for the bug
    where the whole string was normalized before the colon-split."""
    import json
    from click.testing import CliRunner

    from link_project_to_chat.cli import main

    runner = CliRunner()
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "allowed_users": [{
            "username": "alice",
            "role": "executor",
            "locked_identities": ["telegram:12345", "web:web-session:abc"],
        }],
        "projects": {},
    }))
    result = runner.invoke(
        main, ["--config", str(cfg), "configure", "--reset-user-identity", "alice:web"],
    )
    assert result.exit_code == 0
    written = json.loads(cfg.read_text())
    alice = written["allowed_users"][0]
    assert alice["locked_identities"] == ["telegram:12345"]


def test_configure_reset_user_identity_clears_all(tmp_path):
    """`configure --reset-user-identity alice` (no :transport) clears all."""
    import json
    from click.testing import CliRunner

    from link_project_to_chat.cli import main

    runner = CliRunner()
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "allowed_users": [{
            "username": "alice",
            "role": "executor",
            "locked_identities": ["telegram:12345", "web:web-session:abc"],
        }],
        "projects": {},
    }))
    result = runner.invoke(
        main, ["--config", str(cfg), "configure", "--reset-user-identity", "alice"],
    )
    assert result.exit_code == 0
    written = json.loads(cfg.read_text())
    alice = written["allowed_users"][0]
    # Empty list serializes as the absent key.
    assert alice.get("locked_identities", []) == []


def test_configure_legacy_username_flag_warns(tmp_path):
    """Legacy `--username` flag works but emits a deprecation warning."""
    import json
    from click.testing import CliRunner

    from link_project_to_chat.cli import main

    runner = CliRunner()
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"projects": {}}))
    result = runner.invoke(main, ["--config", str(cfg), "configure", "--username", "bob"])
    assert result.exit_code == 0
    assert "deprecated" in result.output.lower() or "deprecated" in (result.stderr or "").lower()
    written = json.loads(cfg.read_text())
    assert any(u["username"] == "bob" for u in written.get("allowed_users", []))


def test_start_manager_requires_allowed_users(tmp_path, monkeypatch):
    """start-manager must hard-fail when allowed_users is empty (fail-closed)."""
    from link_project_to_chat.cli import main
    from link_project_to_chat.config import Config, save_config
    from click.testing import CliRunner

    cfg_path = tmp_path / "config.json"
    # Manager token set, but NO allowed_users. (Config has no top-level
    # telegram_bot_token field — that's per-ProjectConfig. start-manager
    # only needs manager_telegram_bot_token.)
    save_config(
        Config(manager_telegram_bot_token="m", allowed_users=[]),
        cfg_path,
    )
    runner = CliRunner()
    result = runner.invoke(main, ["--config", str(cfg_path), "start-manager"])
    assert result.exit_code != 0
    assert "No users authorized" in (result.output + str(result.exception))


def test_start_manager_passes_allowed_users_into_manager_bot(tmp_path, monkeypatch):
    """start-manager must construct ManagerBot with allowed_users=, not the
    legacy allowed_usernames=. Regression: pre-Task-4 start_manager passed
    allowed_usernames= and trusted_users=, both of which Task 5 deletes."""
    from link_project_to_chat.cli import main
    from link_project_to_chat.config import AllowedUser, Config, save_config
    from click.testing import CliRunner

    cfg_path = tmp_path / "config.json"
    save_config(
        Config(
            manager_telegram_bot_token="m",
            allowed_users=[AllowedUser(username="alice", role="executor")],
        ),
        cfg_path,
    )

    captured_kwargs: dict = {}

    class _FakeBot:
        def __init__(self, *args, **kwargs):
            captured_kwargs["args"] = args
            captured_kwargs["kwargs"] = kwargs

        def build(self):
            class _App:
                def run_polling(self_inner): return None
            return _App()

    monkeypatch.setattr("link_project_to_chat.manager.bot.ManagerBot", _FakeBot)

    class _FakePM:
        def __init__(self, **kwargs): pass
        def start_autostart(self): return 0
        def reap_orphans(self): return []

    monkeypatch.setattr("link_project_to_chat.manager.process.ProcessManager", _FakePM)

    runner = CliRunner()
    result = runner.invoke(main, ["--config", str(cfg_path), "start-manager"])
    assert result.exit_code == 0, result.output
    # allowed_users= must be present; legacy kwargs must NOT be passed.
    assert "allowed_users" in captured_kwargs["kwargs"]
    assert captured_kwargs["kwargs"]["allowed_users"][0].username == "alice"
    assert "allowed_usernames" not in captured_kwargs["kwargs"]
    assert "trusted_users" not in captured_kwargs["kwargs"]


def test_start_manager_runs_migration_on_pending(tmp_path, monkeypatch):
    """start-manager must call save_config when load_config sets migration_pending,
    mirroring start's behavior."""
    from link_project_to_chat.cli import main
    from link_project_to_chat.config import AllowedUser, Config, save_config
    from click.testing import CliRunner

    cfg_path = tmp_path / "config.json"
    # Write a legacy-shaped config that load_config will migrate.
    import json
    cfg_path.write_text(json.dumps({
        "telegram_bot_token": "x",
        "manager_telegram_bot_token": "m",
        "allowed_usernames": ["alice"],  # legacy → will trigger migration
    }))

    monkeypatch.setattr(
        "link_project_to_chat.manager.bot.ManagerBot",
        type("_FB", (), {"__init__": lambda *a, **k: None,
                        "build": lambda self: type("_A", (), {"run_polling": lambda s: None})()}),
    )
    monkeypatch.setattr(
        "link_project_to_chat.manager.process.ProcessManager",
        type("_FPM", (), {"__init__": lambda *a, **k: None,
                          "start_autostart": lambda self: 0,
                          "reap_orphans": lambda self: []}),
    )

    runner = CliRunner()
    result = runner.invoke(main, ["--config", str(cfg_path), "start-manager"])
    assert result.exit_code == 0, result.output

    # After migration, the on-disk file no longer has the legacy key and DOES
    # have allowed_users with role=executor for alice.
    reloaded = json.loads(cfg_path.read_text())
    assert "allowed_usernames" not in reloaded
    assert any(u["username"] == "alice" and u["role"] == "executor"
               for u in reloaded.get("allowed_users", []))


def test_manager_bot_accepts_allowed_users_kwarg():
    """Constructor regression: ManagerBot must accept allowed_users= and set
    self._allowed_users. Catches a future caller that drops this kwarg."""
    from link_project_to_chat.config import AllowedUser
    from link_project_to_chat.manager.bot import ManagerBot
    from link_project_to_chat.manager.process import ProcessManager

    pm = ProcessManager.__new__(ProcessManager)
    bot = ManagerBot(
        token="t",
        process_manager=pm,
        allowed_users=[AllowedUser(username="alice", role="executor")],
    )
    assert bot._allowed_users == [AllowedUser(username="alice", role="executor")]


# Removed in Task 5 Step 11: ``test_manager_bot_legacy_auth_works_with_allowed_users_only``
# called the deleted legacy ``_auth(user)`` method. The post-rewrite
# equivalent (an authorized user authenticates via ``_auth_identity``) is
# covered by ``tests/test_auth_roles.py``.


def test_version_is_consistent_across_pyproject_and_init():
    """Version string in pyproject.toml must match src/.../__init__.py."""
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    pyproject = tomllib.loads((root / "pyproject.toml").read_text())
    pyproject_version = pyproject["project"]["version"]

    import link_project_to_chat
    assert link_project_to_chat.__version__ == pyproject_version, (
        f"Version drift: pyproject.toml={pyproject_version!r} vs "
        f"__init__.py={link_project_to_chat.__version__!r}"
    )


def test_projects_add_with_respond_in_groups_writes_field(tmp_path):
    """`projects add --respond-in-groups` writes respond_in_groups=True
    into the project entry on disk."""
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"projects": {}}))
    runner = CliRunner()
    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    result = runner.invoke(main, [
        "--config", str(cfg),
        "projects", "add",
        "--name", "myproj",
        "--path", str(proj_dir),
        "--token", "t",
        "--respond-in-groups",
    ])
    assert result.exit_code == 0, result.output

    on_disk = json.loads(cfg.read_text())
    proj = on_disk["projects"]["myproj"]
    assert proj["respond_in_groups"] is True


def test_projects_add_without_flag_omits_field(tmp_path):
    """Default off: the field is not written when the flag is absent."""
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"projects": {}}))
    runner = CliRunner()
    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    result = runner.invoke(main, [
        "--config", str(cfg),
        "projects", "add",
        "--name", "myproj",
        "--path", str(proj_dir),
        "--token", "t",
    ])
    assert result.exit_code == 0, result.output
    on_disk = json.loads(cfg.read_text())
    proj = on_disk["projects"]["myproj"]
    assert "respond_in_groups" not in proj


def test_projects_edit_respond_in_groups_true(tmp_path):
    """`projects edit myproj respond_in_groups true` flips the flag on."""
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "projects": {
            "myproj": {
                "path": str(tmp_path),
                "telegram_bot_token": "t",
            }
        }
    }))
    runner = CliRunner()
    result = runner.invoke(main, [
        "--config", str(cfg),
        "projects", "edit", "myproj", "respond_in_groups", "true",
    ])
    assert result.exit_code == 0, result.output

    on_disk = json.loads(cfg.read_text())
    assert on_disk["projects"]["myproj"]["respond_in_groups"] is True


def test_projects_edit_respond_in_groups_false_strips_field(tmp_path):
    """`projects edit myproj respond_in_groups false` flips the flag off,
    and the on-disk emit-only-when-True policy strips the key."""
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "projects": {
            "myproj": {
                "path": str(tmp_path),
                "telegram_bot_token": "t",
                "respond_in_groups": True,
            }
        }
    }))
    runner = CliRunner()
    result = runner.invoke(main, [
        "--config", str(cfg),
        "projects", "edit", "myproj", "respond_in_groups", "false",
    ])
    assert result.exit_code == 0, result.output

    on_disk = json.loads(cfg.read_text())
    proj = on_disk["projects"]["myproj"]
    assert "respond_in_groups" not in proj


def test_projects_edit_respond_in_groups_invalid_input_errors(tmp_path):
    """Garbage values produce a non-zero exit and don't mutate the file."""
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "projects": {
            "myproj": {
                "path": str(tmp_path),
                "telegram_bot_token": "t",
            }
        }
    }))
    runner = CliRunner()
    result = runner.invoke(main, [
        "--config", str(cfg),
        "projects", "edit", "myproj", "respond_in_groups", "maybe",
    ])
    assert result.exit_code != 0
    # File unchanged.
    on_disk = json.loads(cfg.read_text())
    assert "respond_in_groups" not in on_disk["projects"]["myproj"]


def test_projects_add_safety_prompt_flag_writes_custom_string(tmp_path):
    """--safety-prompt flag stores a custom guardrail."""
    from click.testing import CliRunner
    from link_project_to_chat.cli import main
    proj_path = tmp_path / "proj"
    proj_path.mkdir()
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text('{"projects": {}}')
    runner = CliRunner()
    result = runner.invoke(main, [
        "--config", str(cfg_file),
        "projects", "add",
        "--name", "p", "--path", str(proj_path),
        "--token", "t",
        "--safety-prompt", "custom guardrail text",
    ])
    assert result.exit_code == 0, result.output
    import json
    raw = json.loads(cfg_file.read_text())
    assert raw["projects"]["p"]["safety_prompt"] == "custom guardrail text"


def test_projects_edit_safety_prompt_to_empty_string_disables(tmp_path):
    """`projects edit p safety_prompt ""` writes empty string (disable signal)."""
    from click.testing import CliRunner
    from link_project_to_chat.cli import main
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(
        '{"projects": {"p": {"path": "' + str(tmp_path) + '", "telegram_bot_token": "t"}}}'
    )
    runner = CliRunner()
    result = runner.invoke(main, [
        "--config", str(cfg_file),
        "projects", "edit", "p", "safety_prompt", "",
    ])
    assert result.exit_code == 0, result.output
    import json
    raw = json.loads(cfg_file.read_text())
    assert raw["projects"]["p"]["safety_prompt"] == ""


def test_projects_edit_safety_prompt_to_default_strips_key(tmp_path):
    """`projects edit p safety_prompt default` strips the key (back to None state)."""
    from click.testing import CliRunner
    from link_project_to_chat.cli import main
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(
        '{"projects": {"p": {"path": "' + str(tmp_path) + '", '
        '"telegram_bot_token": "t", "safety_prompt": "old"}}}'
    )
    runner = CliRunner()
    result = runner.invoke(main, [
        "--config", str(cfg_file),
        "projects", "edit", "p", "safety_prompt", "default",
    ])
    assert result.exit_code == 0, result.output
    import json
    raw = json.loads(cfg_file.read_text())
    assert "safety_prompt" not in raw["projects"]["p"]


def test_start_accepts_google_chat_config_json(monkeypatch, tmp_path):
    """When --google-chat-config-json is set, the start command must use the
    resolved blob instead of reading config.google_chat from disk."""
    import json
    from click.testing import CliRunner
    from link_project_to_chat.cli import main

    captured = {}

    def fake_run_bot(*args, **kwargs):
        # run_bot's positional signature is (name, path, token, ...).
        captured["transport_kind"] = kwargs.get("transport_kind")
        captured["google_chat"] = kwargs["config"].google_chat
        return  # don't actually start the bot

    monkeypatch.setattr("link_project_to_chat.bot.run_bot", fake_run_bot)

    proj_dir = tmp_path / "alpha"
    proj_dir.mkdir()
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "allowed_users": [{"username": "alice", "role": "executor"}],
        "projects": {"alpha": {"path": str(proj_dir), "telegram_bot_token": "tok"}},
    }))

    blob = json.dumps({
        "service_account_file": "/keys/alpha.json",
        "port": 8091,
        "public_url": "https://alpha.example",
        "root_command_id": 7,
    })

    runner = CliRunner()
    result = runner.invoke(main, [
        "--config", str(cfg_path),
        "start",
        "--project", "alpha",
        "--transport", "google_chat",
        "--google-chat-config-json", blob,
    ])
    assert result.exit_code == 0, result.output

    # The resolved config blob got applied on top of (or in place of)
    # whatever was in config.json's google_chat block.
    assert captured["transport_kind"] == "google_chat"
    assert captured["google_chat"].service_account_file == "/keys/alpha.json"
    assert captured["google_chat"].port == 8091
    assert captured["google_chat"].public_url == "https://alpha.example"
    assert captured["google_chat"].root_command_id == 7
