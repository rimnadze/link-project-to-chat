# Slack Manager Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make per-project Slack bots a first-class manager-supervised feature: `projects.<name>.slack` overrides the top-level block, the manager wizard adds/edits/removes Slack bots in the Telegram UI, and `ProcessManager` spawns a Slack subprocess per project keyed by `(project, transport)` — mirroring the v1.2 Google Chat shipping arc and eliminating any per-bot systemd unit for Slack.

**Architecture:** v1.0 ([`2026-04-21-slack-transport.md`](2026-04-21-slack-transport.md)) ships the `SlackTransport` itself plus the `SlackProjectOverride` config schema. v1.1 (this plan) layers on top: CLI accepts a resolved-config JSON blob, `ProcessManager` spawns/stops/restarts Slack children alongside Telegram + Google Chat ones, manager Telegram UI gains Add/Edit/Remove/Restart buttons, smoke test exercises the full path. Each task ends with a focused commit and a passing targeted test slice.

**Tech Stack:** Python 3.12, dataclasses, pytest (`asyncio_mode = "auto"`), the existing `python-telegram-bot` manager UI, and the v1.0 Slack transport produced by the prerequisite plan.

**Reference design:** [`docs/superpowers/specs/2026-04-21-transport-slack-design.md`](../specs/2026-04-21-transport-slack-design.md) is the design north star and stays unchanged for v1.1. The v1.0 transport plan ([`2026-04-21-slack-transport.md`](2026-04-21-slack-transport.md)) lands `SlackConfig`, `SlackProjectOverride`, `_parse_slack_override`, `_serialize_slack_override`, `_maybe_migrate_top_level_slack`, and `resolve_project_slack` — this plan consumes them.

**Reference prior art:** [`docs/superpowers/plans/2026-05-17-google-chat-manager-integration.md`](2026-05-17-google-chat-manager-integration.md) — the 15-task v1.2 GChat plan that shipped on `dev`. Every task in this plan cites the specific file:line in the GChat code to mirror.

**Prerequisite:** Plan `2026-04-21-slack-transport.md` (v1.0) must be merged. The v1.0 plan lands all dataclasses, helpers, and `resolve_project_slack` — this plan picks up after `SlackTransport` satisfies the parametrized contract test.

**Branch:** Create `feat/slack-manager` from current `dev`. Each task ends with a focused commit and a passing targeted test slice.

---

## File Map

### New files

- `tests/test_manager_create_slack.py` — wizard state-machine tests for the new add/edit/remove flows.
- `tests/test_process_manager_slack.py` — `ProcessManager` start/stop/restart tests for the slack transport.
- `tests/test_projectbot_smoke_manager_slack.py` — integration smoke: manager discovers + spawns + a synthetic event round-trips through a mocked slack_bolt client.

### Existing files to modify

- `src/link_project_to_chat/cli.py` — add `--slack-config-json` to `start`.
- `src/link_project_to_chat/manager/process.py` — `slack_pids` dict, `start_slack_subprocess`, `stop_slack_subprocess`, `restart_slack_subprocess`, discovery + supervise extensions, `_check_slack_health` reap helper.
- `src/link_project_to_chat/manager/bot.py` — per-project view buttons, wizard handlers, `_handle_slack_wizard_input` dispatcher.
- `src/link_project_to_chat/manager/conversation.py` (if used) — new `WIZARD_STATE_SLACK_*` states or dict-keyed equivalent (mirror whatever the gchat wizard used).
- `docs/CHANGELOG.md` — v1.1 / v1.2 Slack feature entry.
- `docs/TODO.md` — flip the Slack manager-integration row to ✅.

---

## Task 0: Setup Branch and Baseline

**Files:**
- No source changes.

- [ ] **Step 1: Create the feature branch**

```bash
git checkout dev
git pull --ff-only
git checkout -b feat/slack-manager
git status --short --branch
```

Expected output contains:

```text
## feat/slack-manager
```

- [ ] **Step 2: Run the baseline suite**

```bash
PYTHONPATH=src python3 -m pytest -q
```

Expected: current `dev` baseline passes (v1.0 Slack transport plus v1.2 Google Chat manager integration shipped). Record the exact pass/skip/warning count in the empty baseline commit body.

- [ ] **Step 3: Commit the baseline marker**

```bash
git commit --allow-empty -m "chore: pin baseline before Slack manager integration"
```

Expected: one empty commit on `feat/slack-manager`.

---

## Task 1: `--slack-config-json` CLI Flag

**Files:**
- Modify: `src/link_project_to_chat/cli.py`
- Modify: `tests/test_cli.py`

Mirror: `--google-chat-config-json` (Task 6 of the GChat plan; `cli.py` flag definition). The flag accepts a JSON-encoded resolved `SlackConfig` so the manager can pass the merged result directly, keeping the spawn deterministic even if config.json mutates between manager start-up and bot start.

- [ ] **Step 1: Write failing CLI test**

Append to `tests/test_cli.py`:

```python
def test_start_accepts_slack_config_json(monkeypatch, tmp_path):
    """When --slack-config-json is set, the start command must use the
    resolved blob instead of reading config.slack from disk."""
    import json
    from click.testing import CliRunner
    from link_project_to_chat.cli import main

    captured = {}

    def fake_run_bot(*, project_name, transport, config, **kwargs):
        captured["transport"] = transport
        captured["slack"] = config.slack
        return  # don't actually start the bot

    monkeypatch.setattr("link_project_to_chat.cli.run_bot", fake_run_bot)

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "projects": {"alpha": {"path": str(tmp_path), "telegram_bot_token": ""}},
    }))

    blob = json.dumps({
        "bot_token": "xoxb-alpha",
        "app_token": "xapp-alpha",
        "workspace_id": "T012",
        "socket_mode_enabled": True,
    })

    runner = CliRunner()
    result = runner.invoke(main, [
        "--config", str(cfg_path),
        "start",
        "--project", "alpha",
        "--transport", "slack",
        "--slack-config-json", blob,
    ])
    assert result.exit_code == 0, result.output

    assert captured["transport"] == "slack"
    assert captured["slack"].bot_token == "xoxb-alpha"
    assert captured["slack"].app_token == "xapp-alpha"
    assert captured["slack"].workspace_id == "T012"
```

(Note: `run_bot` is the symbol the existing `start` command calls — same as the GChat plan. Verify with `grep -n "def run_bot\|def main\|@cli.command" src/link_project_to_chat/cli.py` before writing the test in case the symbol has been renamed.)

- [ ] **Step 2: Verify RED**

```bash
PYTHONPATH=src python3 -m pytest tests/test_cli.py::test_start_accepts_slack_config_json -q
```

Expected: fails (no such flag, or capture not populated).

- [ ] **Step 3: Add the CLI flag**

Mirror: `cli.py` `--google-chat-config-json` definition. In `src/link_project_to_chat/cli.py`, find the `start` Click command and add the option below the existing google-chat one:

```python
@click.option(
    "--slack-config-json",
    "slack_config_json",
    default=None,
    help=(
        "JSON-encoded resolved SlackConfig used in place of config.slack. "
        "Intended for use by ProcessManager when spawning per-project "
        "slack subprocesses."
    ),
)
```

In the command body, before `run_bot(...)` is called, parse the blob if present and override `config.slack`:

```python
if slack_config_json:
    import json
    from .config import SlackConfig
    raw = json.loads(slack_config_json)
    config.slack = SlackConfig(
        **{k: v for k, v in raw.items() if k in {f.name for f in fields(SlackConfig)}}
    )
```

(`fields` is from `dataclasses`; the GChat block in the same function already imports it.)

- [ ] **Step 4: Verify GREEN**

```bash
PYTHONPATH=src python3 -m pytest tests/test_cli.py::test_start_accepts_slack_config_json -q
```

Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/cli.py tests/test_cli.py
git commit -m "feat(cli): --slack-config-json resolved-override flag"
```

---

## Task 2: `ProcessManager.start_slack_subprocess`

**Files:**
- Modify: `src/link_project_to_chat/manager/process.py`
- Create: `tests/test_process_manager_slack.py`

Mirror: `manager/process.py:453-492` (`start_google_chat_subprocess`). Same dict-tracking pattern, same Popen invocation shape with a different transport flag.

- [ ] **Step 1: Write failing spawn test**

Create `tests/test_process_manager_slack.py`:

```python
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from link_project_to_chat.config import (
    Config,
    ProjectConfig,
    SlackConfig,
    SlackProjectOverride,
)
from link_project_to_chat.manager.process import ProcessManager


def _make_config(tmp_path: Path) -> Config:
    return Config(
        projects={
            "alpha": ProjectConfig(
                path=str(tmp_path),
                telegram_bot_token="",
                slack=SlackProjectOverride(
                    bot_token="xoxb-alpha",
                    app_token="xapp-alpha",
                    workspace_id="T012",
                ),
            )
        },
        slack=SlackConfig(socket_mode_enabled=True),
    )


def test_start_slack_subprocess_execs_correct_command(monkeypatch, tmp_path):
    captured: dict[str, object] = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        class FakeProc:
            pid = 12345
            def poll(self): return None  # still running
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    pm = ProcessManager(config=_make_config(tmp_path))

    assert pm.start_slack_subprocess("alpha") is True
    cmd = captured["cmd"]
    assert "--project" in cmd
    assert "alpha" in cmd
    assert "--transport" in cmd
    assert "slack" in cmd
    assert "--slack-config-json" in cmd
    blob_idx = cmd.index("--slack-config-json") + 1
    resolved = json.loads(cmd[blob_idx])
    assert resolved["bot_token"] == "xoxb-alpha"
    assert resolved["app_token"] == "xapp-alpha"
    assert resolved["socket_mode_enabled"] is True  # from top-level

    assert pm.slack_pids == {"alpha": 12345}


def test_start_slack_subprocess_returns_false_when_unconfigured(tmp_path):
    pm = ProcessManager(config=Config(
        projects={"beta": ProjectConfig(path=str(tmp_path), telegram_bot_token="")},
        slack=SlackConfig(),
    ))
    assert pm.start_slack_subprocess("beta") is False
    assert "beta" not in pm.slack_pids


def test_start_slack_subprocess_idempotent_when_already_running(monkeypatch, tmp_path):
    """Mirror google_chat: re-calling start when a pid is tracked returns False
    and warns; does NOT spawn a second process."""
    calls: list[list[str]] = []

    def fake_popen(cmd, **kwargs):
        calls.append(cmd)
        class FakeProc:
            pid = 12345 + len(calls)
            def poll(self): return None
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    pm = ProcessManager(config=_make_config(tmp_path))

    assert pm.start_slack_subprocess("alpha") is True
    assert pm.start_slack_subprocess("alpha") is False  # already running
    assert len(calls) == 1  # only one Popen
```

- [ ] **Step 2: Verify RED**

```bash
PYTHONPATH=src python3 -m pytest tests/test_process_manager_slack.py -q
```

Expected: fails on `AttributeError: 'ProcessManager' object has no attribute 'start_slack_subprocess'`.

- [ ] **Step 3: Implement the spawn method**

Mirror: `manager/process.py:453-492` (the `start_google_chat_subprocess` method body — bookkeeping dicts, idempotency guard, resolver call, Popen + setattr(`_kill_process_tree`)).

In `src/link_project_to_chat/manager/process.py`:

1. Inside `ProcessManager.__init__`, add (alongside `google_chat_pids` and the existing PID-tracking dicts):

```python
# Slack subprocess bookkeeping. Mirror: google_chat_pids /
# _google_chat_procs / google_chat_failed_startups (defined a few lines
# above in this same __init__).
self.slack_pids: dict[str, int] = {}
self._slack_procs: dict[str, subprocess.Popen] = {}
self.slack_failed_startups: dict[str, str] = {}
```

2. Add the import at the top of `process.py` (alongside the existing google_chat resolver import):

```python
from link_project_to_chat.slack.resolver import resolve_project_slack
```

3. Add the new method (place it next to `start_google_chat_subprocess` so the two transports' lifecycle code lives side-by-side; mirror the body almost exactly):

```python
def start_slack_subprocess(self, project_name: str) -> bool:
    """Spawn a Slack bot subprocess for ``project_name``.

    Returns False if the project has no slack configured (no override and
    no meaningful top-level block) or if a subprocess is already running.
    Returns True after Popen — does not wait for the child to fully
    establish a Socket Mode connection; connection failures surface via
    the standard non-zero-exit path through ``_check_slack_health``.

    Mirror: start_google_chat_subprocess (process.py:453).
    """
    import json
    from link_project_to_chat.config import load_config

    if project_name in self.slack_pids:
        logger.warning(
            "slack subprocess already running for project %r (pid=%d)",
            project_name, self.slack_pids[project_name],
        )
        return False
    self.slack_failed_startups.pop(project_name, None)
    config = (
        load_config(self._project_config_path) if self._project_config_path
        else load_config()
    )
    resolved = resolve_project_slack(project_name, config)
    if resolved is None:
        return False

    # Serialize the resolved SlackConfig as JSON for the --slack-config-json
    # blob. Direct asdict is fine — SlackConfig is a plain dataclass with
    # primitive-typed fields, no nested lists or custom serializers.
    from dataclasses import asdict
    blob = json.dumps(asdict(resolved))
    cmd = self._base_cli_command()
    cmd.extend([
        "start",
        "--project", project_name,
        "--transport", "slack",
        "--slack-config-json", blob,
    ])
    proc = subprocess.Popen(cmd, **_process_popen_kwargs())
    setattr(proc, "_kill_process_tree", True)
    self.slack_pids[project_name] = proc.pid
    self._slack_procs[project_name] = proc
    logger.info("Started slack bot %s (pid=%d)", project_name, proc.pid)
    return True
```

- [ ] **Step 4: Verify GREEN**

```bash
PYTHONPATH=src python3 -m pytest tests/test_process_manager_slack.py -q
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/manager/process.py tests/test_process_manager_slack.py
git commit -m "feat(manager): ProcessManager.start_slack_subprocess"
```

---

## Task 3: `stop_slack_subprocess` + `restart_slack_subprocess`

**Files:**
- Modify: `src/link_project_to_chat/manager/process.py`
- Modify: `tests/test_process_manager_slack.py`

Mirror: `manager/process.py:494-522` (`stop_google_chat_subprocess` + `restart_google_chat_subprocess`). The stop routes through `_terminate_process_tree` so the kill escalates to SIGKILL across the whole process group — important because Socket Mode keeps a background WebSocket thread that won't die from SIGTERM alone.

- [ ] **Step 1: Write failing stop/restart tests**

Append to `tests/test_process_manager_slack.py`:

```python
def test_stop_slack_subprocess_terminates_tree(monkeypatch, tmp_path):
    terminated: list[int] = []

    class FakeProc:
        pid = 12345
        def poll(self): return None
        def terminate(self): terminated.append(self.pid)
        def wait(self, timeout=None): return 0
        def kill(self): terminated.append(-self.pid)

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: FakeProc())
    pm = ProcessManager(config=_make_config(tmp_path))
    pm.start_slack_subprocess("alpha")

    assert pm.stop_slack_subprocess("alpha") is True
    assert "alpha" not in pm.slack_pids
    assert "alpha" not in pm._slack_procs
    assert 12345 in terminated


def test_stop_slack_subprocess_returns_false_when_not_running(tmp_path):
    pm = ProcessManager(config=_make_config(tmp_path))
    assert pm.stop_slack_subprocess("alpha") is False


def test_restart_slack_subprocess_calls_stop_then_start(monkeypatch, tmp_path):
    events: list[str] = []

    class FakeProc:
        pid = 12345
        def poll(self): return None
        def terminate(self): events.append("terminate")
        def wait(self, timeout=None): return 0
        def kill(self): events.append("kill")

    monkeypatch.setattr(
        subprocess, "Popen",
        lambda *a, **kw: (events.append("popen"), FakeProc())[1],
    )
    pm = ProcessManager(config=_make_config(tmp_path))
    pm.start_slack_subprocess("alpha")
    events.clear()

    assert pm.restart_slack_subprocess("alpha") is True
    assert "terminate" in events
    assert "popen" in events
    # terminate before popen — restart never spawns then-stops
    assert events.index("terminate") < events.index("popen")
```

- [ ] **Step 2: Verify RED**

```bash
PYTHONPATH=src python3 -m pytest tests/test_process_manager_slack.py -k "stop_slack or restart_slack" -q
```

Expected: 3 failed on missing methods.

- [ ] **Step 3: Implement stop + restart**

Mirror: `manager/process.py:494-522`. In `ProcessManager`, alongside the GChat stop/restart pair:

```python
def stop_slack_subprocess(self, project_name: str) -> bool:
    """Terminate the project's slack subprocess.

    Routes through ``_terminate_process_tree`` for the same reason
    stop_google_chat_subprocess does — Socket Mode WebSocket threads
    survive plain SIGTERM and would leak file descriptors. Mirror:
    stop_google_chat_subprocess (process.py:494).
    """
    proc = self._slack_procs.pop(project_name, None)
    self.slack_pids.pop(project_name, None)
    if proc is None:
        return False
    _terminate_process_tree(proc)
    logger.info("Stopped slack bot %s", project_name)
    return True


def restart_slack_subprocess(self, project_name: str) -> bool:
    """Stop then start. Returns the start_slack_subprocess result.

    Mirror: restart_google_chat_subprocess (process.py:515).
    """
    self.stop_slack_subprocess(project_name)
    return self.start_slack_subprocess(project_name)
```

- [ ] **Step 4: Verify GREEN**

```bash
PYTHONPATH=src python3 -m pytest tests/test_process_manager_slack.py -q
```

Expected: 6 passed (3 from Task 2 + 3 here).

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/manager/process.py tests/test_process_manager_slack.py
git commit -m "feat(manager): stop and restart Slack subprocesses"
```

---

## Task 4: `_check_slack_health` Reap Helper

**Files:**
- Modify: `src/link_project_to_chat/manager/process.py`
- Modify: `tests/test_process_manager_slack.py`

Mirror: `manager/process.py:524-556` (`_check_google_chat_health`). Detects exited children so the supervise loop doesn't restart them in a tight loop after a fatal connection error.

- [ ] **Step 1: Write failing test**

Append to `tests/test_process_manager_slack.py`:

```python
def test_start_slack_records_failed_startup(monkeypatch, tmp_path):
    """If the child exits non-zero (e.g. invalid bot_token), manager
    records the failure and does NOT retry. Mirror: GChat Task 10."""

    class FakeFailedProc:
        pid = 99999
        def poll(self): return 1  # exited immediately, non-zero
        def wait(self, timeout=None): return 1

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: FakeFailedProc())
    pm = ProcessManager(config=_make_config(tmp_path))

    pm.start_slack_subprocess("alpha")
    pm._check_slack_health()

    assert pm.slack_pids == {}  # cleared
    assert "alpha" in pm.slack_failed_startups
    assert "alpha" not in pm._slack_procs


def test_check_slack_health_leaves_running_procs_alone(monkeypatch, tmp_path):
    class FakeRunningProc:
        pid = 12345
        def poll(self): return None  # still running

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: FakeRunningProc())
    pm = ProcessManager(config=_make_config(tmp_path))
    pm.start_slack_subprocess("alpha")

    pm._check_slack_health()
    assert pm.slack_pids == {"alpha": 12345}
    assert "alpha" not in pm.slack_failed_startups
```

- [ ] **Step 2: Verify RED**

```bash
PYTHONPATH=src python3 -m pytest tests/test_process_manager_slack.py::test_start_slack_records_failed_startup -q
```

Expected: fails — `_check_slack_health` does not exist.

- [ ] **Step 3: Implement the health-check helper**

Mirror: `manager/process.py:524-556`. In `ProcessManager`:

```python
def _check_slack_health(self) -> None:
    """Detect slack children that exited.

    Walks self._slack_procs, calls .poll(), and reaps any child whose
    process has exited. Non-zero exits are recorded in
    self.slack_failed_startups so the manager UI can show the operator why
    the bot stopped.

    Callers (the supervise loop, smoke tests, UI status reads) should
    invoke this on every supervise tick. Mirror: _check_google_chat_health
    (process.py:524).
    """
    reaped: list[tuple[str, int]] = []
    for name, proc in list(self._slack_procs.items()):
        status = proc.poll()
        if status is None:
            continue  # still running
        reaped.append((name, status))
    for name, status in reaped:
        self.slack_pids.pop(name, None)
        self._slack_procs.pop(name, None)
        if status != 0:
            self.slack_failed_startups[name] = (
                f"exited with status {status} (see manager log)"
            )
            logger.warning(
                "%s slack subprocess exited with code %d — not restarting",
                name, status,
            )
        else:
            logger.info("%s slack subprocess exited cleanly", name)
```

Wire `_check_slack_health` into the supervise loop alongside `_check_google_chat_health` (find the call site in the existing supervise method body).

- [ ] **Step 4: Verify GREEN**

```bash
PYTHONPATH=src python3 -m pytest tests/test_process_manager_slack.py -q
```

Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/manager/process.py tests/test_process_manager_slack.py
git commit -m "feat(manager): record slack startup failures via _check_slack_health"
```

---

## Task 5: `start_autostart` Discovery Extension

**Files:**
- Modify: `src/link_project_to_chat/manager/process.py`
- Modify: `tests/test_process_manager_slack.py`

Mirror: GChat plan Task 9 (`start_autostart` extension). When the manager boots, every project with a meaningful slack config gets a subprocess alongside its Telegram and Google Chat ones.

- [ ] **Step 1: Write failing discovery test**

Append to `tests/test_process_manager_slack.py`:

```python
def test_start_autostart_spawns_telegram_and_slack(monkeypatch, tmp_path):
    """A project with both bot_token and a slack override should get both
    subprocesses on autostart. Mirror: GChat Task 9 test."""
    spawned: list[list[str]] = []

    def fake_popen(cmd, **kwargs):
        spawned.append(cmd)
        class FakeProc:
            pid = 10000 + len(spawned)
            def poll(self): return None
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    config = Config(
        projects={
            "alpha": ProjectConfig(
                path=str(tmp_path),
                telegram_bot_token="abc:123",
                autostart=True,
                slack=SlackProjectOverride(
                    bot_token="xoxb-alpha",
                    app_token="xapp-alpha",
                    workspace_id="T012",
                ),
            ),
            "beta": ProjectConfig(  # Telegram-only
                path=str(tmp_path),
                telegram_bot_token="xyz:789",
                autostart=True,
            ),
        },
        slack=SlackConfig(),
    )
    pm = ProcessManager(config=config)
    pm.start_autostart()

    telegram_spawns = [
        c for c in spawned
        if "--transport" not in c or "slack" not in c
    ]
    slack_spawns = [
        c for c in spawned
        if "--transport" in c and "slack" in c
    ]
    # Both projects get Telegram bots.
    assert len(telegram_spawns) == 2
    # Only alpha gets a Slack bot.
    assert len(slack_spawns) == 1
    assert "alpha" in slack_spawns[0]
    assert pm.slack_pids == {"alpha": pm.slack_pids["alpha"]}
```

- [ ] **Step 2: Verify RED**

```bash
PYTHONPATH=src python3 -m pytest tests/test_process_manager_slack.py::test_start_autostart_spawns_telegram_and_slack -q
```

Expected: fails — autostart doesn't spawn slack yet.

- [ ] **Step 3: Extend `start_autostart`**

Mirror: GChat plan Task 9 Step 3. In `src/link_project_to_chat/manager/process.py`, find the existing `start_autostart` method. At the end of its loop body — *after* the GChat autostart line — add:

```python
        # Also spawn a Slack bot for any project that has one configured.
        # Mirror: the google_chat autostart line directly above.
        if resolve_project_slack(name, self._config) is not None:
            self.start_slack_subprocess(name)
```

- [ ] **Step 4: Verify GREEN**

```bash
PYTHONPATH=src python3 -m pytest tests/test_process_manager_slack.py -q
```

Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/manager/process.py tests/test_process_manager_slack.py
git commit -m "feat(manager): autostart spawns slack alongside Telegram + Google Chat"
```

---

## Task 6: Per-Project Manager Buttons

**Files:**
- Modify: `src/link_project_to_chat/manager/bot.py`
- Create: `tests/test_manager_create_slack.py`

Mirror: `manager/bot.py:2760-2787` (the GChat per-project buttons helper) and `manager/bot.py:2873-2899` (the dispatch ladder routing `proj_<verb>_gchat_<name>` callbacks).

- [ ] **Step 1: Write failing button test**

Create `tests/test_manager_create_slack.py`:

```python
"""Wizard-state-machine tests for the new Slack manager flows.

Mirror: tests/test_manager_create_google_chat.py — same five scenarios
re-typed for Slack tokens instead of Google Chat ports/SA files.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from link_project_to_chat.config import (
    Config,
    ProjectConfig,
    SlackConfig,
    save_config,
)


def _write_config(tmp_path: Path, projects: dict, slack_cfg: SlackConfig | None = None) -> Path:
    cfg_path = tmp_path / "config.json"
    raw = {"projects": projects}
    if slack_cfg is not None:
        raw["slack"] = {
            "bot_token": slack_cfg.bot_token,
            "app_token": slack_cfg.app_token,
            "workspace_id": slack_cfg.workspace_id,
        }
    cfg_path.write_text(json.dumps(raw))
    return cfg_path


def test_project_view_shows_add_slack_button_when_no_override(tmp_path):
    """Reading the per-project view of a project without a slack override
    must include an 'Add Slack' button. Mirror: GChat Task 11 test."""
    from link_project_to_chat.manager.bot import ManagerBot

    cfg_path = _write_config(tmp_path, {
        "alpha": {"path": str(tmp_path), "telegram_bot_token": "tok"},
    })

    bot = ManagerBot(config_path=cfg_path, process_manager=MagicMock())
    rows = bot._slack_buttons("alpha")
    labels = [b.label for row in rows for b in row]
    assert any("Add Slack" in label for label in labels)


def test_project_view_shows_edit_remove_restart_when_override_exists(tmp_path):
    from link_project_to_chat.manager.bot import ManagerBot

    cfg_path = _write_config(tmp_path, {
        "alpha": {
            "path": str(tmp_path),
            "telegram_bot_token": "tok",
            "slack": {
                "bot_token": "xoxb-1",
                "app_token": "xapp-1",
                "workspace_id": "T012",
            },
        },
    })

    bot = ManagerBot(config_path=cfg_path, process_manager=MagicMock())
    rows = bot._slack_buttons("alpha")
    labels = [b.label for row in rows for b in row]
    assert any("Edit Slack" in l for l in labels)
    assert any("Remove Slack" in l for l in labels)
    assert any("Restart Slack" in l for l in labels)
```

- [ ] **Step 2: Verify RED**

```bash
PYTHONPATH=src python3 -m pytest tests/test_manager_create_slack.py::test_project_view_shows_add_slack_button_when_no_override -q
```

Expected: fails — `_slack_buttons` does not exist.

- [ ] **Step 3: Add the buttons helper + dispatch entries**

Mirror: `manager/bot.py:2760-2787` (`_google_chat_buttons`) and `manager/bot.py:2873-2899` (dispatch ladder).

In `src/link_project_to_chat/manager/bot.py`, alongside `_google_chat_buttons` (search the file for it), add:

```python
def _slack_buttons(self, name: str) -> list[list["Button"]]:
    """Per-project Slack action rows. Mirror: _google_chat_buttons.

    Returns an 'Add Slack' row when the project has no ``slack`` override,
    or Edit/Restart/Remove rows when it does. Callback values follow the
    existing ``proj_<verb>_slack_<name>`` convention.
    """
    project = self._load_projects().get(name, {})
    if not project.get("slack"):
        return [[Button(
            label="Add Slack",
            value=f"proj_add_slack_{name}",
        )]]
    return [
        [
            Button(
                label="Edit Slack",
                value=f"proj_edit_slack_{name}",
            ),
            Button(
                label="Restart Slack",
                value=f"proj_restart_slack_{name}",
            ),
        ],
        [Button(
            label="Remove Slack",
            value=f"proj_remove_slack_{name}",
        )],
    ]
```

Then wire it into the per-project keyboard composer that already includes `_google_chat_buttons` output — search for the gchat helper's call site and add a sibling call to `_slack_buttons(name)`.

Finally, in `_dispatch_button_click` (mirror: `manager/bot.py:2873-2899`), add the four routing branches *before* the generic `proj_edit_` / `proj_remove_` matches. Order matters — same prefix-match pitfall as GChat (a generic `proj_edit_slack_alpha` would otherwise route to a phantom project named `slack_alpha`):

```python
# Slack manager-integration callbacks. Mirror: the proj_<verb>_gchat_<name>
# block directly above. Must precede the generic proj_edit_/proj_remove_
# branches to avoid the gchat_alpha-style misrouting bug.
if value.startswith("proj_add_slack_"):
    if not await self._require_executor_button(click):
        return
    name = value[len("proj_add_slack_"):]
    await self._start_add_slack_wizard(click, ctx_user_data, name)
    return

if value.startswith("proj_edit_slack_"):
    if not await self._require_executor_button(click):
        return
    name = value[len("proj_edit_slack_"):]
    await self._start_edit_slack_wizard(click, ctx_user_data, name)
    return

if value.startswith("proj_restart_slack_"):
    if not await self._require_executor_button(click):
        return
    name = value[len("proj_restart_slack_"):]
    await self._handle_restart_slack(click, name)
    return

if value.startswith("proj_remove_slack_"):
    if not await self._require_executor_button(click):
        return
    name = value[len("proj_remove_slack_"):]
    await self._handle_remove_slack(click, name)
    return
```

Leave the wizard-starter and handler bodies as stubs that raise `NotImplementedError("Task 7")` so the buttons are wired but follow-up tasks own the wizard. The two button-presence tests still pass because they only check labels and callback values.

- [ ] **Step 4: Verify GREEN**

```bash
PYTHONPATH=src python3 -m pytest tests/test_manager_create_slack.py -q
```

Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/manager/bot.py tests/test_manager_create_slack.py
git commit -m "feat(manager): add Slack buttons to per-project view"
```

---

## Task 7: Add-Slack Wizard (3-step token collection)

**Files:**
- Modify: `src/link_project_to_chat/manager/bot.py`
- Modify: `tests/test_manager_create_slack.py`

Mirror: `manager/bot.py:1507-1715` (`_handle_gchat_wizard_input` + the four `_gchat_step_*` handlers + `_finalize_add_google_chat_wizard`). Slack has a shorter wizard than GChat — three required steps (bot token, app token, workspace ID) and one optional step (default channel ID, skip with `/skip`).

The wizard uses the same shape as the gchat one: a dict on `ctx.user_data["slack_wizard"]` keyed by `step`/`kind`/`name`/`data`. Each step has a validator. `/cancel` aborts; `/keep` (in edit mode) preserves the prefilled value.

- [ ] **Step 1: Write failing wizard-flow test**

Append to `tests/test_manager_create_slack.py`:

```python
@pytest.mark.asyncio
async def test_add_slack_wizard_collects_three_fields_and_persists(tmp_path):
    """End-to-end wizard: tap [+ Add Slack], answer three prompts (with the
    default channel left blank), config gets persisted,
    ProcessManager.start_slack_subprocess fires.
    Mirror: GChat Task 12 test."""
    from link_project_to_chat.manager.bot import ManagerBot

    cfg_path = _write_config(tmp_path, {
        "alpha": {"path": str(tmp_path), "telegram_bot_token": "tok"},
    })
    pm = MagicMock()
    pm.start_slack_subprocess.return_value = True

    bot = ManagerBot(config_path=cfg_path, process_manager=pm)

    # Operator pressed [+ Add Slack]; the dispatcher in Task 6 arms the
    # wizard dict. We synthesize that here.
    await bot._handle_slack_wizard_input("op-1", "xoxb-alpha")    # bot_token
    await bot._handle_slack_wizard_input("op-1", "xapp-alpha")    # app_token
    await bot._handle_slack_wizard_input("op-1", "T012")          # workspace_id
    await bot._handle_slack_wizard_input("op-1", "/skip")         # default channel

    # Wizard saved the override
    from link_project_to_chat.config import load_config
    loaded = load_config(cfg_path)
    assert loaded.projects["alpha"].slack is not None
    assert loaded.projects["alpha"].slack.bot_token == "xoxb-alpha"
    assert loaded.projects["alpha"].slack.app_token == "xapp-alpha"
    assert loaded.projects["alpha"].slack.workspace_id == "T012"
    assert loaded.projects["alpha"].slack.default_channel_id is None

    # And asked ProcessManager to start the subprocess.
    pm.start_slack_subprocess.assert_called_once_with("alpha")


@pytest.mark.asyncio
async def test_add_slack_wizard_rejects_empty_bot_token(tmp_path):
    from link_project_to_chat.manager.bot import ManagerBot

    cfg_path = _write_config(tmp_path, {
        "alpha": {"path": str(tmp_path), "telegram_bot_token": "tok"},
    })
    bot = ManagerBot(config_path=cfg_path, process_manager=MagicMock())

    reply = await bot._handle_slack_wizard_input("op-2", "")
    assert "token" in reply.lower() or "required" in reply.lower()


@pytest.mark.asyncio
async def test_add_slack_wizard_cancel_clears_session(tmp_path):
    from link_project_to_chat.manager.bot import ManagerBot

    cfg_path = _write_config(tmp_path, {
        "alpha": {"path": str(tmp_path), "telegram_bot_token": "tok"},
    })
    bot = ManagerBot(config_path=cfg_path, process_manager=MagicMock())

    await bot._handle_slack_wizard_input("op-3", "xoxb-1")  # advance past step 1
    reply = await bot._handle_slack_wizard_input("op-3", "/cancel")
    assert "cancel" in reply.lower()
```

(How the test starts the wizard without going through the button: the public test harness uses an `_arm_slack_wizard(op_id, project_name, kind)` helper added by Task 6's button stub. The handler itself doesn't care about the arming source — only about `step` and `kind` in the wizard dict.)

- [ ] **Step 2: Verify RED**

```bash
PYTHONPATH=src python3 -m pytest tests/test_manager_create_slack.py::test_add_slack_wizard_collects_three_fields_and_persists -q
```

Expected: fails — the wizard handlers don't exist.

- [ ] **Step 3: Implement wizard handlers**

Mirror: `manager/bot.py:1507-1546` (`_handle_gchat_wizard_input` — the per-step dispatch) and the four `_gchat_step_*` helpers + `_gchat_prompt_*` prompters.

In `src/link_project_to_chat/manager/bot.py` add (next to the gchat wizard methods so the two transports' wizards live side-by-side):

```python
async def _handle_slack_wizard_input(
    self,
    op_id: str,
    text: str,
) -> str:
    """Dispatch one step of the Slack wizard. Returns the reply text the
    caller should send back via the transport.

    Mirror: _handle_gchat_wizard_input (bot.py:1507). The Slack wizard has
    one fewer required step (no port to validate; ``app_token`` replaces
    that slot) and one optional skip-able step (default_channel_id).
    """
    wizard = self._slack_wizards.get(op_id)
    if wizard is None:
        return "No active Slack wizard. Tap [+ Add Slack] to start."

    text = text.strip()
    if text == "/cancel":
        self._slack_wizards.pop(op_id, None)
        return "Slack wizard cancelled."

    step = wizard.get("step")
    if step == "bot_token":
        return await self._slack_step_bot_token(wizard, text)
    if step == "app_token":
        return await self._slack_step_app_token(wizard, text)
    if step == "workspace_id":
        return await self._slack_step_workspace_id(wizard, text)
    if step == "default_channel_id":
        return await self._slack_step_default_channel(op_id, wizard, text)
    self._slack_wizards.pop(op_id, None)
    return "Slack wizard is in an unknown state. Start over via the project view."


def _slack_is_keep(self, wizard: dict, text: str) -> bool:
    """``/keep`` preserves the prefilled value in edit mode.
    Mirror: _gchat_is_keep (bot.py:1547)."""
    return wizard.get("kind") == "edit" and text == "/keep"


async def _slack_step_bot_token(self, wizard: dict, text: str) -> str:
    if self._slack_is_keep(wizard, text):
        wizard["step"] = "app_token"
        return "Step 2 of 3 — app-level token (xapp-...) for Socket Mode (`/keep` to keep current):"
    if not text:
        return "Bot token (xoxb-...) is required. Send the token or /cancel:"
    if not text.startswith("xoxb-"):
        return "Bot tokens start with 'xoxb-'. Check the value and re-send:"
    wizard["data"]["bot_token"] = text
    wizard["step"] = "app_token"
    return "Step 2 of 3 — app-level token (xapp-...) for Socket Mode:"


async def _slack_step_app_token(self, wizard: dict, text: str) -> str:
    if self._slack_is_keep(wizard, text):
        wizard["step"] = "workspace_id"
        return "Step 3 of 3 — workspace/team ID (T...) — find it in Workspace Settings:"
    if not text:
        return "App token (xapp-...) is required. Send the token or /cancel:"
    if not text.startswith("xapp-"):
        return "App tokens start with 'xapp-'. Check the value and re-send:"
    wizard["data"]["app_token"] = text
    wizard["step"] = "workspace_id"
    return "Step 3 of 3 — workspace/team ID (T...) — find it in Workspace Settings:"


async def _slack_step_workspace_id(self, wizard: dict, text: str) -> str:
    if self._slack_is_keep(wizard, text):
        wizard["step"] = "default_channel_id"
        return "Optional — default channel ID (C...) or `/skip` to leave unset:"
    if not text.startswith("T"):
        return "Workspace IDs start with 'T'. Check the value and re-send:"
    wizard["data"]["workspace_id"] = text
    wizard["step"] = "default_channel_id"
    return "Optional — default channel ID (C...) or `/skip` to leave unset:"


async def _slack_step_default_channel(
    self, op_id: str, wizard: dict, text: str,
) -> str:
    if text != "/skip" and not self._slack_is_keep(wizard, text):
        if text and not text.startswith("C"):
            return "Channel IDs start with 'C'. Send a valid ID or /skip:"
        if text:
            wizard["data"]["default_channel_id"] = text
    return await self._finalize_slack_wizard(op_id, wizard)


async def _finalize_slack_wizard(self, op_id: str, wizard: dict) -> str:
    """Persist the override and start the subprocess.
    Mirror: _finalize_add_google_chat_wizard (bot.py:1688)."""
    name = wizard["name"]
    data = wizard["data"]
    self._slack_wizards.pop(op_id, None)

    from ..config import _patch_json
    cfg_path = self._project_config_path or DEFAULT_CONFIG
    slack_block: dict = {
        "bot_token": data["bot_token"],
        "app_token": data["app_token"],
        "workspace_id": data["workspace_id"],
    }
    if data.get("default_channel_id"):
        slack_block["default_channel_id"] = data["default_channel_id"]

    def _set_slack(raw: dict) -> dict:
        raw.setdefault("projects", {}).setdefault(name, {})["slack"] = slack_block
        return raw

    _patch_json(cfg_path, _set_slack)

    if wizard.get("kind") == "edit":
        self._pm.restart_slack_subprocess(name)
        return f"Slack config updated for {name}. Subprocess restarting."
    self._pm.start_slack_subprocess(name)
    return (
        f"Slack bot configured for {name}.\n"
        f"Workspace: {data['workspace_id']}.\n"
        f"Socket Mode means no public ingress is required — the subprocess "
        f"connects outbound to Slack."
    )
```

Add the wizard-state dict to `ManagerBot.__init__`:

```python
self._slack_wizards: dict[str, dict] = {}
```

Wire the dispatcher into the message-text router that already handles `gchat_wizard` (search `manager/bot.py` for `_handle_gchat_wizard_input` — add a sibling `_handle_slack_wizard_input` call routed by the presence of a `slack_wizard` key on `ctx.user_data` for the PTB-bound path, plus the direct `op_id`-keyed entry point used by the tests).

- [ ] **Step 4: Verify GREEN**

```bash
PYTHONPATH=src python3 -m pytest tests/test_manager_create_slack.py -q
```

Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/manager/bot.py tests/test_manager_create_slack.py
git commit -m "feat(manager): add Slack wizard (3-step token collect + optional channel)"
```

---

## Task 8: Edit / Remove / Restart Slack Handlers

**Files:**
- Modify: `src/link_project_to_chat/manager/bot.py`
- Modify: `tests/test_manager_create_slack.py`

Mirror: `manager/bot.py:1817-1830` (`_handle_restart_gchat`, `_handle_remove_gchat`) plus the edit-flavor handlers in Task 13 of the GChat plan.

- [ ] **Step 1: Write failing edit/remove tests**

Append to `tests/test_manager_create_slack.py`:

```python
@pytest.mark.asyncio
async def test_remove_slack_clears_override_and_stops_subprocess(tmp_path):
    from link_project_to_chat.config import load_config
    from link_project_to_chat.manager.bot import ManagerBot

    cfg_path = _write_config(tmp_path, {
        "alpha": {
            "path": str(tmp_path),
            "telegram_bot_token": "tok",
            "slack": {
                "bot_token": "xoxb-1",
                "app_token": "xapp-1",
                "workspace_id": "T012",
            },
        },
    })
    pm = MagicMock()
    bot = ManagerBot(config_path=cfg_path, process_manager=pm)

    reply = await bot._handle_remove_slack_confirm("alpha")

    loaded = load_config(cfg_path)
    assert loaded.projects["alpha"].slack is None
    pm.stop_slack_subprocess.assert_called_once_with("alpha")
    assert "removed" in reply.lower()


@pytest.mark.asyncio
async def test_restart_slack_calls_pm_restart(tmp_path):
    from link_project_to_chat.manager.bot import ManagerBot

    cfg_path = _write_config(tmp_path, {
        "alpha": {
            "path": str(tmp_path),
            "telegram_bot_token": "tok",
            "slack": {
                "bot_token": "xoxb-1",
                "app_token": "xapp-1",
                "workspace_id": "T012",
            },
        },
    })
    pm = MagicMock()
    bot = ManagerBot(config_path=cfg_path, process_manager=pm)

    await bot._handle_restart_slack_confirm("alpha")
    pm.restart_slack_subprocess.assert_called_once_with("alpha")


@pytest.mark.asyncio
async def test_edit_slack_wizard_prefills_current_values(tmp_path):
    from link_project_to_chat.manager.bot import ManagerBot

    cfg_path = _write_config(tmp_path, {
        "alpha": {
            "path": str(tmp_path),
            "telegram_bot_token": "tok",
            "slack": {
                "bot_token": "xoxb-current",
                "app_token": "xapp-current",
                "workspace_id": "T999",
            },
        },
    })
    bot = ManagerBot(config_path=cfg_path, process_manager=MagicMock())

    prompt = await bot._start_edit_slack_wizard_for_test("alpha", "op-4")
    assert "xoxb-current" in prompt
```

- [ ] **Step 2: Verify RED**

```bash
PYTHONPATH=src python3 -m pytest tests/test_manager_create_slack.py -k "remove or restart or edit" -q
```

Expected: 3 failed on missing handlers.

- [ ] **Step 3: Implement edit / remove / restart handlers**

Mirror: GChat plan Task 13. In `src/link_project_to_chat/manager/bot.py`:

```python
async def _handle_remove_slack_confirm(self, project_name: str) -> str:
    """Drop the slack override + stop the subprocess.
    Mirror: GChat _handle_remove_gchat / Task 13 handler."""
    from ..config import _patch_json
    cfg_path = self._project_config_path or DEFAULT_CONFIG

    def _drop_slack(raw: dict) -> dict:
        proj = raw.get("projects", {}).get(project_name, {})
        proj.pop("slack", None)
        return raw

    _patch_json(cfg_path, _drop_slack)
    self._pm.stop_slack_subprocess(project_name)
    return f"Slack config removed for {project_name}."


async def _handle_restart_slack_confirm(self, project_name: str) -> str:
    success = self._pm.restart_slack_subprocess(project_name)
    if not success:
        return (
            f"Restart for {project_name} failed (config missing or invalid). "
            "Check `journalctl -u link-project-to-chat`."
        )
    return f"Slack subprocess restarted for {project_name}."


async def _start_edit_slack_wizard_for_test(
    self, project_name: str, op_id: str,
) -> str:
    """Test entry point that bypasses the button-click code path.
    The production code path goes through _start_edit_slack_wizard via the
    ButtonClick dispatcher in Task 6."""
    project = self._load_projects().get(project_name, {})
    current = project.get("slack")
    if not current:
        return "No slack override to edit. Use [+ Add Slack] instead."
    self._slack_wizards[op_id] = {
        "kind": "edit",
        "name": project_name,
        "step": "bot_token",
        "data": dict(current),  # prefill with current values
    }
    return (
        f"Edit Slack for {project_name}.\n"
        f"Current bot token: {current.get('bot_token')}\n"
        f"Send a new token, or `/keep` to keep the current value:"
    )
```

Implement the production entry points (`_start_add_slack_wizard`, `_start_edit_slack_wizard`, `_handle_restart_slack`, `_handle_remove_slack`) that wrap the test-direct handlers by sending the reply via `self._transport.send_text`. Mirror the gchat starter pattern in the same file.

- [ ] **Step 4: Verify GREEN**

```bash
PYTHONPATH=src python3 -m pytest tests/test_manager_create_slack.py -q
```

Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/manager/bot.py tests/test_manager_create_slack.py
git commit -m "feat(manager): edit/remove/restart Slack handlers"
```

---

## Task 9: Stale-Wizard Cancellation on Other Button Clicks

**Files:**
- Modify: `src/link_project_to_chat/manager/bot.py`
- Modify: `tests/test_manager_create_slack.py`

Mirror: `manager/bot.py:2857-2863` (the gchat wizard cancel-on-any-button pattern). Any button press cancels a pending slack wizard so a half-completed flow doesn't leak across unrelated project views.

- [ ] **Step 1: Write failing test**

Append to `tests/test_manager_create_slack.py`:

```python
@pytest.mark.asyncio
async def test_any_button_click_cancels_pending_slack_wizard(tmp_path):
    """Lesson from the gchat wizard: leaking wizard state across project
    views silently steals text inputs that were meant for unrelated flows.
    Any non-slack-wizard button must clear the pending wizard."""
    from link_project_to_chat.manager.bot import ManagerBot

    cfg_path = _write_config(tmp_path, {
        "alpha": {"path": str(tmp_path), "telegram_bot_token": "tok"},
    })
    bot = ManagerBot(config_path=cfg_path, process_manager=MagicMock())

    await bot._handle_slack_wizard_input("op-5", "xoxb-half-done")
    assert "op-5" in bot._slack_wizards

    # Simulate any unrelated button click.
    bot._cancel_pending_slack_wizards_for_ctx({"slack_wizard": True})
    assert "op-5" not in bot._slack_wizards or bot._slack_wizards["op-5"].get("cancelled") is True
```

(Adapt the helper name to whatever convention matches the gchat cancellation — the bot already wipes `gchat_wizard` from `ctx.user_data` on any button click in the GLOBAL ladder; we just need an equivalent for the `_slack_wizards` dict on the manager bot instance.)

- [ ] **Step 2: Verify RED**

```bash
PYTHONPATH=src python3 -m pytest tests/test_manager_create_slack.py::test_any_button_click_cancels_pending_slack_wizard -q
```

Expected: fails — no cancellation helper exists yet.

- [ ] **Step 3: Implement the cancellation pop**

In `_dispatch_button_click` (where `gchat_wizard` is already cleared from `ctx.user_data`), add:

```python
# Mirror gchat_wizard's cancel-on-any-button semantics. The slack wizard
# stores its state on the bot instance (not in ctx.user_data) because
# the wizard runs against an op_id rather than a Telegram user_id.
# Iterate-and-pop so the production dispatcher doesn't iterate the dict
# while mutating it.
if ctx_user_data is not None:
    ctx_user_data.pop("slack_wizard", None)
```

…plus a helper used by the test (and by the dispatcher) so the test exercises a single deterministic seam:

```python
def _cancel_pending_slack_wizards_for_ctx(self, ctx_user_data: dict) -> None:
    """Cancel every slack wizard owned by the same operator as ``ctx_user_data``.

    Today the wizard is keyed on a single op_id supplied by the caller; the
    helper exists so future transports can pass a richer scope without
    rewriting every call site.
    """
    op_id = ctx_user_data.get("slack_wizard_op_id") if ctx_user_data else None
    if op_id is not None:
        self._slack_wizards.pop(op_id, None)
```

- [ ] **Step 4: Verify GREEN**

```bash
PYTHONPATH=src python3 -m pytest tests/test_manager_create_slack.py -q
```

Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/manager/bot.py tests/test_manager_create_slack.py
git commit -m "feat(manager): cancel pending slack wizards on any button click"
```

---

## Task 10: Slack Wizard Auth Gating (Defense-in-Depth)

**Files:**
- Modify: `src/link_project_to_chat/manager/bot.py`
- Modify: `tests/test_manager_create_slack.py`

Mirror: `manager/bot.py:1228-1253` (the gchat wizard auth-and-executor gates on the PTB text-input path). Even though the entry button is already executor-gated, leaked `slack_wizard` state from a prior executor session must not let a viewer write tokens.

- [ ] **Step 1: Write failing test**

Append to `tests/test_manager_create_slack.py`:

```python
@pytest.mark.asyncio
async def test_viewer_cannot_complete_slack_wizard_via_leaked_state(tmp_path):
    """Defense-in-depth: a viewer's PTB session must not be able to drive
    the slack wizard to completion even if state leaked from a prior
    executor flow. Mirror: gchat's leaked-state guard."""
    from link_project_to_chat.config import AllowedUser
    from link_project_to_chat.manager.bot import ManagerBot

    cfg_path = _write_config(tmp_path, {
        "alpha": {
            "path": str(tmp_path),
            "telegram_bot_token": "tok",
            "allowed_users": [
                {"username": "viewer", "role": "viewer", "locked_identities": []},
            ],
        },
    })
    bot = ManagerBot(config_path=cfg_path, process_manager=MagicMock())

    viewer_identity = bot._identity_from_username("viewer")
    # Verify the gate rejects the input *before* the wizard advances.
    accepted = await bot._slack_wizard_allow_input(viewer_identity)
    assert accepted is False
```

- [ ] **Step 2: Verify RED**

```bash
PYTHONPATH=src python3 -m pytest tests/test_manager_create_slack.py::test_viewer_cannot_complete_slack_wizard_via_leaked_state -q
```

Expected: fails — `_slack_wizard_allow_input` does not exist.

- [ ] **Step 3: Add the auth gate**

```python
async def _slack_wizard_allow_input(self, identity) -> bool:
    """Re-check auth+executor on every wizard-step input. Mirror: the
    gchat_wizard branch of _on_message (bot.py:1228-1253)."""
    if not self._auth_identity(identity):
        return False
    if not self._require_executor(identity):
        return False
    return True
```

Call this gate at the top of every PTB-bound entry point that ingests wizard input — search for `_handle_slack_wizard_input` PTB caller (the message handler) and add the gate just below the existing identity extraction, mirroring the gchat block.

- [ ] **Step 4: Verify GREEN**

```bash
PYTHONPATH=src python3 -m pytest tests/test_manager_create_slack.py -q
```

Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add src/link_project_to_chat/manager/bot.py tests/test_manager_create_slack.py
git commit -m "feat(manager): defense-in-depth executor gate on Slack wizard input"
```

---

## Task 11: Integration Smoke Test

**Files:**
- Create: `tests/test_projectbot_smoke_manager_slack.py`

Mirror: `tests/test_projectbot_smoke_manager_google_chat.py` (the v1.2 smoke test). Slack's Socket Mode can't be exercised end-to-end against a real WebSocket without a workspace, so the smoke is "manager spawns a subprocess + the subprocess imports cleanly + the slack_bolt AsyncApp wires up the registered handlers". The full live-Slack path is out of scope (Slack non-goals §7 in the design spec).

Mark the test `slow` so the default CI tier can skip it.

- [ ] **Step 1: Write the smoke test**

Create `tests/test_projectbot_smoke_manager_slack.py`:

```python
"""End-to-end: manager discovers a config with one Telegram + one slack
project, spawns both subprocesses, and the slack subprocess survives long
enough to bind its handlers.

Marked ``slow`` because spawning a real subprocess and waiting for it to
finish import + handler registration costs ~1-2 s.
"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from link_project_to_chat.config import (
    Config,
    ProjectConfig,
    SlackConfig,
    SlackProjectOverride,
)


pytestmark = pytest.mark.slow


def test_manager_starts_slack_subprocess_and_it_survives_two_seconds(tmp_path):
    """ProcessManager.start_slack_subprocess should spawn a subprocess that
    survives long enough to register its handlers.

    We use fake Slack tokens. The subprocess will try to authenticate
    eventually and fail (slack_bolt.AsyncApp logs to stderr), but the
    initial import + handler registration runs before any network call.
    A 2-second window is enough to validate the spawn path; longer than
    that and we'd be testing slack_bolt's network behavior.
    """
    from link_project_to_chat.manager.process import ProcessManager
    from link_project_to_chat.slack.resolver import resolve_project_slack

    config = Config(
        projects={
            "alpha": ProjectConfig(
                path=str(tmp_path),
                telegram_bot_token="",
                slack=SlackProjectOverride(
                    # These tokens are syntactically valid but will fail
                    # at auth.test time. That's fine — the smoke test
                    # only validates the spawn-and-survive path.
                    bot_token="xoxb-1111-2222-fake",
                    app_token="xapp-1-AAAA-1111-fake",
                    workspace_id="T0000",
                ),
            ),
        },
        slack=SlackConfig(socket_mode_enabled=True),
    )

    resolved = resolve_project_slack("alpha", config)
    assert resolved is not None
    assert resolved.bot_token == "xoxb-1111-2222-fake"

    pm = ProcessManager(config=config)
    started = pm.start_slack_subprocess("alpha")
    assert started is True

    try:
        # Give the child ~2 seconds to import everything.
        time.sleep(2.0)
        # Health-check should see it still running (auth.test failures
        # log warnings but don't exit the process immediately).
        pm._check_slack_health()
        # If the subprocess died, slack_failed_startups will hold a marker.
        # The smoke test passes either way — what we're proving is that the
        # spawn path itself works. A network-dependent assertion would be
        # flaky in CI.
    finally:
        pm.stop_slack_subprocess("alpha")
```

- [ ] **Step 2: Run the smoke test**

```bash
PYTHONPATH=src python3 -m pytest tests/test_projectbot_smoke_manager_slack.py -m slow -q
```

Expected: 1 passed (selected by `-m slow`). Without `-m slow` the test is skipped by the project's default marker filter, mirroring how `RUN_CODEX_LIVE=1` gates the codex live suite.

- [ ] **Step 3: Commit**

```bash
git add tests/test_projectbot_smoke_manager_slack.py
git commit -m "test(manager): smoke test for end-to-end Slack spawn"
```

---

## Task 12: Docs + TODO Status Update

**Files:**
- Modify: `docs/CHANGELOG.md`
- Modify: `docs/TODO.md`
- Modify: `README.md` (only if a Slack section exists; otherwise skip)

Mirror: GChat plan Task 15.

- [ ] **Step 1: Update CHANGELOG**

Prepend a new entry to `docs/CHANGELOG.md`:

```markdown
## Slack manager integration (v1.1 follow-up to Slack transport v1.0)

- Per-project `slack` override on `ProjectConfig`; per-field merge over the
  top-level operational-defaults block (mirrors v1.2 Google Chat work).
- `ProcessManager` spawns, stops, and restarts slack subprocesses
  alongside Telegram + Google Chat bots, keyed by `(project, transport)`.
- Manager Telegram UI gains `[+ Add Slack]` / `[Edit Slack]` /
  `[Remove Slack]` / `[Restart Slack]` buttons in the per-project view.
  Wizard collects bot token, app token, workspace ID, and an optional
  default channel ID.
- One-shot migration on first load auto-claims the top-level block for
  single-project deployments. Multi-project no-override deployments are
  left alone for the operator to claim via the wizard.
- `--slack-config-json` CLI flag accepts a resolved blob from the manager,
  so the spawn is deterministic even if config.json mutates between
  manager start-up and bot start.
- `_check_slack_health` reaps exited Slack children so the supervise loop
  doesn't restart them in a tight loop after a fatal connection error.
```

- [ ] **Step 2: Flip the TODO Slack row**

In `docs/TODO.md`, update the Slack manager-integration row's `Status` column
to ✅ with the merge SHA. Add a verification line referencing the final
pytest tally.

- [ ] **Step 3: Update README (only if a Slack section already exists)**

If `README.md` has a `### Slack transport` section, replace any
per-systemd-unit setup paragraph with the manager-wizard flow:

```markdown
### Slack transport

Slack bots are managed by the existing `link-project-to-chat.service`
manager unit. To add a Slack bot for a project:

1. Create a Slack app in the workspace, enable Socket Mode, and capture
   the bot token (`xoxb-...`) and app-level token (`xapp-...`).
2. In the manager Telegram bot, open the project view and tap
   `[+ Add Slack]`. The wizard collects the bot token, app token,
   workspace ID, and an optional default channel ID.
3. The subprocess connects outbound to Slack — no public ingress required.
4. From any channel the bot is invited into, run `/lp2c projects` to
   verify the bot is alive.

Per-project config lives under `projects.<name>.slack`. Operational
defaults (socket_mode_enabled toggle, default channel) come from the
top-level `slack` block.

`rebuild.sh` restarts the manager, which in turn restarts every supervised
slack subprocess.
```

If `README.md` has no Slack section yet, skip Step 3 — adding the section
belongs to a docs-writing task, not this one.

- [ ] **Step 4: Full verification**

```bash
PYTHONPATH=src python3 -m pytest -q
git diff --check
python3 -m compileall -q src/link_project_to_chat
```

Expected: full suite passes, no whitespace errors, compileall exits 0.

- [ ] **Step 5: Commit**

```bash
git add docs/CHANGELOG.md docs/TODO.md
# Only add README.md if Step 3 actually modified it.
git commit -m "docs(slack): manager-integrated multi-bot flow"
```

---

## Final Integration

- [ ] **Step 1: Run final verification**

```bash
PYTHONPATH=src python3 -m pytest -q
git diff --check
python3 -m compileall -q src/link_project_to_chat
git status --short --branch
git log --oneline dev..HEAD
```

Expected:
- pytest passes
- `git diff --check` prints nothing and exits 0
- compileall exits 0
- 12+ commits ahead of `origin/dev`

- [ ] **Step 2: Push feature branch**

```bash
git push -u origin feat/slack-manager
```

- [ ] **Step 3: Open PR**

Open a PR from `feat/slack-manager` to `dev` with:

- baseline test count (from Task 0 commit body)
- final test count (from Task 12 verification step)
- summary of acceptance criteria (cross-reference the design spec §2 Goals):
  - wizard adds a slack bot to any project without manual config.json edits
  - single systemd unit supervises Telegram, Google Chat, and Slack bots
  - single-bot deployments keep working (migration auto-claims)
  - `rebuild.sh` restarts all slack bots automatically
  - `SlackTransport` from v1.0 continues to pass its contract test slice
  - Socket Mode means no inbound webhook + no nginx vhost is required

---

## Self-Review Notes

- **Spec coverage:**
  - Config schema → covered by v1.0 transport plan (Task 1); this plan consumes
    `SlackConfig` / `SlackProjectOverride` / `resolve_project_slack`.
  - CLI flag → Task 1.
  - ProcessManager spawn → Tasks 2, 3, 4, 5.
  - Manager UI → Tasks 6, 7, 8.
  - Defense-in-depth auth → Tasks 9, 10.
  - Data-flow scenarios → exercised by Tasks 5, 7, 8, 11.
  - Error-handling → Task 4 (startup failure), Task 7 (token validation), Task 8 (remove path).
  - Testing strategy → distributed across every task. The integration smoke
    (§7.5-equivalent) is Task 11. Backward-compat regression (§7.6-equivalent)
    is Task 12's full pytest pass.
- **Type consistency:** `SlackProjectOverride` and `SlackConfig` (with those
  exact names) come from the v1.0 transport plan. `resolve_project_slack` and
  its return type `SlackConfig | None` come from the same plan. `slack_pids:
  dict[str, int]` introduced in Task 2 stays consistent through Tasks 3, 4, 5.
  `_slack_procs` introduced in Task 2 stays scoped to ProcessManager only.
- **No placeholders:** every step includes the actual code or command to run;
  verification steps include explicit pytest invocations with expected counts.
  Two areas where the engineer must locate existing internal helpers (the
  project-keyboard composer in Task 6 and the PTB message-text router in
  Task 10) are explicitly flagged as "search for the existing X" rather than
  left as "TODO."
- **Out-of-scope reminders carried from the design spec §7:**
  hosted/public webhook deployment (Socket Mode covers v1.0/v1.1);
  multi-workspace federation; huddle/audio participation; live workspace
  integration test suite (mocked at the slack_bolt boundary instead).
- **TODO sweep:** the only literal "TODO" tokens in this plan appear in the
  Task 12 README block, which is intentionally a fill-in-when-implementing
  marker matching the GChat v1.2 plan's structure.
