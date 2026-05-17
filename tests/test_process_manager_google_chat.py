from __future__ import annotations

import json
import subprocess
from pathlib import Path

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
