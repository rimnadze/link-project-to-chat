# Sandbox / Directory Jailing Plan

## Goal

Optionally restrict all user-triggered execution (claude subprocess + `/run` commands) to the project directory. Applied at the two execution points inside the project bot, not at the process level.

## Jail levels

| Level | Applied by | Covers | Status |
|---|---|---|---|
| Execution-point jail | `claude_client.py` + `task_manager.py` | `claude` subprocess + `/run` commands | **This plan** |
| Whole-bot jail (manager wraps `start`) | `manager/process.py` | entire bot process tree | Future / optional extension |

Execution-point jailing covers all user-reachable paths. The Python bot process itself is unjailed but trusted — its own code is not user-executable.

---

## 1. New module: `sandbox.py`

`src/link_project_to_chat/sandbox.py`

```python
def available() -> bool
    # macOS: check `sandbox-exec` on PATH
    # Linux: check `bwrap` on PATH + user namespaces available
    # Other: False

def wrap(cmd: list[str], project_dir: str) -> list[str]
    # macOS: ["sandbox-exec", "-p", <profile>, *cmd]
    # Linux: ["bwrap", "--ro-bind", "/usr", ..., "--bind", project_dir, project_dir, "--chdir", project_dir, *cmd]
    # Unavailable: return cmd unchanged, log warning once
```

### macOS Seatbelt profile

```scheme
(version 1)
(allow default)
(deny file-write*)
(allow file-write*
  (subpath "/path/to/project")
  (subpath "/private/tmp")
  (subpath "/private/var/folders")
  (literal "/dev/null"))
```

- Reads allowed everywhere (system paths, ~/.claude/, etc.)
- Writes restricted to project dir + temp
- Network allowed by default

### Linux bwrap invocation

```
bwrap
  --ro-bind /usr /usr
  --ro-bind /etc /etc
  --ro-bind /lib /lib           # if exists
  --ro-bind /lib64 /lib64       # if exists
  --ro-bind /opt /opt           # homebrew / nix paths
  --ro-bind /proc /proc
  --dev /dev
  --bind <project_dir> <project_dir>
  --tmpfs /tmp
  --unshare-all
  --share-net
  --chdir <project_dir>
  -- <cmd...>
```

- Graceful fallback if user namespaces unavailable (log warning, run unjailed)

---

## 2. Config changes

### `ProjectConfig` (config.py)

Add:
```python
jailed: bool = True
```

Persisted as `"jailed": true/false` in the project entry in `config.json`.

### `Config` (config.py)

Add:
```python
projects_dir: str | None = None
```

Persisted as `"projects_dir"` in `config.json`. Used by manager to auto-create project subdirectories.

### `load_config` / `save_config`

Round-trip both new fields. `jailed` defaults to `True` when absent from JSON (new field, safe default).

---

## 3. Execution point: `claude_client.py`

`ClaudeClient.__init__` gains `jailed: bool = True`.

In `chat_stream()`, before `subprocess.Popen`:
```python
if self.jailed:
    cmd = sandbox.wrap(cmd, str(self.project_path))
```

---

## 4. Execution point: `task_manager.py`

`TaskManager.__init__` gains `jailed: bool = True`. Passes it to `ClaudeClient`.

In `_exec_command()`, before `subprocess.Popen`:
```python
if self.jailed:
    parts = sandbox.wrap(shlex.split(task.input), str(self.project_path))
else:
    parts = shlex.split(task.input)
```

---

## 5. Propagation through the stack

```
ProjectConfig.jailed
  -> run_bot(jailed=...)          [bot.py]
    -> ProjectBot.__init__
      -> TaskManager(jailed=...)
        -> ClaudeClient(jailed=...)
          -> sandbox.wrap() in chat_stream()
        -> sandbox.wrap() in _exec_command()
```

---

## 6. CLI

### `projects add`

Add `--jail/--no-jail` flag, default `True`:
```
--jail / --no-jail    Jail executions to project directory (default: on)
```
Stored as `jailed` in project config entry.

### `start`

Add `--jail/--no-jail` flag, default `None` (meaning: use project config value):
```
--jail / --no-jail    Override project jail setting for this run
```

### `configure`

Add `--projects-dir PATH`:
```
--projects-dir PATH   Default directory for manager-created projects
```

---

## 7. Manager bot — add project wizard

If `projects_dir` is configured in config:
- Skip the "enter path" prompt
- Auto-create `{projects_dir}/{name}/` (mkdir)
- Use that path automatically

If `projects_dir` is not configured:
- Keep existing "enter path" prompt

Both paths:
- Add jail toggle step at the end: "Jail executions to project directory? (yes/skip=yes)"
- Stored as `jailed: true/false` in project config

### Manager bot UI

- Project detail view: show jail status line
- Edit flow: `jailed` as boolean toggle button (not text input)
- `_BUTTON_EDIT_FIELDS`: exclude `jailed` from text-input edit fields; handle as toggle callback

---

## 8. `manager/process.py`

When spawning `link-project-to-chat start --project NAME`:
- If `ProjectConfig.jailed` -> append `--jail` to subprocess args
- If not -> append `--no-jail`

This ensures the project config value is respected when manager starts bots.

---

## 9. Tests

- `tests/test_sandbox.py`: mock `shutil.which`; test `available()`, `wrap()` output on macOS path, Linux path, unavailable path
- `tests/test_cli.py`: test `projects add --no-jail` stores `jailed: false`; default stores `jailed: true`
- `tests/test_config.py`: test `jailed` and `projects_dir` round-trip in load/save

---

## Open questions

1. On Linux, if `bwrap` is absent but `landlock` PyPI package is installed, use landlock as secondary option?
2. Should `start --jail` permanently update stored `ProjectConfig.jailed` or only apply for that run? (Recommendation: runtime-only, don't persist)
3. Should `configure --projects-dir` create the directory immediately or lazily? (Recommendation: lazily, on first project add)
4. `~/.claude/` writes — allow or block? Claude CLI may write settings/cache there. Blocking may cause claude to misbehave. (Recommendation: allow reads, block writes to `~/.claude/` — or allow writes and accept slight jail weakening)
