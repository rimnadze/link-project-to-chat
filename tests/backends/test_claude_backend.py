import io
import json
from pathlib import Path

import pytest

from link_project_to_chat.backends.base import BackendCapabilities, HealthStatus
from link_project_to_chat.backends.claude import (
    ClaudeBackend,
    DEFAULT_MODEL,
    _build_telegram_awareness,
    _telegram_command_summary,
)


def _capabilities(**overrides) -> BackendCapabilities:
    defaults = dict(
        models=("alpha", "beta"),
        supports_thinking=True,
        supports_permissions=True,
        supports_resume=True,
        supports_compact=True,
        supports_allowed_tools=True,
        supports_usage_cap_detection=True,
        supports_effort=True,
        effort_levels=("low", "medium", "high", "xhigh", "max"),
    )
    defaults.update(overrides)
    return BackendCapabilities(**defaults)


def test_claude_backend_declares_name_and_capabilities():
    backend = ClaudeBackend(project_path=Path("/tmp/project"))
    assert backend.name == "claude"
    assert backend.model == DEFAULT_MODEL
    assert backend.capabilities.supports_thinking is True
    assert backend.capabilities.supports_usage_cap_detection is True


def test_claude_team_mode_blocks_external_side_effect_tools(tmp_path):
    from link_project_to_chat.team_safety import TeamAuthority

    backend = ClaudeBackend(project_path=tmp_path, skip_permissions=True)
    backend.team_system_note = "team mode"
    backend.team_authority = TeamAuthority("lpct")

    cmd = backend._build_cmd()

    assert "--dangerously-skip-permissions" not in cmd
    blocked = cmd[cmd.index("--disallowedTools") + 1].split(",")
    assert "Bash(git push:*)" in blocked
    assert "Bash(gh pr create:*)" in blocked
    assert "Bash(glab mr create:*)" in blocked
    assert "Bash(gh pr merge:*)" in blocked
    assert "Bash(glab mr merge:*)" in blocked
    assert "Bash(gh release create:*)" in blocked
    assert "Bash(glab release create:*)" in blocked
    assert "Bash(gh workflow run:*)" in blocked
    assert "Bash(glab ci run:*)" in blocked


def test_claude_team_mode_respects_push_grant(tmp_path):
    from link_project_to_chat.team_safety import TeamAuthority

    authority = TeamAuthority("lpct")
    authority.record_user_message(1, "--auth push")
    backend = ClaudeBackend(project_path=tmp_path)
    backend.team_system_note = "team mode"
    backend.team_authority = authority

    cmd = backend._build_cmd()

    blocked = cmd[cmd.index("--disallowedTools") + 1].split(",")
    assert "Bash(git push:*)" not in blocked
    assert "Bash(gh pr create:*)" in blocked
    assert "Bash(glab mr create:*)" in blocked


def test_status_includes_permission_tools_and_usage_cap_state():
    backend = ClaudeBackend(
        project_path=Path("/tmp/project"),
        skip_permissions=False,
        permission_mode="acceptEdits",
        allowed_tools=["Read", "Grep"],
        disallowed_tools=["Bash"],
    )
    backend._last_error = "USAGE_CAP: usage limit reached"
    backend._usage_capped = True

    status = backend.status

    assert status["permission"] == "acceptEdits"
    assert status["allowed_tools"] == ["Read", "Grep"]
    assert status["disallowed_tools"] == ["Bash"]
    assert status["last_error"] == "USAGE_CAP: usage limit reached"
    assert status["usage_capped"] is True


class _FakeProc:
    def __init__(self, stdout_lines: list[str], stderr_text: str = "", returncode: int = 0):
        payload = "".join(line + "\n" for line in stdout_lines).encode("utf-8")
        self.stdout = io.BytesIO(payload)
        self.stderr = io.BytesIO(stderr_text.encode("utf-8"))
        self.returncode = returncode
        self.pid = 4242

    def poll(self):
        if self.stdout.tell() >= len(self.stdout.getvalue()):
            return self.returncode
        return None

    def wait(self, timeout=None):
        return self.returncode


@pytest.mark.asyncio
async def test_successful_result_clears_stale_error_state():
    backend = ClaudeBackend(project_path=Path("/tmp/project"))
    backend._last_error = "old error"
    backend._usage_capped = True
    line = json.dumps({
        "type": "result",
        "result": "ok",
        "session_id": "s1",
        "modelUsage": {"claude-3": 1},
    })

    events = [event async for event in backend._read_events(_FakeProc([line]))]

    assert events
    assert backend.status["last_error"] is None
    assert backend.status["usage_capped"] is False


@pytest.mark.asyncio
async def test_claude_chat_returns_empty_string_when_stream_has_no_result(tmp_path):
    backend = ClaudeBackend(project_path=tmp_path)

    async def empty_stream(*_args, **_kwargs):
        if False:
            yield None

    backend.chat_stream = empty_stream  # type: ignore[method-assign]

    assert await backend.chat("ping") == ""


@pytest.mark.asyncio
async def test_probe_health_returns_ok_when_chat_succeeds(monkeypatch):
    backend = ClaudeBackend(project_path=Path("/tmp/project"))

    async def _fake_chat(message, on_proc=None):
        return "pong"

    monkeypatch.setattr(backend, "chat", _fake_chat)

    status = await backend.probe_health()

    assert status == HealthStatus(ok=True, usage_capped=False, error_message=None)


@pytest.mark.asyncio
async def test_probe_health_detects_usage_cap(monkeypatch):
    from link_project_to_chat.backends.claude import ClaudeStreamError

    backend = ClaudeBackend(project_path=Path("/tmp/project"))

    async def _fake_chat(message, on_proc=None):
        raise ClaudeStreamError("USAGE_CAP: usage limit reached")

    monkeypatch.setattr(backend, "chat", _fake_chat)

    status = await backend.probe_health()

    assert status.ok is False
    assert status.usage_capped is True
    assert backend.status["usage_capped"] is True
    assert backend.status["last_error"] == "USAGE_CAP: usage limit reached"


@pytest.mark.asyncio
async def test_probe_health_reports_stream_error(monkeypatch):
    from link_project_to_chat.backends.claude import ClaudeStreamError

    backend = ClaudeBackend(project_path=Path("/tmp/project"))

    async def _fake_chat(message, on_proc=None):
        raise ClaudeStreamError("connection refused")

    monkeypatch.setattr(backend, "chat", _fake_chat)

    status = await backend.probe_health()

    assert status.ok is False
    assert status.usage_capped is False
    assert status.error_message == "connection refused"


# ---------------------------------------------------------------------------
# Telegram-awareness preamble built from BackendCapabilities
# ---------------------------------------------------------------------------


def test_telegram_command_summary_includes_thinking_when_supported():
    summary = _telegram_command_summary(_capabilities(supports_thinking=True))
    assert "`/thinking on|off`" in summary


def test_telegram_command_summary_omits_thinking_when_unsupported():
    summary = _telegram_command_summary(_capabilities(supports_thinking=False))
    assert "/thinking" not in summary


def test_telegram_command_summary_omits_permissions_when_unsupported():
    summary = _telegram_command_summary(_capabilities(supports_permissions=False))
    assert "/permissions" not in summary


def test_telegram_command_summary_omits_compact_when_unsupported():
    summary = _telegram_command_summary(_capabilities(supports_compact=False))
    assert "/compact" not in summary


def test_telegram_command_summary_renders_models_pipe_joined():
    summary = _telegram_command_summary(_capabilities(models=("alpha", "beta")))
    assert "`/model alpha|beta`" in summary


def test_telegram_command_summary_omits_model_when_no_models_declared():
    summary = _telegram_command_summary(_capabilities(models=()))
    assert "/model" not in summary


def test_build_telegram_awareness_preserves_prefix_and_suffix_prose():
    preamble = _build_telegram_awareness(_capabilities())
    # Prefix prose must be intact.
    assert "running inside `link-project-to-chat`" in preamble
    assert "OUTPUT:" in preamble
    assert "USER COMMANDS:" in preamble
    # Prefix ends with "Suggest them when relevant: " and the helper output
    # flows directly into it (no double-"relevant", no separate label).
    assert "Suggest them when relevant: `/run <cmd>`" in preamble
    # Suffix prose (channel fragility) must be intact.
    assert "CHANNEL FRAGILITY:" in preamble
    assert "rebuild.sh" in preamble


def test_build_telegram_awareness_drops_unsupported_commands_for_minimal_backend():
    preamble = _build_telegram_awareness(
        _capabilities(
            models=(),
            supports_thinking=False,
            supports_permissions=False,
            supports_compact=False,
            supports_effort=False,
            effort_levels=(),
        )
    )
    assert "/thinking" not in preamble
    assert "/permissions" not in preamble
    assert "/compact" not in preamble
    # `/model` is also gated on declared models.
    assert "/model" not in preamble
    # `/effort` is gated on supports_effort; minimal backend has it False so
    # `/effort` is correctly absent here — see the supports_effort=True test.
    assert "/effort" not in preamble
    # Bot-level commands always appear regardless of capability flags —
    # they are defined in bot.py and not gated on the active backend.
    assert "`/run <cmd>`" in preamble
    assert "`/help`" in preamble
    assert "`/stop_skill`" in preamble
    assert "`/stop_persona`" in preamble


def test_telegram_command_summary_always_includes_bot_level_commands():
    """Bot-level commands (defined in bot.py) must appear regardless of
    backend capabilities — they are not gated on supports_* flags."""
    summary = _telegram_command_summary(
        _capabilities(
            models=(),
            supports_thinking=False,
            supports_permissions=False,
            supports_compact=False,
            supports_effort=False,
            effort_levels=(),
        )
    )
    # Original always-on commands (backtick-wrapped so LLMs reliably parse
    # the command tokens).
    assert "`/run <cmd>`" in summary
    assert "`/tasks`" in summary
    assert "`/skills`" in summary
    assert "`/use [name]`" in summary
    assert "`/persona [name]`" in summary
    assert "`/voice`" in summary
    assert "`/lang`" in summary
    assert "`/reset`" in summary
    assert "`/status`" in summary
    assert "`/help`" in summary
    # Restored always-on commands (regression: these were dropped in the
    # initial parameterization and must remain bot-level, not capability-gated).
    # `/effort` was promoted to a capability flag in the Codex /model+/effort
    # slice, so it's no longer always-on — see the
    # test_telegram_command_summary_includes_effort_when_supported test below.
    assert "`/stop_skill`" in summary
    assert "`/stop_persona`" in summary
