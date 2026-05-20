from __future__ import annotations

from pathlib import Path


def test_persona_keyboard_lists_discovered_personas(tmp_path):
    from link_project_to_chat.manager.bot import _build_persona_keyboard

    # Create fake personas (path layout matches what load_personas() expects)
    personas_dir = tmp_path / ".claude" / "personas"
    personas_dir.mkdir(parents=True)
    (personas_dir / "developer.md").write_text("# Developer")
    (personas_dir / "tester.md").write_text("# Tester")

    kb = _build_persona_keyboard(tmp_path, callback_prefix="team_persona_mgr")
    buttons = [btn for row in kb.rows for btn in row]
    labels = {btn.label for btn in buttons}
    # Assert at LEAST our two test personas appear (load_personas may also discover globals)
    assert "developer" in labels
    assert "tester" in labels
    # Callbacks are prefixed
    for btn in buttons:
        assert btn.value.startswith("team_persona_mgr:")


import pytest

from link_project_to_chat.config import (
    Config,
    ProjectConfig,
    TeamBotConfig,
    TeamConfig,
    save_config,
)


def test_preflight_rejects_existing_team_name(tmp_path):
    from link_project_to_chat.manager.bot import _create_team_preflight

    cfg_path = tmp_path / "config.json"
    config = Config(
        telegram_api_id=1,
        telegram_api_hash="x",
        github_pat="ghp_x",
        teams={
            "acme": TeamConfig(path="/a", group_chat_id=-1, bots={})
        },
    )
    save_config(config, cfg_path)
    (cfg_path.parent / "telethon.session").write_text("x")  # fake session file

    err = _create_team_preflight(cfg_path, "acme")
    assert err is not None
    assert "already configured" in err


def test_preflight_rejects_legacy_project_name_collision(tmp_path):
    from link_project_to_chat.manager.bot import _create_team_preflight

    cfg_path = tmp_path / "config.json"
    config = Config(
        telegram_api_id=1,
        telegram_api_hash="x",
        github_pat="ghp_x",
        projects={"acme_mgr": ProjectConfig(path="/a", telegram_bot_token="t")},
    )
    save_config(config, cfg_path)
    (cfg_path.parent / "telethon.session").write_text("x")

    err = _create_team_preflight(cfg_path, "acme")
    assert err is not None
    assert "project names are taken" in err or "acme_mgr" in err


def test_preflight_rejects_missing_telethon_config(tmp_path):
    from link_project_to_chat.manager.bot import _create_team_preflight

    cfg_path = tmp_path / "config.json"
    config = Config(github_pat="ghp_x")  # telegram_api_id/hash missing
    save_config(config, cfg_path)

    err = _create_team_preflight(cfg_path, "acme")
    assert err is not None
    assert "/setup" in err


def test_preflight_passes_when_all_good(tmp_path):
    from link_project_to_chat.manager.bot import _create_team_preflight

    cfg_path = tmp_path / "config.json"
    config = Config(telegram_api_id=1, telegram_api_hash="x", github_pat="ghp_x")
    save_config(config, cfg_path)
    (cfg_path.parent / "telethon.session").write_text("x")

    err = _create_team_preflight(cfg_path, "acme")
    assert err is None


def test_preflight_passes_with_gitlab_only_auth(tmp_path, monkeypatch):
    from link_project_to_chat.manager.bot import _create_team_preflight

    monkeypatch.setattr("link_project_to_chat.github_client._gh_available", lambda: False)
    cfg_path = tmp_path / "config.json"
    config = Config(
        telegram_api_id=1,
        telegram_api_hash="x",
        gitlab_pat="glpat-x",
        gitlab_host="gitlab.com",
    )
    save_config(config, cfg_path)
    (cfg_path.parent / "telethon.session").write_text("x")

    err = _create_team_preflight(cfg_path, "acme")
    assert err is None


def test_preflight_rejects_prefix_over_22_chars(tmp_path):
    """Prefix + '_{role}_{N}_bot' must fit Telegram's 32-char username cap.

    v1.0+: the suffix dropped from '_claude_bot' to backend-agnostic '_bot',
    freeing 7 chars of prefix budget (15 → 22).
    """
    from link_project_to_chat.manager.bot import _create_team_preflight

    cfg_path = tmp_path / "config.json"
    save_config(Config(telegram_api_id=1, telegram_api_hash="x", github_pat="ghp_x"), cfg_path)
    (cfg_path.parent / "telethon.session").write_text("x")

    err = _create_team_preflight(cfg_path, "a_very_long_team_prefix_xx")  # 26 chars
    assert err is not None
    assert "too long" in err
    assert "a_very_long_team_prefix_xx" in err


def test_preflight_accepts_22_char_prefix(tmp_path):
    from link_project_to_chat.manager.bot import _create_team_preflight

    cfg_path = tmp_path / "config.json"
    save_config(Config(telegram_api_id=1, telegram_api_hash="x", github_pat="ghp_x"), cfg_path)
    (cfg_path.parent / "telethon.session").write_text("x")

    # Exactly 22 chars — should pass.
    err = _create_team_preflight(cfg_path, "abcdefghijklmnopqrstuv")
    assert err is None


from unittest.mock import AsyncMock, MagicMock


@pytest.mark.asyncio
async def test_show_repo_page_supports_user_data_key(tmp_path, monkeypatch):
    """_show_repo_page must read/write to the key passed in, not hardcoded 'create'."""
    from link_project_to_chat.manager.bot import ManagerBot
    from link_project_to_chat.manager.process import ProcessManager
    from link_project_to_chat.transport import ChatKind, ChatRef, MessageRef
    from link_project_to_chat.transport.fake import FakeTransport

    cfg_path = tmp_path / "config.json"
    save_config(Config(telegram_api_id=1, telegram_api_hash="x", github_pat="ghp_x"), cfg_path)
    (cfg_path.parent / "telethon.session").write_text("x")

    mb = ManagerBot(
        token="t",
        process_manager=ProcessManager(project_config_path=cfg_path),
        project_config_path=cfg_path,
    )
    mb._transport = FakeTransport()
    ctx = MagicMock()
    ctx.user_data = {"create_team": {"config_path": str(cfg_path)}}
    chat = ChatRef(transport_id="fake", native_id="1", kind=ChatKind.DM)
    msg_ref = MessageRef(transport_id="fake", native_id="1", chat=chat)

    # Monkeypatch GitHubClient.list_repos to avoid network
    from link_project_to_chat.repo_provider import RepoInfo

    async def fake_list_repos(self, *a, **kw):
        repo = RepoInfo(
            name="acme",
            full_name="me/acme",
            description="example",
            private=False,
            html_url="https://github.com/me/acme",
            clone_url="https://github.com/me/acme.git",
        )
        return [repo], False

    monkeypatch.setattr(
        "link_project_to_chat.github_client.GitHubClient.list_repos", fake_list_repos
    )

    await mb._show_repo_page(msg_ref, ctx, page=1, user_data_key="create_team")
    # Assert the repos landed in ctx.user_data["create_team"], not ["create"]
    assert "repos" in ctx.user_data["create_team"]
    repos = ctx.user_data["create_team"]["repos"]
    assert any(r.get("full_name") == "me/acme" for r in repos)


@pytest.mark.asyncio
async def test_create_bot_with_retry_tries_suffixes(monkeypatch):
    from link_project_to_chat.manager.bot import _create_bot_with_retry

    attempts = []

    async def fake_create_bot(display_name: str, username: str) -> str:
        attempts.append(username)
        if len(attempts) < 3:
            raise ValueError("username taken")
        return "FAKE_TOKEN"

    bfc = MagicMock()
    bfc.create_bot = fake_create_bot

    token, username = await _create_bot_with_retry(bfc, "Acme Manager", "acme_mgr_bot")
    assert token == "FAKE_TOKEN"
    assert username == "acme_mgr_2_bot"
    assert attempts == ["acme_mgr_bot", "acme_mgr_1_bot", "acme_mgr_2_bot"]


@pytest.mark.asyncio
async def test_create_bot_with_retry_fails_after_5_tries():
    from link_project_to_chat.manager.bot import _create_bot_with_retry

    async def fake_create_bot(display_name: str, username: str) -> str:
        raise ValueError("username taken")

    bfc = MagicMock()
    bfc.create_bot = fake_create_bot

    with pytest.raises(RuntimeError, match="5 attempts"):
        await _create_bot_with_retry(bfc, "Acme Manager", "acme_mgr_claude_bot")


@pytest.mark.asyncio
async def test_create_bot_with_retry_backs_off_on_rate_limit(monkeypatch):
    """Rate-limit should sleep + retry the SAME candidate (no suffix bump)."""
    from link_project_to_chat.botfather import BotFatherRateLimit
    from link_project_to_chat.manager.bot import _create_bot_with_retry

    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr("asyncio.sleep", fake_sleep)

    attempts = []

    async def fake_create_bot(display_name: str, username: str) -> str:
        attempts.append(username)
        if len(attempts) == 1:
            raise BotFatherRateLimit("throttled", retry_after=8.0)
        return "FAKE_TOKEN"

    bfc = MagicMock()
    bfc.create_bot = fake_create_bot

    token, username = await _create_bot_with_retry(bfc, "Acme Manager", "acme_mgr_claude_bot")
    assert token == "FAKE_TOKEN"
    # Same candidate on both calls — no suffix bump after rate-limit.
    assert attempts == ["acme_mgr_claude_bot", "acme_mgr_claude_bot"]
    # Slept at least the hinted retry_after.
    assert sleeps and sleeps[0] >= 8.0


def _dm_chat_ref(chat_id: int = 1):
    from link_project_to_chat.transport import ChatKind, ChatRef
    return ChatRef(transport_id="fake", native_id=str(chat_id), kind=ChatKind.DM)


@pytest.mark.asyncio
async def test_delete_team_execute_happy_path(tmp_path, monkeypatch):
    """Stops bots, deletes bots via BotFather, deletes group, rm -rf folder, removes config entry."""
    from unittest.mock import patch
    from link_project_to_chat.manager.bot import ManagerBot
    from link_project_to_chat.config import (
        Config, TeamBotConfig, TeamConfig, save_config, load_config,
    )
    from link_project_to_chat.transport.fake import FakeTransport

    # Set up on-disk config with one team + project folder.
    cfg_path = tmp_path / "config.json"
    proj_dir = tmp_path / "acme"
    proj_dir.mkdir()
    save_config(
        Config(
            telegram_api_id=1, telegram_api_hash="x", github_pat="ghp_x",
            teams={
                "acme": TeamConfig(
                    path=str(proj_dir), group_chat_id=-100_111,
                    bots={
                        "manager": TeamBotConfig(
                            telegram_bot_token="t1",
                            bot_username="acme_mgr_claude_bot",
                        ),
                        "dev": TeamBotConfig(
                            telegram_bot_token="t2",
                            bot_username="acme_dev_claude_bot",
                        ),
                    },
                )
            },
        ),
        cfg_path,
    )
    (cfg_path.parent / "telethon.session").write_text("x")

    mb = ManagerBot.__new__(ManagerBot)
    mb._project_config_path = cfg_path
    mb._transport = FakeTransport()
    mb._pm = MagicMock()
    mb._pm.stop = MagicMock()
    mb._telethon_client = MagicMock()

    # Patch the heavy bits.
    with patch(
        "link_project_to_chat.botfather.BotFatherClient.delete_bot",
        new=AsyncMock(),
    ) as mock_delete_bot, patch(
        "link_project_to_chat.transport._telegram_group.delete_supergroup",
        new=AsyncMock(),
    ) as mock_delete_group:
        await mb._delete_team_execute(_dm_chat_ref(), target="acme")

    # Both bots stopped.
    assert mb._pm.stop.call_count == 2
    # Both bots deleted via BotFather.
    assert mock_delete_bot.await_count == 2
    # Supergroup deleted.
    assert mock_delete_group.await_count == 1
    # Project folder removed.
    assert not proj_dir.exists()
    # Team entry gone from config.
    assert "acme" not in load_config(cfg_path).teams
    # Success message sent.
    sent_texts = [m.text for m in mb._transport.sent_messages]
    assert any("fully deleted" in t for t in sent_texts)


@pytest.mark.asyncio
async def test_delete_team_execute_unknown_target_is_noop(tmp_path):
    from link_project_to_chat.manager.bot import ManagerBot
    from link_project_to_chat.config import Config, save_config
    from link_project_to_chat.transport.fake import FakeTransport

    cfg_path = tmp_path / "config.json"
    save_config(Config(), cfg_path)

    mb = ManagerBot.__new__(ManagerBot)
    mb._project_config_path = cfg_path
    mb._transport = FakeTransport()

    await mb._delete_team_execute(_dm_chat_ref(), target="ghost")
    # Should send one message and not raise.
    assert len(mb._transport.sent_messages) == 1
    assert "not found" in mb._transport.sent_messages[0].text


@pytest.mark.asyncio
async def test_delete_team_execute_continues_on_individual_failures(tmp_path, monkeypatch):
    """If BotFather delete fails for one bot, the rest of the cleanup still runs."""
    from unittest.mock import patch
    from link_project_to_chat.manager.bot import ManagerBot
    from link_project_to_chat.config import (
        Config, TeamBotConfig, TeamConfig, save_config, load_config,
    )
    from link_project_to_chat.transport.fake import FakeTransport

    cfg_path = tmp_path / "config.json"
    proj_dir = tmp_path / "acme"
    proj_dir.mkdir()
    save_config(
        Config(
            telegram_api_id=1, telegram_api_hash="x", github_pat="ghp_x",
            teams={
                "acme": TeamConfig(
                    path=str(proj_dir), group_chat_id=-100_111,
                    bots={
                        "manager": TeamBotConfig(telegram_bot_token="t1", bot_username="mgr_bot"),
                        "dev": TeamBotConfig(telegram_bot_token="t2", bot_username="dev_bot"),
                    },
                )
            },
        ),
        cfg_path,
    )
    (cfg_path.parent / "telethon.session").write_text("x")

    mb = ManagerBot.__new__(ManagerBot)
    mb._project_config_path = cfg_path
    mb._transport = FakeTransport()
    mb._pm = MagicMock()
    mb._telethon_client = MagicMock()

    async def sometimes_fail(username):
        if username == "mgr_bot":
            raise RuntimeError("BotFather said no")

    with patch(
        "link_project_to_chat.botfather.BotFatherClient.delete_bot",
        new=AsyncMock(side_effect=sometimes_fail),
    ), patch(
        "link_project_to_chat.transport._telegram_group.delete_supergroup",
        new=AsyncMock(),
    ):
        await mb._delete_team_execute(_dm_chat_ref(), target="acme")

    # Team still removed from config, folder still gone, but issues reported.
    assert "acme" not in load_config(cfg_path).teams
    assert not proj_dir.exists()
    sent_texts = [m.text for m in mb._transport.sent_messages]
    assert any("deleted with issues" in t for t in sent_texts)
    assert any("BotFather /deletebot @mgr_bot" in t for t in sent_texts)


@pytest.mark.asyncio
async def test_create_bot_with_retry_gives_up_after_repeated_rate_limits(monkeypatch):
    """Permanent throttle should surface as a RuntimeError, not hang."""
    from link_project_to_chat.botfather import BotFatherRateLimit
    from link_project_to_chat.manager.bot import _create_bot_with_retry

    async def fake_sleep(seconds):
        return None

    monkeypatch.setattr("asyncio.sleep", fake_sleep)

    async def fake_create_bot(display_name: str, username: str) -> str:
        raise BotFatherRateLimit("throttled", retry_after=60.0)

    bfc = MagicMock()
    bfc.create_bot = fake_create_bot

    with pytest.raises(RuntimeError, match="throttled"):
        await _create_bot_with_retry(
            bfc, "Acme Manager", "acme_mgr_claude_bot", max_rate_limit_retries=2,
        )


@pytest.mark.asyncio
async def test_create_bot_with_retry_short_circuits_on_long_flood_wait(monkeypatch):
    """A multi-hour BotFather cooldown must not be waited out inside the
    /create_team callback — we surface the wait to the user instead of
    sleeping. Regression for the 2026-04-20 incident (68436s ≈ 19h).
    """
    from link_project_to_chat.botfather import BotFatherRateLimit
    from link_project_to_chat.manager.bot import _create_bot_with_retry

    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr("asyncio.sleep", fake_sleep)

    async def fake_create_bot(display_name: str, username: str) -> str:
        raise BotFatherRateLimit("flooded", retry_after=68436.0)

    bfc = MagicMock()
    bfc.create_bot = fake_create_bot

    with pytest.raises(RuntimeError, match=r"flood-limited for ~19\."):
        await _create_bot_with_retry(bfc, "Acme Dev", "acme_dev_claude_bot")

    # Must not have slept — the short-circuit path fires before any await sleep.
    assert sleeps == []


def _make_update_for_team_execute(username: str = "alice"):
    """Construct a telegram-like Update payload that _incoming_from_update can consume."""
    update = MagicMock()
    # effective_chat needs .id and .type for chat_ref_from_telegram
    update.effective_chat = MagicMock(id=1)
    update.effective_chat.type = "private"
    # effective_user needs id/full_name/username/is_bot for identity_from_telegram_user
    update.effective_user = MagicMock(id=42, full_name=username, username=username, is_bot=False)
    # effective_message is read for text; force .text to an empty string (we're past command entry)
    msg = MagicMock()
    msg.text = ""
    update.effective_message = msg
    return update


@pytest.mark.asyncio
async def test_create_team_execute_partial_failure_report(tmp_path):
    """When orchestration aborts mid-flight, a partial-failure report is sent."""
    from unittest.mock import patch

    from link_project_to_chat.manager.bot import ManagerBot
    from link_project_to_chat.transport.fake import FakeTransport

    cfg_path = tmp_path / "config.json"
    save_config(
        Config(telegram_api_id=1, telegram_api_hash="x", github_pat="ghp_x"), cfg_path
    )
    (cfg_path.parent / "telethon.session").write_text("x")

    # Bypass __init__ to isolate the orchestrator from auth/process-manager wiring.
    mb = ManagerBot.__new__(ManagerBot)
    mb._project_config_path = cfg_path
    mb._transport = FakeTransport()
    mb._pm = MagicMock()

    update = _make_update_for_team_execute()
    ctx = MagicMock()
    ctx.user_data = {
        "create_team": {
            "project_prefix": "acme",
            "persona_mgr": "developer",
            "persona_dev": "tester",
            "repo": MagicMock(),
        }
    }

    # Force BotFatherClient.create_bot to fail (simulates auth missing / API error).
    with patch(
        "link_project_to_chat.botfather.BotFatherClient.create_bot",
        new=AsyncMock(side_effect=Exception("simulated failure")),
    ):
        result = await mb._create_team_execute(update, ctx)

    from telegram.ext import ConversationHandler

    assert result == ConversationHandler.END
    # The first send_text is the progress status; subsequent send_text calls
    # include the failure report.
    sent_texts = [m.text for m in mb._transport.sent_messages]
    failure_msgs = [t for t in sent_texts if "failed" in t.lower()]
    assert failure_msgs, f"Expected a failure report, got: {sent_texts}"


@pytest.mark.asyncio
async def test_create_team_execute_partial_failure_after_bot1(tmp_path):
    """Verify the failure report includes bot1 details when failure happens AFTER bot1 succeeds."""
    from link_project_to_chat.manager.bot import ManagerBot
    from link_project_to_chat.transport.fake import FakeTransport
    from unittest.mock import AsyncMock, MagicMock, patch

    cfg_path = tmp_path / "config.json"
    save_config(Config(telegram_api_id=1, telegram_api_hash="x", github_pat="ghp_x"), cfg_path)
    (cfg_path.parent / "telethon.session").write_text("x")

    mb = ManagerBot.__new__(ManagerBot)
    mb._project_config_path = cfg_path
    mb._transport = FakeTransport()
    mb._pm = MagicMock()

    update = _make_update_for_team_execute()
    ctx = MagicMock()
    ctx.user_data = {
        "create_team": {
            "project_prefix": "acme",
            "persona_mgr": "developer",
            "persona_dev": "tester",
            "repo": MagicMock(),
        }
    }

    # Bot1 succeeds with FAKE_TOKEN; bot2's first attempt raises.
    call_count = {"n": 0}
    async def fake_create_bot(display_name, username):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return "BOT1_TOKEN"
        raise Exception("simulated bot2 failure")

    with patch(
        "link_project_to_chat.botfather.BotFatherClient.create_bot",
        side_effect=fake_create_bot,
    ):
        result = await mb._create_team_execute(update, ctx)

    from telegram.ext import ConversationHandler
    assert result == ConversationHandler.END
    sent_texts = [m.text for m in mb._transport.sent_messages]
    failure_msg = next((t for t in sent_texts if "failed" in t.lower()), None)
    assert failure_msg is not None
    assert "Bot @" in failure_msg  # bot1 was completed; should appear in cleanup list
    assert "Safe to retry" in failure_msg  # config NOT committed


@pytest.mark.asyncio
async def _DISABLED_test_create_team_execute_adopts_new_telethon_client_for_relay(tmp_path, monkeypatch):
    """DISABLED: tested a main-only behavior (manager adopts Telethon client for relay).
    Feature branch moved relay ownership to project bots (#0c); this behavior no longer exists."""
    import sys
    import types

    from telegram.ext import ConversationHandler
    from link_project_to_chat.manager.bot import ManagerBot

    cfg_path = tmp_path / "config.json"
    save_config(
        Config(telegram_api_id=1, telegram_api_hash="x", github_pat="ghp_x"), cfg_path
    )
    (cfg_path.parent / "telethon.session").write_text("x")

    shared_client = MagicMock()
    disconnect_ownership: list[bool] = []
    started_relays: list[tuple[object, str, int, set[str]]] = []

    class FakeBotFatherClient:
        def __init__(self, api_id, api_hash, session_path, client=None):
            self._client = client
            self._owns_client = client is None

        async def create_bot(self, display_name: str, username: str) -> str:
            return f"TOKEN_FOR_{username}"

        async def disable_privacy(self, username: str) -> None:
            return None

        async def _ensure_client(self):
            if self._client is None:
                self._client = shared_client
            return self._client

        async def disconnect(self) -> None:
            disconnect_ownership.append(self._owns_client)

    class FakeGitHubClient:
        def __init__(self, pat=None):
            self.pat = pat

        async def clone_repo(self, repo, dest):
            dest.mkdir(parents=True, exist_ok=True)

        async def close(self) -> None:
            return None

    class FakeTeamRelay:
        def __init__(self, client, team_name, group_chat_id, bot_usernames):
            self._client = client
            self._team_name = team_name
            self._group_chat_id = group_chat_id
            self._bot_usernames = set(bot_usernames)

        async def start(self) -> None:
            started_relays.append(
                (
                    self._client,
                    self._team_name,
                    self._group_chat_id,
                    self._bot_usernames,
                )
            )

    async def fake_create_supergroup(client, title: str) -> int:
        assert client is shared_client
        return -100_111

    async def fake_add_bot(client, chat_id: int, bot_username: str) -> None:
        return None

    async def fake_promote_admin(client, chat_id: int, bot_username: str) -> None:
        return None

    async def fake_invite_user(client, chat_id: int, username: str) -> None:
        return None

    fake_telegram_group = types.ModuleType("link_project_to_chat.manager.telegram_group")
    fake_telegram_group.create_supergroup = fake_create_supergroup
    fake_telegram_group.add_bot = fake_add_bot
    fake_telegram_group.promote_admin = fake_promote_admin
    fake_telegram_group.invite_user = fake_invite_user

    fake_team_relay = types.ModuleType("link_project_to_chat.manager.team_relay")
    fake_team_relay.TeamRelay = FakeTeamRelay

    monkeypatch.setattr("link_project_to_chat.botfather.BotFatherClient", FakeBotFatherClient)
    monkeypatch.setattr("link_project_to_chat.github_client.GitHubClient", FakeGitHubClient)
    monkeypatch.setitem(sys.modules, "link_project_to_chat.manager.telegram_group", fake_telegram_group)
    monkeypatch.setitem(sys.modules, "link_project_to_chat.manager.team_relay", fake_team_relay)

    mb = ManagerBot.__new__(ManagerBot)
    mb._project_config_path = cfg_path
    mb._app = MagicMock()
    mb._app.bot = MagicMock()
    mb._app.bot.send_message = AsyncMock(
        return_value=MagicMock(edit_text=AsyncMock())
    )
    mb._pm = MagicMock()
    mb._team_relays = {}
    mb._telethon_client = None

    update = MagicMock()
    update.effective_chat = MagicMock(id=1)
    update.effective_user = MagicMock(username="alice")
    ctx = MagicMock()
    ctx.user_data = {
        "create_team": {
            "project_prefix": "acme",
            "persona_mgr": "developer",
            "persona_dev": "tester",
            "repo": MagicMock(),
        }
    }

    result = await mb._create_team_execute(update, ctx)

    assert result == ConversationHandler.END
    assert mb._telethon_client is shared_client
    assert "acme" in mb._team_relays
    assert started_relays == [
        (
            shared_client,
            "acme",
            -100_111,
            {"acme_mgr_claude_bot", "acme_dev_claude_bot"},
        )
    ]
    assert disconnect_ownership == [False]
    calls = {c.args for c in mb._pm.start_team.call_args_list}
    assert calls == {("acme", "manager"), ("acme", "dev")}
