from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from link_project_to_chat.config import (
    Config,
    GoogleChatConfig,
    GoogleChatProjectOverride,
    ProjectConfig,
    save_config,
)
from link_project_to_chat.manager.process import ProcessManager


def _write_config(tmp_path: Path, *, with_google_chat: bool) -> Path:
    """Write a config.json with one project, optionally configured for google_chat."""
    cfg_path = tmp_path / "config.json"
    project_path = str(tmp_path)
    if with_google_chat:
        project = ProjectConfig(
            path=project_path,
            telegram_bot_token="",
            google_chat=GoogleChatProjectOverride(
                port=8091,
                service_account_file="/keys/alpha.json",
                public_url="https://alpha.example",
                root_command_id=7,
            ),
        )
        gc = GoogleChatConfig(host="127.0.0.1")
        config = Config(projects={"alpha": project}, google_chat=gc)
    else:
        project = ProjectConfig(path=project_path, telegram_bot_token="")
        config = Config(projects={"beta": project}, google_chat=GoogleChatConfig())
    save_config(config, cfg_path)
    return cfg_path


def test_start_google_chat_subprocess_execs_correct_command(monkeypatch, tmp_path):
    captured: dict[str, object] = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs

        class FakeProc:
            pid = 12345

            def poll(self):
                return None  # still running

        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    cfg_path = _write_config(tmp_path, with_google_chat=True)
    pm = ProcessManager(project_config_path=cfg_path)

    assert pm.start_google_chat_subprocess("alpha") is True
    cmd = captured["cmd"]
    assert "--project" in cmd
    assert "alpha" in cmd
    assert "--transport" in cmd
    assert "google_chat" in cmd
    assert "--google-chat-config-json" in cmd
    blob_idx = cmd.index("--google-chat-config-json") + 1
    resolved = json.loads(cmd[blob_idx])
    assert resolved["port"] == 8091
    assert resolved["service_account_file"] == "/keys/alpha.json"
    assert resolved["host"] == "127.0.0.1"  # from top-level

    assert pm.google_chat_pids == {"alpha": 12345}


def test_start_google_chat_subprocess_returns_false_when_unconfigured(tmp_path):
    cfg_path = _write_config(tmp_path, with_google_chat=False)
    pm = ProcessManager(project_config_path=cfg_path)
    assert pm.start_google_chat_subprocess("beta") is False
    assert "beta" not in pm.google_chat_pids


def test_start_google_chat_subprocess_is_idempotent_returns_false_on_second_call(
    monkeypatch, tmp_path
):
    """Calling start twice without an intervening stop must NOT leak the first PID."""
    pids_seen: list[int] = []

    def fake_popen(cmd, **kwargs):
        pid = 12345 + len(pids_seen)
        pids_seen.append(pid)
        class FakeProc:
            def poll(self): return None
        proc = FakeProc()
        proc.pid = pid
        return proc

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    cfg_path = _write_config(tmp_path, with_google_chat=True)
    pm = ProcessManager(project_config_path=cfg_path)

    assert pm.start_google_chat_subprocess("alpha") is True
    first_pid = pm.google_chat_pids["alpha"]

    # Second call without stop: must return False, must NOT replace the PID.
    assert pm.start_google_chat_subprocess("alpha") is False
    assert pm.google_chat_pids["alpha"] == first_pid

    # And no second Popen was issued.
    assert len(pids_seen) == 1


def test_stop_google_chat_subprocess_kills_and_clears_pid(monkeypatch, tmp_path):
    """stop() must route through _terminate_process_tree (uniform with the
    sibling stop()), and clear both pid + proc bookkeeping."""
    procs_terminated: list[object] = []

    class FakeProc:
        pid = 12345

        def poll(self):
            return None  # still alive when stop() is called

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: FakeProc())
    monkeypatch.setattr(
        "link_project_to_chat.manager.process._terminate_process_tree",
        lambda p: procs_terminated.append(p),
    )
    cfg_path = _write_config(tmp_path, with_google_chat=True)
    pm = ProcessManager(project_config_path=cfg_path)
    pm.start_google_chat_subprocess("alpha")

    assert pm.stop_google_chat_subprocess("alpha") is True
    assert len(procs_terminated) == 1
    assert procs_terminated[0].pid == 12345
    assert "alpha" not in pm.google_chat_pids


def test_stop_google_chat_subprocess_returns_false_when_not_running(tmp_path):
    cfg_path = _write_config(tmp_path, with_google_chat=True)
    pm = ProcessManager(project_config_path=cfg_path)
    assert pm.stop_google_chat_subprocess("alpha") is False


def test_stop_google_chat_subprocess_escalates_to_kill_on_timeout(monkeypatch, tmp_path):
    """If the child won't exit on graceful signals, _terminate_process_tree
    must escalate to SIGKILL. With ``_kill_process_tree=True`` set on the
    Popen (see ``start_google_chat_subprocess``), the helper goes through
    ``os.killpg(getpgid(pid), SIGKILL)`` — verify that path is taken.

    Note: _terminate_process_tree is a SIGKILL helper, not a SIGTERM-then-
    SIGKILL helper — it goes straight to SIGKILL on the process group when
    ``_kill_process_tree`` is set. We assert that the kill IS sent (so a
    stuck uvicorn worker can't survive stop()), not the SIGTERM step.
    """
    import signal
    from link_project_to_chat.manager import process as process_mod

    killpg_calls: list[tuple[int, int]] = []

    class StubbornProc:
        pid = 99999

        def poll(self):
            return None  # always "alive" — _terminate_process_tree must escalate

        def wait(self, timeout=None):
            # Don't actually block — the helper suppresses TimeoutExpired,
            # so returning immediately is fine and avoids slowing the test.
            raise subprocess.TimeoutExpired(cmd="x", timeout=timeout)

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: StubbornProc())
    monkeypatch.setattr(process_mod.os, "getpgid", lambda pid: 88888)
    monkeypatch.setattr(
        process_mod.os,
        "killpg",
        lambda pgid, sig: killpg_calls.append((pgid, sig)),
    )
    cfg_path = _write_config(tmp_path, with_google_chat=True)
    pm = ProcessManager(project_config_path=cfg_path)
    pm.start_google_chat_subprocess("alpha")
    # start_google_chat_subprocess must have set _kill_process_tree=True so
    # _terminate_process_tree takes the killpg(SIGKILL) branch.
    assert getattr(pm._google_chat_procs["alpha"], "_kill_process_tree", False) is True

    assert pm.stop_google_chat_subprocess("alpha") is True
    # killpg must have been invoked with SIGKILL on the resolved process group.
    assert killpg_calls == [(88888, signal.SIGKILL)]
    assert "alpha" not in pm.google_chat_pids
    assert "alpha" not in pm._google_chat_procs


def test_restart_google_chat_subprocess_calls_stop_then_start(monkeypatch, tmp_path):
    events: list[str] = []

    class FakeProc:
        pid = 12345

        def poll(self):
            return None

    def fake_popen(*a, **kw):
        events.append("popen")
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        "link_project_to_chat.manager.process._terminate_process_tree",
        lambda p: events.append("terminate"),
    )
    cfg_path = _write_config(tmp_path, with_google_chat=True)
    pm = ProcessManager(project_config_path=cfg_path)
    pm.start_google_chat_subprocess("alpha")
    events.clear()

    assert pm.restart_google_chat_subprocess("alpha") is True
    assert events == ["terminate", "popen"]


def test_start_autostart_spawns_telegram_and_google_chat(monkeypatch, tmp_path):
    """A project with both bot_token and a google_chat override should get
    both subprocesses on autostart."""
    spawned: list[list[str]] = []

    def fake_popen(cmd, **kwargs):
        spawned.append(cmd)
        class FakeProc:
            pid = 10000 + len(spawned)
            def poll(self): return None
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    # Build a config with one project having both Telegram + google_chat,
    # and one project Telegram-only. Both have autostart=True so
    # start_autostart actually iterates them.
    config = Config(
        projects={
            "alpha": ProjectConfig(
                path=str(tmp_path),
                telegram_bot_token="abc:123",
                autostart=True,
                google_chat=GoogleChatProjectOverride(
                    port=8091,
                    service_account_file="/keys/a.json",
                    public_url="https://a.example",
                    root_command_id=7,
                ),
            ),
            "beta": ProjectConfig(  # Telegram-only
                path=str(tmp_path),
                telegram_bot_token="xyz:789",
                autostart=True,
            ),
        },
        google_chat=GoogleChatConfig(host="127.0.0.1"),
    )

    cfg_path = tmp_path / "config.json"
    save_config(config, cfg_path)
    pm = ProcessManager(project_config_path=cfg_path)

    # Use whatever the actual autostart method is named on this codebase.
    # Common alternatives: start_autostart, start_all, run_autostart.
    if hasattr(pm, "start_autostart"):
        pm.start_autostart()
    elif hasattr(pm, "start_all"):
        pm.start_all()
    else:
        pytest.fail("ProcessManager has no autostart method — check the API")

    # Discriminate spawn types by argv content.
    telegram_spawns = [c for c in spawned if "--transport" not in c or "google_chat" not in c]
    google_chat_spawns = [c for c in spawned if "--transport" in c and "google_chat" in c]
    # Alpha + Beta both get Telegram bots (existing behavior).
    assert len(telegram_spawns) == 2, f"Expected 2 telegram spawns, got {len(telegram_spawns)}: {spawned}"
    # Only Alpha gets a Google Chat bot.
    assert len(google_chat_spawns) == 1, f"Expected 1 google_chat spawn, got {len(google_chat_spawns)}: {spawned}"
    assert "alpha" in google_chat_spawns[0]
    assert "alpha" in pm.google_chat_pids
    assert "beta" not in pm.google_chat_pids
