from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import link_project_to_chat.config as config_module
from link_project_to_chat.config import (
    _atomic_write,
    AllowedUser,
    Config,
    ConfigError,
    ProjectConfig,
    TeamBotConfig,
    TeamConfig,
    clear_session,
    load_config,
    load_sessions,
    load_teams,
    patch_team,
    save_config,
    save_session,
)


def test_load_config_missing(tmp_path: Path):
    config = load_config(tmp_path / "missing.json")
    assert config.allowed_users == []
    assert config.projects == {}


def test_load_config_strips_at_and_lowercases(tmp_path: Path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"allowed_username": "@Alice", "projects": {}}))
    config = load_config(p)
    assert [u.username for u in config.allowed_users] == ["alice"]


def test_save_and_load_config(tmp_path: Path):
    p = tmp_path / "cfg.json"
    cfg = Config(
        allowed_users=[AllowedUser(username="bob", role="executor")],
        manager_telegram_bot_token="MGR",
        projects={"proj": ProjectConfig(path="/some/path", telegram_bot_token="TOK")},
    )
    save_config(cfg, p)
    if sys.platform != "win32":
        assert p.stat().st_mode & 0o777 == 0o600
    loaded = load_config(p)
    assert [u.username for u in loaded.allowed_users] == ["bob"]
    assert loaded.manager_telegram_bot_token == "MGR"
    assert loaded.projects["proj"].path == "/some/path"
    assert loaded.projects["proj"].telegram_bot_token == "TOK"


def test_save_config_preserves_unknown_keys(tmp_path: Path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"allowed_username": "alice", "future_key": "future_value", "projects": {}}))
    cfg = Config(allowed_users=[AllowedUser(username="alice", role="executor")])
    save_config(cfg, p)
    raw = json.loads(p.read_text())
    assert raw["future_key"] == "future_value"


def test_save_config_preserves_project_unknown_keys(tmp_path: Path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({
        "allowed_username": "alice",
        "projects": {
            "myproj": {
                "path": "/p",
                "telegram_bot_token": "T",
                "future_field": "future_value",
            }
        },
    }))
    cfg = Config(
        allowed_users=[AllowedUser(username="alice", role="executor")],
        projects={"myproj": ProjectConfig(path="/p", telegram_bot_token="T")},
    )
    save_config(cfg, p)
    raw = json.loads(p.read_text())
    # Forward-compat: keys this code version does not know about must be
    # preserved. Legacy Claude-shaped fields (``model``/``effort``/...) are
    # NOT unknown — they are tracked by ``backend_state`` and are mirrored
    # OUT at save time, so dropping them from the in-memory config drops
    # them from disk too.
    assert raw["projects"]["myproj"]["future_field"] == "future_value"


def test_save_config_removes_deleted_projects(tmp_path: Path):
    p = tmp_path / "cfg.json"
    cfg1 = Config(
        allowed_users=[AllowedUser(username="alice", role="executor")],
        projects={
            "a": ProjectConfig(path="/a", telegram_bot_token="Ta"),
            "b": ProjectConfig(path="/b", telegram_bot_token="Tb"),
        },
    )
    save_config(cfg1, p)
    cfg2 = Config(
        allowed_users=[AllowedUser(username="alice", role="executor")],
        projects={"a": ProjectConfig(path="/a", telegram_bot_token="Ta")},
    )
    save_config(cfg2, p)
    raw = json.loads(p.read_text())
    assert "a" in raw["projects"]
    assert "b" not in raw["projects"]


def test_save_and_load_per_project_username(tmp_path: Path):
    from link_project_to_chat.config import AllowedUser
    p = tmp_path / "cfg.json"
    cfg = Config(
        allowed_users=[AllowedUser(username="global", role="executor")],
        projects={"proj": ProjectConfig(
            path="/p", telegram_bot_token="T",
            allowed_users=[AllowedUser(username="perproject", role="executor")],
        )},
    )
    save_config(cfg, p)
    loaded = load_config(p)
    assert [u.username for u in loaded.projects["proj"].allowed_users] == ["perproject"]
    assert [u.username for u in loaded.allowed_users] == ["global"]


def test_save_and_load_per_project_locked_identity(tmp_path: Path):
    from link_project_to_chat.config import AllowedUser
    p = tmp_path / "cfg.json"
    cfg = Config(
        allowed_users=[AllowedUser(username="alice", role="executor")],
        projects={"proj": ProjectConfig(
            path="/p", telegram_bot_token="T",
            allowed_users=[AllowedUser(
                username="alice", role="executor", locked_identities=["telegram:42"],
            )],
        )},
    )
    save_config(cfg, p)
    loaded = load_config(p)
    assert loaded.projects["proj"].allowed_users[0].locked_identities == ["telegram:42"]


def test_save_and_load_global_locked_identity(tmp_path: Path):
    from link_project_to_chat.config import AllowedUser
    p = tmp_path / "cfg.json"
    cfg = Config(allowed_users=[
        AllowedUser(username="alice", role="executor", locked_identities=["telegram:99"]),
    ])
    save_config(cfg, p)
    loaded = load_config(p)
    assert loaded.allowed_users[0].locked_identities == ["telegram:99"]


def test_save_session_and_load(tmp_path: Path):
    p = tmp_path / "sessions.json"
    save_session("myproj", "sess-abc", p)
    if sys.platform != "win32":
        assert p.stat().st_mode & 0o777 == 0o600
    sessions = load_sessions(p)
    assert sessions["myproj"] == "sess-abc"


def test_save_session_merges(tmp_path: Path):
    p = tmp_path / "sessions.json"
    save_session("a", "id-a", p)
    save_session("b", "id-b", p)
    sessions = load_sessions(p)
    assert sessions["a"] == "id-a"
    assert sessions["b"] == "id-b"


def test_clear_session(tmp_path: Path):
    p = tmp_path / "sessions.json"
    save_session("x", "id-x", p)
    clear_session("x", p)
    assert "x" not in load_sessions(p)


def test_clear_session_missing_key(tmp_path: Path):
    p = tmp_path / "sessions.json"
    save_session("a", "id-a", p)
    clear_session("nope", p)  # should not raise
    assert load_sessions(p)["a"] == "id-a"


def test_load_sessions_missing(tmp_path: Path):
    assert load_sessions(tmp_path / "nope.json") == {}


def test_load_sessions_corrupt(tmp_path: Path):
    p = tmp_path / "sessions.json"
    p.write_text("not json")
    assert load_sessions(p) == {}


def test_patch_json_serializes_overlapping_writes(tmp_path: Path, monkeypatch):
    p = tmp_path / "cfg.json"

    first_ready = threading.Event()
    second_ready = threading.Event()
    release_first = threading.Event()
    order_lock = threading.Lock()
    call_order = {"n": 0}
    real_atomic_write = config_module._atomic_write

    def blocking_atomic_write(path: Path, data: str) -> None:
        with order_lock:
            call_order["n"] += 1
            idx = call_order["n"]
        if idx == 1:
            first_ready.set()
            assert release_first.wait(timeout=2)
        elif idx == 2:
            second_ready.set()
        real_atomic_write(path, data)

    monkeypatch.setattr(config_module, "_atomic_write", blocking_atomic_write)

    t1 = threading.Thread(target=save_session, args=("proj1", "sess-123", p))
    t1.start()
    assert first_ready.wait(timeout=1)

    t2 = threading.Thread(target=save_session, args=("proj2", "sess-456", p))
    t2.start()

    time.sleep(0.2)
    assert not second_ready.is_set()

    release_first.set()
    t1.join(timeout=2)
    t2.join(timeout=2)

    assert not t1.is_alive()
    assert not t2.is_alive()
    assert second_ready.is_set()

    raw = json.loads(p.read_text())
    # v1.0.0 dropped the legacy top-level mirror; canonical home is
    # backend_state["claude"]["session_id"].
    assert raw["projects"]["proj1"]["backend_state"]["claude"]["session_id"] == "sess-123"
    assert raw["projects"]["proj2"]["backend_state"]["claude"]["session_id"] == "sess-456"


def test_per_project_username_legacy_migrate_strips_at(tmp_path: Path):
    """Loader still accepts legacy ``allowed_username`` / ``username`` keys on
    disk for migration. The result lands in ``allowed_users`` with the @ stripped."""
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({
        "allowed_username": "global",
        "projects": {"proj": {"path": "/p", "telegram_bot_token": "T", "username": "@Alice"}},
    }))
    loaded = load_config(p)
    assert [u.username for u in loaded.projects["proj"].allowed_users] == ["alice"]


# --- New multi-user tests ---

def test_load_config_multi_user(tmp_path: Path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({
        "allowed_usernames": ["alice", "bob"],
        "trusted_user_ids": [10, 20],
        "github_pat": "ghp_test",
        "telegram_api_id": 12345,
        "telegram_api_hash": "abc123",
        "projects": {
            "proj": {
                "path": "/p",
                "telegram_bot_token": "T",
                "allowed_usernames": ["alice"],
                "trusted_user_ids": [10],
            }
        },
    }))
    config = load_config(p)
    # Legacy multi-user inputs synthesize into Config.allowed_users (Task 5
    # Step 12 removed the legacy fields; the migration helper still reads
    # raw JSON keys for compatibility).
    assert [u.username for u in config.allowed_users] == ["alice", "bob"]
    assert config.github_pat == "ghp_test"
    assert config.telegram_api_id == 12345
    assert config.telegram_api_hash == "abc123"
    assert [u.username for u in config.projects["proj"].allowed_users] == ["alice"]


def test_load_config_migrates_single_username(tmp_path: Path):
    """Legacy single-value keys auto-migrate to ``allowed_users``."""
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({
        "allowed_username": "alice",
        "trusted_user_id": 42,
        "projects": {
            "proj": {
                "path": "/p",
                "telegram_bot_token": "T",
                "username": "bob",
                "trusted_user_id": 99,
            }
        },
    }))
    config = load_config(p)
    assert [u.username for u in config.allowed_users] == ["alice"]
    assert config.allowed_users[0].locked_identities == ["telegram:42"]
    assert [u.username for u in config.projects["proj"].allowed_users] == ["bob"]
    assert config.projects["proj"].allowed_users[0].locked_identities == ["telegram:99"]


def test_load_config_empty_username_no_migration(tmp_path: Path):
    """Empty old username should result in empty list, not [AllowedUser('')]."""
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"allowed_username": "", "projects": {}}))
    config = load_config(p)
    assert config.allowed_users == []


def test_save_config_multi_user_roundtrip(tmp_path: Path):
    from link_project_to_chat.config import AllowedUser
    p = tmp_path / "cfg.json"
    cfg = Config(
        allowed_users=[
            AllowedUser(username="alice", role="executor", locked_identities=["telegram:10"]),
            AllowedUser(username="bob", role="executor", locked_identities=["telegram:20"]),
        ],
        github_pat="ghp_xxx",
        telegram_api_id=111,
        telegram_api_hash="hash",
        manager_telegram_bot_token="MGR",
        projects={"proj": ProjectConfig(
            path="/p", telegram_bot_token="T",
            allowed_users=[AllowedUser(
                username="alice", role="executor", locked_identities=["telegram:10"],
            )],
        )},
    )
    save_config(cfg, p)
    loaded = load_config(p)
    assert [u.username for u in loaded.allowed_users] == ["alice", "bob"]
    assert loaded.allowed_users[0].locked_identities == ["telegram:10"]
    assert loaded.github_pat == "ghp_xxx"
    assert loaded.telegram_api_id == 111
    assert loaded.telegram_api_hash == "hash"
    assert [u.username for u in loaded.projects["proj"].allowed_users] == ["alice"]


def test_save_config_removes_old_singular_keys(tmp_path: Path):
    """After save, legacy singular AND plural auth keys are stripped from disk;
    the new identity-keyed ``allowed_users`` shape carries the data forward.
    """
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({
        "allowed_username": "alice",
        "trusted_user_id": 42,
        "projects": {"proj": {"path": "/p", "telegram_bot_token": "T", "username": "bob"}},
    }))
    cfg = load_config(p)
    save_config(cfg, p)
    raw = json.loads(p.read_text())
    assert "allowed_username" not in raw
    assert "trusted_user_id" not in raw
    assert "allowed_usernames" not in raw
    assert "trusted_users" not in raw
    assert "trusted_user_ids" not in raw
    assert raw["allowed_users"] == [
        {"username": "alice", "role": "executor", "locked_identities": ["telegram:42"]},
    ]
    assert "username" not in raw["projects"]["proj"]
    assert "allowed_usernames" not in raw["projects"]["proj"]
    assert raw["projects"]["proj"]["allowed_users"] == [
        {"username": "bob", "role": "executor"},
    ]


# Removed in Task 5 Step 12: tests for ``add_trusted_user_id``,
# ``add_project_trusted_user_id``, ``bind_trusted_user``,
# ``bind_project_trusted_user``, ``unbind_trusted_user``,
# ``trusted_user_id_roundtrip``, ``save_project_trusted_user_id``.
# All wrote to legacy fields that no longer exist.


def test_load_config_voice_fields(tmp_path: Path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({
        "allowed_usernames": ["alice"],
        "stt_backend": "whisper-api",
        "openai_api_key": "sk-test123",
        "whisper_model": "whisper-1",
        "whisper_language": "en",
        "projects": {},
    }))
    config = load_config(p)
    assert config.stt_backend == "whisper-api"
    assert config.openai_api_key == "sk-test123"
    assert config.whisper_model == "whisper-1"
    assert config.whisper_language == "en"


def test_load_config_voice_defaults(tmp_path: Path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"allowed_usernames": ["alice"], "projects": {}}))
    config = load_config(p)
    assert config.stt_backend == ""
    assert config.openai_api_key == ""
    assert config.whisper_model == "whisper-1"
    assert config.whisper_language == ""


def test_save_config_voice_roundtrip(tmp_path: Path):
    p = tmp_path / "cfg.json"
    cfg = Config(
        allowed_users=[AllowedUser(username="alice", role="executor")],
        stt_backend="whisper-api",
        openai_api_key="sk-xxx",
        whisper_model="small",
        whisper_language="ka",
    )
    save_config(cfg, p)
    loaded = load_config(p)
    assert loaded.stt_backend == "whisper-api"
    assert loaded.openai_api_key == "sk-xxx"
    assert loaded.whisper_model == "small"
    assert loaded.whisper_language == "ka"


def test_save_config_omits_empty_voice_fields(tmp_path: Path):
    p = tmp_path / "cfg.json"
    cfg = Config(allowed_users=[AllowedUser(username="alice", role="executor")])
    save_config(cfg, p)
    raw = json.loads(p.read_text())
    assert "stt_backend" not in raw
    assert "openai_api_key" not in raw
    assert "whisper_language" not in raw
    # whisper_model defaults to "whisper-1" and is omitted when at default
    assert "whisper_model" not in raw


def test_save_config_persists_non_default_model(tmp_path: Path):
    """Non-default whisper_model must round-trip."""
    p = tmp_path / "cfg.json"
    cfg = Config(allowed_users=[AllowedUser(username="alice", role="executor")], whisper_model="small")
    save_config(cfg, p)
    assert json.loads(p.read_text())["whisper_model"] == "small"
    assert load_config(p).whisper_model == "small"


class TestAtomicWrite:
    def test_writes_file_correctly(self, tmp_path):
        target = tmp_path / "test.json"
        _atomic_write(target, '{"key": "value"}\n')
        assert target.read_text() == '{"key": "value"}\n'
        if sys.platform != "win32":
            assert oct(target.stat().st_mode & 0o777) == "0o600"

    def test_cleans_up_on_rename_failure(self, tmp_path):
        target = tmp_path / "test.json"
        with patch("os.replace", side_effect=OSError("rename failed")):
            try:
                _atomic_write(target, "data")
            except OSError:
                pass
        temps = list(tmp_path.glob("*.tmp"))
        assert len(temps) == 0
        assert not target.exists()


def test_team_config_default_empty_dict(tmp_path: Path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"projects": {}}))
    config = load_config(p)
    assert config.teams == {}


def test_load_config_teams(tmp_path: Path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({
        "projects": {},
        "teams": {
            "acme": {
                "path": "/home/user/acme",
                "group_chat_id": -1001234567890,
                "bots": {
                    "manager": {"telegram_bot_token": "t1", "active_persona": "software_manager"},
                    "dev":     {"telegram_bot_token": "t2", "active_persona": "software_dev"},
                },
            }
        },
    }))
    config = load_config(p)
    team = config.teams["acme"]
    assert team.path == "/home/user/acme"
    assert team.group_chat_id == -1001234567890
    assert team.bots["manager"].telegram_bot_token == "t1"
    assert team.bots["manager"].active_persona == "software_manager"
    assert team.bots["dev"].telegram_bot_token == "t2"


def test_save_and_load_team(tmp_path: Path):
    p = tmp_path / "cfg.json"
    cfg = Config(
        teams={
            "acme": TeamConfig(
                path="/home/user/acme",
                group_chat_id=-1001234567890,
                bots={
                    "manager": TeamBotConfig(telegram_bot_token="t1", active_persona="developer"),
                    "dev": TeamBotConfig(telegram_bot_token="t2", active_persona="tester"),
                },
            )
        }
    )
    save_config(cfg, p)
    loaded = load_config(p)
    team = loaded.teams["acme"]
    assert team.path == "/home/user/acme"
    assert team.group_chat_id == -1001234567890
    assert team.bots["manager"].telegram_bot_token == "t1"
    assert team.bots["dev"].active_persona == "tester"


def test_teams_coexist_with_projects(tmp_path: Path):
    p = tmp_path / "cfg.json"
    cfg = Config(
        projects={"solo": ProjectConfig(path="/a", telegram_bot_token="tx")},
        teams={
            "acme": TeamConfig(
                path="/b",
                group_chat_id=-100,
                bots={"manager": TeamBotConfig(telegram_bot_token="t1")},
            )
        },
    )
    save_config(cfg, p)
    loaded = load_config(p)
    assert "solo" in loaded.projects
    assert "acme" in loaded.teams


def test_save_config_removes_deleted_teams(tmp_path: Path):
    """Saving a config where a team was dropped from config.teams removes it from the JSON too."""
    p = tmp_path / "cfg.json"
    # First save: two teams
    cfg = Config(
        teams={
            "acme": TeamConfig(
                path="/a", group_chat_id=-100,
                bots={"manager": TeamBotConfig(telegram_bot_token="t1")},
            ),
            "beta": TeamConfig(
                path="/b", group_chat_id=-200,
                bots={"manager": TeamBotConfig(telegram_bot_token="t2")},
            ),
        }
    )
    save_config(cfg, p)
    # Second save: drop "beta"
    cfg.teams.pop("beta")
    save_config(cfg, p)
    # Reload: only "acme" should remain
    loaded = load_config(p)
    assert "acme" in loaded.teams
    assert "beta" not in loaded.teams


def test_patch_team_creates_entry(tmp_path: Path):
    p = tmp_path / "cfg.json"
    patch_team(
        "acme",
        {
            "path": "/home/user/acme",
            "group_chat_id": -1001,
            "bots": {"manager": {"telegram_bot_token": "t1"}},
        },
        p,
    )
    raw = json.loads(p.read_text())
    assert raw["teams"]["acme"]["path"] == "/home/user/acme"
    assert raw["teams"]["acme"]["group_chat_id"] == -1001
    assert raw["teams"]["acme"]["bots"]["manager"]["telegram_bot_token"] == "t1"


def test_patch_team_replaces_at_top_level(tmp_path: Path):
    p = tmp_path / "cfg.json"
    patch_team("acme", {"path": "/a", "group_chat_id": -1, "bots": {"manager": {"telegram_bot_token": "t1"}}}, p)
    patch_team("acme", {"bots": {"dev": {"telegram_bot_token": "t2"}}}, p)
    raw = json.loads(p.read_text())
    # Entire bots dict replaced; path and group_chat_id preserved from first call
    assert raw["teams"]["acme"]["bots"] == {"dev": {"telegram_bot_token": "t2"}}
    assert raw["teams"]["acme"]["path"] == "/a"


def test_patch_team_none_removes_key(tmp_path: Path):
    p = tmp_path / "cfg.json"
    patch_team("acme", {"path": "/a", "group_chat_id": -1}, p)
    patch_team("acme", {"path": None}, p)
    raw = json.loads(p.read_text())
    assert "path" not in raw["teams"]["acme"]


def test_load_teams_helper(tmp_path: Path):
    p = tmp_path / "cfg.json"
    patch_team(
        "acme",
        {
            "path": "/a",
            "group_chat_id": -1,
            "bots": {"manager": {"telegram_bot_token": "t1", "active_persona": "developer"}},
        },
        p,
    )
    teams = load_teams(p)
    assert teams["acme"].bots["manager"].active_persona == "developer"


def test_load_teams_missing_file_returns_empty(tmp_path: Path):
    assert load_teams(tmp_path / "nope.json") == {}


def test_team_with_sentinel_chat_id_roundtrips(tmp_path: Path):
    """A team with group_chat_id=0 (not yet captured) saves and loads cleanly."""
    p = tmp_path / "cfg.json"
    cfg = Config(
        teams={
            "acme": TeamConfig(
                path="/a",
                group_chat_id=0,
                bots={"manager": TeamBotConfig(telegram_bot_token="t1")},
            )
        }
    )
    save_config(cfg, p)
    loaded = load_config(p)
    assert loaded.teams["acme"].group_chat_id == 0


def test_team_bot_autostart_defaults_false(tmp_path: Path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({
        "projects": {},
        "teams": {
            "acme": {
                "path": "/a",
                "group_chat_id": -1,
                "bots": {"manager": {"telegram_bot_token": "t1"}},
            }
        },
    }))
    loaded = load_config(p)
    assert loaded.teams["acme"].bots["manager"].autostart is False


def test_team_bot_autostart_roundtrip(tmp_path: Path):
    p = tmp_path / "cfg.json"
    cfg = Config(
        teams={
            "acme": TeamConfig(
                path="/a",
                group_chat_id=-1,
                bots={
                    "manager": TeamBotConfig(telegram_bot_token="t1", autostart=True),
                    "dev": TeamBotConfig(telegram_bot_token="t2", autostart=False),
                },
            )
        }
    )
    save_config(cfg, p)
    loaded = load_config(p)
    assert loaded.teams["acme"].bots["manager"].autostart is True
    assert loaded.teams["acme"].bots["dev"].autostart is False
    # Default-false autostart should not be written to JSON to keep files clean
    raw = json.loads(p.read_text())
    assert "autostart" not in raw["teams"]["acme"]["bots"]["dev"]
    assert raw["teams"]["acme"]["bots"]["manager"]["autostart"] is True


def test_load_teams_reads_autostart(tmp_path: Path):
    p = tmp_path / "cfg.json"
    patch_team(
        "acme",
        {
            "path": "/a",
            "group_chat_id": -1,
            "bots": {"manager": {"telegram_bot_token": "t1", "autostart": True}},
        },
        p,
    )
    teams = load_teams(p)
    assert teams["acme"].bots["manager"].autostart is True


def test_project_show_thinking_roundtrip(tmp_path: Path):
    p = tmp_path / "cfg.json"
    cfg = Config(
        projects={
            "proj": ProjectConfig(
                path="/x",
                telegram_bot_token="T",
                show_thinking=True,
            )
        }
    )
    save_config(cfg, p)
    loaded = load_config(p)
    assert loaded.projects["proj"].show_thinking is True


def test_project_show_thinking_defaults_false(tmp_path: Path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({
        "projects": {"proj": {"path": "/x", "telegram_bot_token": "T"}}
    }))
    loaded = load_config(p)
    assert loaded.projects["proj"].show_thinking is False


def test_save_does_not_emit_legacy_top_level_claude_keys(tmp_path: Path):
    """Phase 2 of the backend abstraction dual-wrote model/effort/permissions/
    session_id/show_thinking at the project entry top level alongside
    backend_state["claude"]. The mirror was kept one release for downgrade
    safety; v1.0.0 dropped it. Save must emit ONLY the canonical nested shape.
    """
    p = tmp_path / "cfg.json"
    cfg = Config(
        projects={
            "proj": ProjectConfig(
                path="/x",
                telegram_bot_token="T",
                backend_state={
                    "claude": {
                        "model": "opus",
                        "effort": "high",
                        "permissions": "plan",
                        "session_id": "sess-1",
                        "show_thinking": True,
                    }
                },
            )
        },
        default_model_claude="sonnet",
    )
    save_config(cfg, p)
    raw = json.loads(p.read_text())

    state = raw["projects"]["proj"]["backend_state"]["claude"]
    assert state == {
        "model": "opus",
        "effort": "high",
        "permissions": "plan",
        "session_id": "sess-1",
        "show_thinking": True,
    }
    proj_entry = raw["projects"]["proj"]
    for legacy_key in ("model", "effort", "permissions", "session_id", "show_thinking"):
        assert legacy_key not in proj_entry, (
            f"legacy mirror key {legacy_key!r} should not be re-emitted on save"
        )
    assert raw["default_model_claude"] == "sonnet"
    assert "default_model" not in raw


def test_save_strips_lingering_pre_v1_legacy_keys(tmp_path: Path):
    """A pre-v1.0 on-disk config carrying both shapes (because v0.16 dual-wrote
    the legacy mirror) must end up with ONLY the canonical shape on first
    save after upgrade. The top-level legacy keys are stripped, the nested
    backend_state["claude"] entry is preserved unchanged.
    """
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({
        "default_model": "sonnet",
        "default_model_claude": "sonnet",
        "projects": {
            "demo": {
                "path": "/p",
                "telegram_bot_token": "T",
                "model": "opus",
                "permissions": "plan",
                "session_id": "sess-1",
                "show_thinking": True,
                "backend": "claude",
                "backend_state": {
                    "claude": {
                        "model": "opus",
                        "permissions": "plan",
                        "session_id": "sess-1",
                        "show_thinking": True,
                    }
                },
            }
        },
    }))
    loaded = load_config(p)
    save_config(loaded, p)
    raw = json.loads(p.read_text())

    proj_entry = raw["projects"]["demo"]
    for legacy_key in ("model", "effort", "permissions", "session_id", "show_thinking"):
        assert legacy_key not in proj_entry
    assert proj_entry["backend_state"]["claude"]["model"] == "opus"
    assert proj_entry["backend_state"]["claude"]["session_id"] == "sess-1"
    assert "default_model" not in raw
    assert raw["default_model_claude"] == "sonnet"


def test_load_config_skips_phantom_project_without_path(tmp_path: Path, caplog):
    """Pre-34b8dc5 buggy /persona on team bots created projects[<team>_<role>]
    entries with only active_persona (no path). load_config must tolerate them
    so the bot still starts; save_config then filters them out on next write."""
    import logging
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({
        "projects": {
            "good": {"path": "/x", "telegram_bot_token": "T"},
            "acme_manager": {"active_persona": "software_manager"},
        },
    }))
    with caplog.at_level(logging.WARNING, logger="link_project_to_chat.config"):
        loaded = load_config(p)
    assert "good" in loaded.projects
    assert "acme_manager" not in loaded.projects
    assert "acme_manager" in caplog.text


def test_load_config_self_heals_phantom_project_without_path(tmp_path: Path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({
        "projects": {
            "good": {"path": "/x", "telegram_bot_token": "T"},
            "acme_manager": {"active_persona": "software_manager"},
        },
        "future_key": "kept",
    }))
    load_config(p)
    raw = json.loads(p.read_text())
    assert raw["projects"] == {"good": {"path": "/x", "telegram_bot_token": "T"}}
    assert raw["future_key"] == "kept"


def test_load_config_team_missing_path_skipped_with_warning(tmp_path: Path, caplog):
    """A team missing 'path' is skipped with a warning (was: raised ConfigError).

    Strict-fail made one stray phantom entry take down the whole manager
    service; skip-and-warn matches the project-side leniency precedent.
    """
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({
        "teams": {
            "acme": {
                "group_chat_id": -1,
                "bots": {"manager": {"active_persona": "developer"}},
            }
        },
    }))
    with caplog.at_level("WARNING"):
        cfg = load_config(p)
    assert "acme" not in cfg.teams
    assert any(
        "acme" in r.message and "path" in r.message for r in caplog.records
    )


def test_load_config_team_missing_group_chat_id_skipped_with_warning(
    tmp_path: Path, caplog
):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({
        "teams": {
            "beta": {
                "path": "/some/path",
                "bots": {"manager": {"active_persona": "developer"}},
            }
        },
    }))
    with caplog.at_level("WARNING"):
        cfg = load_config(p)
    assert "beta" not in cfg.teams
    assert any(
        "beta" in r.message and "group_chat_id" in r.message
        for r in caplog.records
    )


def test_load_config_team_missing_everything_skipped_with_warning(
    tmp_path: Path, caplog
):
    """A stub team entry (only bots) is skipped, naming all missing fields."""
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({
        "teams": {
            "acme": {"bots": {"manager": {"active_persona": "developer"}}},
        },
    }))
    with caplog.at_level("WARNING"):
        cfg = load_config(p)
    assert "acme" not in cfg.teams
    record = next(r for r in caplog.records if "acme" in r.message)
    assert "path" in record.message
    assert "group_chat_id" in record.message


def test_save_config_drops_phantom_project_on_next_write(tmp_path: Path):
    """After a tolerant load, saving the config removes the phantom entry from disk."""
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({
        "projects": {
            "good": {"path": "/x", "telegram_bot_token": "T"},
            "acme_manager": {"active_persona": "software_manager"},
        },
    }))
    cfg = load_config(p)
    save_config(cfg, p)
    raw = json.loads(p.read_text())
    assert "acme_manager" not in raw["projects"]
    assert "good" in raw["projects"]


def test_team_config_safety_defaults():
    from link_project_to_chat.config import TeamConfig

    team = TeamConfig(path="/tmp/project")
    assert team.max_autonomous_turns == 5
    assert team.safety_mode == "strict"


def test_team_config_load_save_preserves_safety_fields(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "teams": {
            "lpct": {
                "path": "/tmp/lpct",
                "group_chat_id": -1001,
                "max_autonomous_turns": 3,
                "safety_mode": "strict",
                "bots": {},
            }
        }
    }))

    cfg = load_config(cfg_path)
    assert cfg.teams["lpct"].max_autonomous_turns == 3
    assert cfg.teams["lpct"].safety_mode == "strict"

    save_config(cfg, cfg_path)
    raw = json.loads(cfg_path.read_text())
    assert raw["teams"]["lpct"]["max_autonomous_turns"] == 3
    assert raw["teams"]["lpct"]["safety_mode"] == "strict"


def test_google_chat_project_override_defaults_all_optional_except_port():
    from link_project_to_chat.config import GoogleChatProjectOverride

    override = GoogleChatProjectOverride(port=8091)

    # port is required; everything else defaults to None / unset
    assert override.port == 8091
    assert override.service_account_file is None
    assert override.public_url is None
    assert override.root_command_id is None
    assert override.project_number is None
    assert override.auth_audience_type is None
    assert override.host is None
    assert override.callback_token_ttl_seconds is None
    assert override.pending_prompt_ttl_seconds is None
    assert override.max_message_bytes is None
    assert override.attachment_max_bytes is None
    assert override.endpoint_path is None
    assert override.allowed_audiences is None
    assert override.app_id is None
    assert override.root_command_name is None


def test_google_chat_project_override_port_must_be_in_range():
    from link_project_to_chat.config import ConfigError, GoogleChatProjectOverride

    with pytest.raises(ConfigError):
        GoogleChatProjectOverride(port=0).validate()
    with pytest.raises(ConfigError):
        GoogleChatProjectOverride(port=70_000).validate()


def test_google_chat_project_override_validate_accepts_valid_port():
    from link_project_to_chat.config import GoogleChatProjectOverride

    GoogleChatProjectOverride(port=8091).validate()  # no exception


def test_google_chat_project_override_validate_rejects_invalid_audience_type():
    from link_project_to_chat.config import ConfigError, GoogleChatProjectOverride

    with pytest.raises(ConfigError, match="auth_audience_type"):
        GoogleChatProjectOverride(port=8091, auth_audience_type="invalid").validate()


def test_parse_google_chat_override_minimal():
    from link_project_to_chat.config import _parse_google_chat_override

    override = _parse_google_chat_override({"port": 8091})
    assert override.port == 8091
    assert override.service_account_file is None


def test_parse_google_chat_override_full():
    from link_project_to_chat.config import _parse_google_chat_override

    raw = {
        "port": 8092,
        "service_account_file": "/keys/proj.json",
        "public_url": "https://proj.example.com",
        "root_command_id": 7,
        "project_number": "12345",
    }
    override = _parse_google_chat_override(raw)
    assert override.port == 8092
    assert override.service_account_file == "/keys/proj.json"
    assert override.public_url == "https://proj.example.com"
    assert override.root_command_id == 7
    assert override.project_number == "12345"


def test_parse_google_chat_override_missing_port_raises():
    from link_project_to_chat.config import ConfigError, _parse_google_chat_override

    with pytest.raises(ConfigError, match="port"):
        _parse_google_chat_override({"public_url": "https://x.test"})


def test_serialize_google_chat_override_omits_none_fields():
    from link_project_to_chat.config import (
        GoogleChatProjectOverride,
        _serialize_google_chat_override,
    )

    raw = _serialize_google_chat_override(
        GoogleChatProjectOverride(port=8091, public_url="https://x.test")
    )
    assert raw == {"port": 8091, "public_url": "https://x.test"}
    assert "service_account_file" not in raw  # None fields stripped


def test_google_chat_override_round_trip():
    from link_project_to_chat.config import (
        GoogleChatProjectOverride,
        _parse_google_chat_override,
        _serialize_google_chat_override,
    )

    original = GoogleChatProjectOverride(
        port=8091,
        service_account_file="/keys/a.json",
        public_url="https://a.example",
        root_command_id=3,
    )
    raw = _serialize_google_chat_override(original)
    reparsed = _parse_google_chat_override(raw)
    assert reparsed == original


def test_parse_google_chat_override_rejects_non_list_allowed_audiences():
    from link_project_to_chat.config import ConfigError, _parse_google_chat_override

    with pytest.raises(ConfigError, match="allowed_audiences"):
        _parse_google_chat_override({"port": 8091, "allowed_audiences": "not-a-list"})


def test_parse_google_chat_override_rejects_invalid_audience_type():
    from link_project_to_chat.config import ConfigError, _parse_google_chat_override

    with pytest.raises(ConfigError, match="auth_audience_type"):
        _parse_google_chat_override({"port": 8091, "auth_audience_type": "bogus"})


def test_project_config_google_chat_defaults_none():
    from link_project_to_chat.config import ProjectConfig

    pc = ProjectConfig(path="/p", telegram_bot_token="")
    assert pc.google_chat is None


def test_project_config_round_trips_google_chat_override(tmp_path):
    import json
    from link_project_to_chat.config import (
        GoogleChatProjectOverride,
        load_config,
        save_config,
    )

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "projects": {
            "alpha": {
                "path": "/p",
                "telegram_bot_token": "",
                "google_chat": {
                    "port": 8091,
                    "service_account_file": "/keys/a.json",
                    "public_url": "https://a.example",
                    "root_command_id": 5,
                },
            }
        }
    }))

    loaded = load_config(cfg_path)
    alpha = loaded.projects["alpha"]
    assert alpha.google_chat == GoogleChatProjectOverride(
        port=8091,
        service_account_file="/keys/a.json",
        public_url="https://a.example",
        root_command_id=5,
    )

    save_config(loaded, cfg_path)
    raw = json.loads(cfg_path.read_text())
    assert raw["projects"]["alpha"]["google_chat"] == {
        "port": 8091,
        "service_account_file": "/keys/a.json",
        "public_url": "https://a.example",
        "root_command_id": 5,
    }


def test_project_config_load_rejects_invalid_google_chat_block(tmp_path):
    import json
    from link_project_to_chat.config import ConfigError, load_config

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "projects": {"alpha": {"path": "/p", "telegram_bot_token": "", "google_chat": {}}}
    }))

    with pytest.raises(ConfigError, match="port"):
        load_config(cfg_path)


def test_project_config_warns_on_non_dict_google_chat(tmp_path, caplog):
    import json
    import logging
    from link_project_to_chat.config import load_config

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "projects": {
            "alpha": {
                "path": "/p",
                "telegram_bot_token": "",
                "google_chat": "not-a-dict",
            }
        }
    }))

    with caplog.at_level(logging.WARNING):
        loaded = load_config(cfg_path)

    assert loaded.projects["alpha"].google_chat is None
    assert any("google_chat" in rec.getMessage().lower() for rec in caplog.records)


def test_migration_auto_claims_for_single_project(tmp_path):
    import json
    from link_project_to_chat.config import load_config, save_config

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "projects": {
            "solo": {"path": "/p", "telegram_bot_token": ""},
        },
        "google_chat": {
            "service_account_file": "/keys/sa.json",
            "public_url": "https://x.example",
            "port": 8090,
            "root_command_id": 1,
        },
    }))

    loaded = load_config(cfg_path)
    assert loaded.projects["solo"].google_chat is not None
    assert loaded.projects["solo"].google_chat.port == 8090

    save_config(loaded, cfg_path)
    raw = json.loads(cfg_path.read_text())
    assert "google_chat" in raw["projects"]["solo"]


def test_migration_idempotent(tmp_path):
    import json
    from link_project_to_chat.config import load_config, save_config

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "projects": {"solo": {"path": "/p", "telegram_bot_token": ""}},
        "google_chat": {
            "service_account_file": "/keys/sa.json",
            "public_url": "https://x.example",
            "port": 8090,
            "root_command_id": 1,
        },
    }))

    # First load → migration auto-claims the single project with port=8090.
    loaded = load_config(cfg_path)
    assert loaded.projects["solo"].google_chat is not None
    assert loaded.projects["solo"].google_chat.port == 8090

    # Operator subsequently bumps the override's port via the wizard (Task 12).
    # Save with the bumped port to simulate that.
    loaded.projects["solo"].google_chat.port = 9100
    save_config(loaded, cfg_path)

    # Re-load: migration MUST NOT fire again. The bumped port must survive.
    reloaded = load_config(cfg_path)
    assert reloaded.projects["solo"].google_chat is not None
    assert reloaded.projects["solo"].google_chat.port == 9100, (
        "migration re-fired and overwrote the operator's port choice"
    )

    # And the raw JSON must reflect the bumped port, not the original 8090.
    save_config(reloaded, cfg_path)
    raw = json.loads(cfg_path.read_text())
    assert raw["projects"]["solo"]["google_chat"]["port"] == 9100


def test_migration_skips_when_multiple_projects_no_overrides(tmp_path):
    import json
    from link_project_to_chat.config import load_config

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "projects": {
            "a": {"path": "/a", "telegram_bot_token": ""},
            "b": {"path": "/b", "telegram_bot_token": ""},
        },
        "google_chat": {
            "service_account_file": "/keys/sa.json",
            "public_url": "https://x.example",
            "port": 8090,
            "root_command_id": 1,
        },
    }))

    loaded = load_config(cfg_path)
    # Ambiguous which project to claim — migration MUST NOT guess.
    assert loaded.projects["a"].google_chat is None
    assert loaded.projects["b"].google_chat is None
    # The top-level block is preserved, the operator will use the wizard to claim.
    assert loaded.google_chat.port == 8090


def test_gitlab_pat_round_trips(tmp_path: Path):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"gitlab_pat": "glpat-secret"}))
    config = load_config(cfg_file)
    assert config.gitlab_pat == "glpat-secret"
    save_config(config, cfg_file)
    raw = json.loads(cfg_file.read_text())
    assert raw["gitlab_pat"] == "glpat-secret"


def test_gitlab_host_defaults_to_gitlab_com(tmp_path: Path):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({}))
    config = load_config(cfg_file)
    assert config.gitlab_host == "gitlab.com"


def test_gitlab_host_default_is_omitted_from_saved_json(tmp_path: Path):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({}))
    config = load_config(cfg_file)
    save_config(config, cfg_file)
    raw = json.loads(cfg_file.read_text())
    assert "gitlab_host" not in raw


def test_gitlab_host_custom_is_persisted(tmp_path: Path):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"gitlab_host": "gitlab.example.com"}))
    config = load_config(cfg_file)
    assert config.gitlab_host == "gitlab.example.com"
    save_config(config, cfg_file)
    raw = json.loads(cfg_file.read_text())
    assert raw["gitlab_host"] == "gitlab.example.com"


def test_empty_gitlab_pat_is_omitted_from_saved_json(tmp_path: Path):
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({}))
    config = load_config(cfg_file)
    assert config.gitlab_pat == ""
    save_config(config, cfg_file)
    raw = json.loads(cfg_file.read_text())
    assert "gitlab_pat" not in raw
