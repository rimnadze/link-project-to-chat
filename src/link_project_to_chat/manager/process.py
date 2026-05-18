from __future__ import annotations

import collections
import contextlib
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import urllib.parse
from collections.abc import Callable
from pathlib import Path

from .config import load_project_configs, set_project_autostart, set_team_bot_autostart
from ..config import (
    DEFAULT_CONFIG,
    _serialize_google_chat,
    load_config,
    resolve_start_model,
)
from ..google_chat.resolver import resolve_project_google_chat

logger = logging.getLogger(__name__)


def _process_popen_kwargs() -> dict[str, object]:
    if os.name == "nt":
        return {
            "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        }
    return {"start_new_session": True}


def _terminate_process_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return

    if os.name == "nt":
        with contextlib.suppress(OSError):
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        if proc.poll() is None:
            with contextlib.suppress(OSError):
                proc.kill()
    else:
        if getattr(proc, "_kill_process_tree", False):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        else:
            with contextlib.suppress(OSError):
                proc.kill()

    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        proc.wait(timeout=5)


def _export_telethon_session_string(session_path: Path) -> str | None:
    """Export a SQLite Telethon session as a ``StringSession`` string.

    Returns ``None`` if the file is missing, ``telethon`` is not installed, or
    the session has no auth key (i.e. ``/setup`` was never completed). The
    open is read-only with respect to the auth row — Telethon's SQLite schema
    init may CREATE TABLE IF NOT EXISTS, but the session table itself is not
    rewritten, so this is safe to run alongside the manager's own
    ``_telethon_client``.

    Used to seed subprocess relays via ``LP2C_TELETHON_SESSION_STRING`` so
    each subprocess constructs an in-memory ``StringSession`` instead of
    opening the shared SQLite file (which serialises through a single write
    lock at ``connect()`` time and crashes concurrent starts — spec D′).
    """
    if not session_path.exists():
        return None
    try:
        from telethon.sessions import SQLiteSession, StringSession
    except ImportError:
        return None
    try:
        sql = SQLiteSession(str(session_path))
        try:
            if sql.auth_key is None:
                return None
            # StringSession.save returns "" when the underlying session has no
            # auth data — normalize to None so callers can ``or``-chain it.
            return StringSession.save(sql) or None
        finally:
            sql.close()
    except Exception:
        logger.warning(
            "Failed to export Telethon session %s as StringSession",
            session_path,
            exc_info=True,
        )
        return None


def _build_project_bot_env(
    team_name: str | None,
    config_dir: Path,
    *,
    session_string: str | None = None,
) -> dict[str, str]:
    """Build the env dict for a project-bot subprocess.

    Returns a fresh copy of ``os.environ`` so callers can mutate it without
    leaking state into the parent process.

    For team-mode bots, exposes one of two env vars so the project bot can
    construct its own Telethon client and call ``enable_team_relay`` (spec
    #0c). When ``session_string`` is provided (the spec-D′ path), it is
    surfaced as ``LP2C_TELETHON_SESSION_STRING`` and the file-path fallback
    is suppressed — subprocesses then build an in-memory ``StringSession``
    instead of opening the shared SQLite session file, which eliminates the
    ``database is locked`` race when several team bots autostart at once.
    Otherwise, falls back to ``LP2C_TELETHON_SESSION`` pointing at the
    shared on-disk session file when it exists.

    A solo-mode bot (``team_name is None``) never receives either var — it
    has no relay to attach.
    """
    env = os.environ.copy()
    if team_name is not None:
        if session_string:
            env["LP2C_TELETHON_SESSION_STRING"] = session_string
        else:
            session_path = (config_dir / "telethon.session").resolve()
            if session_path.exists():
                env["LP2C_TELETHON_SESSION"] = str(session_path)
    return env


class _AdoptedProc:
    """Lightweight stand-in for subprocess.Popen wrapping a foreign PID.

    ProcessManager builds one of these for each live orphan it finds at
    startup so the rest of the lifecycle (status, stop, terminate) can
    operate uniformly on `self._processes` whether the entry is a Popen
    we spawned or a PID we adopted from a stale pidfile.
    """

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.stdout = None
        self.returncode: int | None = None
        # _terminate_process_tree consults this to decide whether to
        # killpg(getpgid(pid)) — adopted orphans were spawned with
        # start_new_session=True, so killpg is the right hammer.
        self._kill_process_tree = True

    def poll(self) -> int | None:
        try:
            os.kill(self.pid, 0)
        except ProcessLookupError:
            self.returncode = -1
            return -1
        except PermissionError:
            return None  # exists, signal denied — treat as alive
        return None

    def wait(self, timeout: float | None = None) -> int:
        import time as _time

        deadline = _time.monotonic() + timeout if timeout is not None else None
        while True:
            rc = self.poll()
            if rc is not None:
                return rc
            if deadline is not None and _time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired("wait", timeout)
            _time.sleep(0.1)


class ProcessManager:
    def __init__(
        self,
        project_config_path: Path | None = None,
        command_builder: Callable[[str, dict], list[str]] | None = None,
        run_dir: Path | None = None,
    ):
        self._project_config_path = project_config_path
        self._command_builder = command_builder or self._default_command_builder
        self._processes: dict[str, subprocess.Popen | _AdoptedProc] = {}
        self._logs: dict[str, collections.deque] = {}
        self._log_threads: dict[str, threading.Thread] = {}
        # `run/` holds one `<name>.pid` per running project bot. A fresh
        # manager scans this on startup (`reap_orphans`) so it can adopt or
        # kill subprocesses that survived a previous manager crash.
        self._run_dir = run_dir or (
            (project_config_path.parent if project_config_path else DEFAULT_CONFIG.parent)
            / "run"
        )
        # Cached spec-D′ StringSession export. ``None`` means "not yet computed";
        # a falsy str means "tried, no usable session" (caller falls back to
        # path-mode env var). Computed lazily on first ``start_team`` so the
        # SQLite file is opened at most once per manager lifetime.
        self._cached_session_string: str | None = None
        self._session_string_cached: bool = False
        # PID tracking for Google Chat bot subprocesses (one per project that
        # has a google_chat block configured). Keyed by project name; mirrors
        # the telegram-bot tracking in ``self._processes`` but lives in a
        # separate dict because google_chat bots have their own lifecycle
        # (per-project HTTP listener, dedicated start/stop/restart surface).
        # ``google_chat_pids`` is the public observable surface (used by tests
        # and the manager UI to identify which projects are running) while
        # ``_google_chat_procs`` holds the live ``Popen`` handle used by
        # ``stop_google_chat_subprocess`` to ``.terminate()`` and by Task 10
        # (crash detection) to ``.poll()``.
        self.google_chat_pids: dict[str, int] = {}
        self._google_chat_procs: dict[str, subprocess.Popen] = {}
        # Projects whose google_chat subprocess exited non-zero shortly after
        # spawn (typically a port-bind failure). The manager will NOT restart
        # these on its own — a restart loop on an already-bound port is
        # pointless and noisy. Operators see the entry via the manager UI and
        # read the manager log to diagnose. Cleared when
        # start_google_chat_subprocess is called again for the same project
        # (operator retry path).
        self.google_chat_failed_startups: dict[str, str] = {}

    def _base_cli_command(self) -> list[str]:
        cmd = [sys.executable, "-m", "link_project_to_chat.cli"]
        if self._project_config_path is not None:
            cmd.extend(["--config", str(self._project_config_path)])
        return cmd

    def _default_command_builder(self, project_name: str, project_config: dict) -> list[str]:
        cmd = self._base_cli_command()
        cmd.extend(["start", "--project", project_name])

        permissions = project_config.get("permissions")
        if permissions == "dangerously-skip-permissions":
            cmd.append("--dangerously-skip-permissions")
        elif permissions and permissions != "default":
            cmd.extend(["--permission-mode", permissions])
        cfg = load_config(self._project_config_path) if self._project_config_path else load_config()
        # Phase 2+: prefer backend_state[<active_backend>].model. Legacy flat
        # and global defaults are Claude-shaped, so only apply them to Claude
        # projects; otherwise Codex can start with a stale value like
        # ``opus[1m]`` and show it as the current Codex model.
        backend_name = project_config.get("backend") or "claude"
        backend_state = project_config.get("backend_state", {}).get(backend_name, {})
        model = resolve_start_model(
            backend_name,
            backend_model=backend_state.get("model"),
            legacy_claude_model=project_config.get("model"),
            default_model_claude=cfg.default_model_claude,
            default_model=cfg.default_model,
        )
        if model:
            cmd.extend(["--model", model])
        return cmd

    def _load_projects(self) -> dict[str, dict]:
        if self._project_config_path is not None:
            return load_project_configs(self._project_config_path)
        return load_project_configs()

    def _config_dir(self) -> Path:
        """Directory containing the manager's config.json (and telethon.session)."""
        return (self._project_config_path or DEFAULT_CONFIG).parent

    def _capture_output(self, name: str, proc: subprocess.Popen) -> None:
        buf = self._logs[name]
        try:
            for raw_line in proc.stdout:
                line = raw_line.decode("utf-8", errors="replace").rstrip("\n")
                buf.append(line)
                logger.info("[%s] %s", name, line)
        except (ValueError, OSError):
            pass
        try:
            returncode = proc.wait(timeout=0.1)
        except (subprocess.TimeoutExpired, ValueError, OSError):
            return
        self._processes.pop(name, None)
        self._log_threads.pop(name, None)
        # Same-process pidfile cleanup: without this, a normal exit (clean
        # or non-zero) leaves a stale pidfile that the next start() / a
        # follow-on reap_orphans() will misread as a live bot if the pid
        # gets recycled to an unrelated process.
        self._delete_pidfile(name)
        if returncode == 0:
            logger.info("%s exited cleanly", name)
        else:
            logger.warning("%s exited with code %d", name, returncode)

    @staticmethod
    def _safe_pidfile_basename(key: str) -> str:
        """Filesystem-safe encoding of a process key for pidfile naming.

        Uses RFC-3986 percent-encoding with no safe characters, so the result
        only contains [A-Za-z0-9-_.~%]. That keeps team keys (which embed `:`)
        usable on NTFS, where `:` is reserved for alternate data streams. The
        inverse via `_key_from_pidfile_basename` round-trips losslessly.
        """
        return urllib.parse.quote(key, safe="")

    @staticmethod
    def _key_from_pidfile_basename(basename: str) -> str:
        """Inverse of `_safe_pidfile_basename`: the pidfile basename minus
        the `.pid` extension, percent-decoded back to the original key."""
        return urllib.parse.unquote(basename)

    def _pidfile_path(self, name: str) -> Path:
        return self._run_dir / f"{self._safe_pidfile_basename(name)}.pid"

    def _write_pidfile(self, name: str, pid: int) -> None:
        try:
            self._run_dir.mkdir(parents=True, exist_ok=True)
            self._pidfile_path(name).write_text(str(pid))
        except OSError:
            logger.warning("could not write pidfile for %s; orphan reap will skip it", name, exc_info=True)

    def _delete_pidfile(self, name: str) -> None:
        path = self._pidfile_path(name)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("could not delete pidfile %s", path, exc_info=True)

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def start(self, project_name: str) -> bool:
        if project_name in self._processes and self._processes[project_name].poll() is None:
            return False
        # Pidfile fence: if a previous manager left a pidfile pointing at a
        # live process, refuse the start so we don't spawn a duplicate. A
        # call to reap_orphans() promotes that survivor into self._processes
        # and lets the operator decide via stop()/start().
        pidfile = self._pidfile_path(project_name)
        if pidfile.exists():
            try:
                old_pid = int(pidfile.read_text().strip())
            except (OSError, ValueError):
                old_pid = None
            if old_pid and self._pid_alive(old_pid):
                logger.warning(
                    "refusing to start %s: pidfile %s points at live pid %d "
                    "(call reap_orphans first)",
                    project_name, pidfile, old_pid,
                )
                return False
            self._delete_pidfile(project_name)
        projects = self._load_projects()
        if project_name not in projects:
            return False
        cmd = self._command_builder(project_name, projects[project_name])
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            **_process_popen_kwargs(),
        )
        setattr(proc, "_kill_process_tree", True)
        self._processes[project_name] = proc
        self._logs[project_name] = collections.deque(maxlen=200)
        thread = threading.Thread(target=self._capture_output, args=(project_name, proc), daemon=True)
        thread.start()
        self._log_threads[project_name] = thread
        self._write_pidfile(project_name, proc.pid)
        logger.info("Started %s (pid=%d)", project_name, proc.pid)
        self._set_autostart(project_name, True)
        return True

    def _team_command_builder(self, team_name: str, role: str) -> list[str]:
        cmd = self._base_cli_command()
        cmd.extend(["start", "--team", team_name, "--role", role])
        return cmd

    def _telethon_session_string(self) -> str | None:
        """Lazy-export the manager's Telethon session as a StringSession.

        Cached for the manager's lifetime — the on-disk SQLite file is opened
        once, even when ``start_autostart`` spawns several team bots in a row.
        """
        if not self._session_string_cached:
            self._cached_session_string = _export_telethon_session_string(
                self._config_dir() / "telethon.session"
            )
            self._session_string_cached = True
        return self._cached_session_string

    def start_team(self, team_name: str, role: str) -> bool:
        config = load_config(self._project_config_path) if self._project_config_path else load_config()
        if team_name not in config.teams:
            logger.error("Team %s not found in config", team_name)
            return False
        if role not in config.teams[team_name].bots:
            logger.error("Role %s not in team %s", role, team_name)
            return False
        key = f"team:{team_name}:{role}"
        if key in self._processes and self._processes[key].poll() is None:
            return False
        # Pidfile fence (same contract as `start`): refuse if a survivor of
        # a prior manager is still polling Telegram with the team bot's
        # token. reap_orphans() promotes that survivor into self._processes
        # so stop()/start() can decide what to do with it.
        pidfile = self._pidfile_path(key)
        if pidfile.exists():
            try:
                old_pid = int(pidfile.read_text().strip())
            except (OSError, ValueError):
                old_pid = None
            if old_pid and self._pid_alive(old_pid):
                logger.warning(
                    "refusing to start %s: pidfile %s points at live pid %d "
                    "(call reap_orphans first)",
                    key, pidfile, old_pid,
                )
                return False
            self._delete_pidfile(key)
        cmd = self._team_command_builder(team_name, role)
        env = _build_project_bot_env(
            team_name=team_name,
            config_dir=self._config_dir(),
            session_string=self._telethon_session_string(),
        )
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
            **_process_popen_kwargs(),
        )
        setattr(proc, "_kill_process_tree", True)
        self._processes[key] = proc
        self._logs[key] = collections.deque(maxlen=200)
        thread = threading.Thread(target=self._capture_output, args=(key, proc), daemon=True)
        thread.start()
        self._log_threads[key] = thread
        self._write_pidfile(key, proc.pid)
        logger.info("Started team %s/%s (pid=%d)", team_name, role, proc.pid)
        self._set_team_bot_autostart(team_name, role, True)
        return True

    def start_google_chat_subprocess(self, project_name: str) -> bool:
        """Spawn a Google Chat bot subprocess for ``project_name``.

        Returns False if the project has no google_chat configured (no
        per-project override and no meaningful top-level block). Returns True
        after ``Popen`` — does not wait for the child to fully bind the port;
        binding failures surface via the standard non-zero-exit path.

        The merged config is passed to the child as a JSON blob on
        ``--google-chat-config-json`` so the child does not need to re-merge
        from disk — keeps the spawn deterministic even if the operator edits
        config.json between manager start-up and bot start.
        """
        if project_name in self.google_chat_pids:
            logger.warning(
                "google_chat subprocess already running for project %r (pid=%d)",
                project_name, self.google_chat_pids[project_name],
            )
            return False
        # Clear any prior failure marker — operator is retrying.
        self.google_chat_failed_startups.pop(project_name, None)
        config = load_config(self._project_config_path) if self._project_config_path else load_config()
        resolved = resolve_project_google_chat(project_name, config)
        if resolved is None:
            return False

        blob = json.dumps(_serialize_google_chat(resolved))
        cmd = self._base_cli_command()
        cmd.extend([
            "start",
            "--project", project_name,
            "--transport", "google_chat",
            "--google-chat-config-json", blob,
        ])
        proc = subprocess.Popen(cmd, **_process_popen_kwargs())
        setattr(proc, "_kill_process_tree", True)
        self.google_chat_pids[project_name] = proc.pid
        self._google_chat_procs[project_name] = proc
        logger.info("Started google_chat bot %s (pid=%d)", project_name, proc.pid)
        return True

    def stop_google_chat_subprocess(self, project_name: str) -> bool:
        """Terminate the project's google_chat subprocess.

        Routes through ``_terminate_process_tree`` so the kill escalates to
        SIGKILL across the whole process group (uvicorn workers, etc.) and
        does not silently leave a half-stuck server holding the port.

        Returns True if a subprocess was tracked, False if nothing was
        running. Clears both ``google_chat_pids`` and ``_google_chat_procs``
        regardless of whether the underlying process was still alive — the
        bookkeeping is the source of truth for "is this project running"
        and must be left consistent.
        """
        proc = self._google_chat_procs.pop(project_name, None)
        self.google_chat_pids.pop(project_name, None)
        if proc is None:
            return False
        _terminate_process_tree(proc)
        logger.info("Stopped google_chat bot %s", project_name)
        return True

    def restart_google_chat_subprocess(self, project_name: str) -> bool:
        """Stop then start. Returns the ``start_google_chat_subprocess`` result.

        A False stop result just means nothing was running; the start
        result is what callers care about.
        """
        self.stop_google_chat_subprocess(project_name)
        return self.start_google_chat_subprocess(project_name)

    def _check_google_chat_health(self) -> None:
        """Detect google_chat children that exited.

        Walks self._google_chat_procs, calls .poll(), and reaps any child whose
        process has exited. Non-zero exits are also recorded in
        self.google_chat_failed_startups so the manager UI can show the operator
        why the bot stopped.

        Callers (the manager UI, Task 14's smoke test, or any future supervise
        loop) should invoke this on every supervise tick or every UI status read.
        """
        reaped: list[tuple[str, int]] = []
        for name, proc in list(self._google_chat_procs.items()):
            status = proc.poll()
            if status is None:
                continue  # still running
            reaped.append((name, status))
        for name, status in reaped:
            self.google_chat_pids.pop(name, None)
            self._google_chat_procs.pop(name, None)
            if status != 0:
                self.google_chat_failed_startups[name] = (
                    f"exited with status {status} (see manager log)"
                )
                logger.warning(
                    "%s google_chat subprocess exited with code %d — not restarting",
                    name, status,
                )
            else:
                logger.info(
                    "%s google_chat subprocess exited cleanly",
                    name,
                )

    def stop(self, project_name: str) -> bool:
        """Stop a running project / team-bot subprocess.

        Does NOT mutate the project's ``autostart`` flag. The flag is a
        sticky user preference; ``stop()`` semantically means "stop this
        process now", not "never start again automatically".

        Previously this method flipped ``autostart`` to ``False``, which
        meant the PTB shutdown lifecycle (``ManagerBot._post_stop`` →
        ``pm.stop_all()`` → ``stop()`` per project) silently disabled
        autostart on every systemd restart. Operators then had to manually
        click Start in ``/projects`` after every restart. A dedicated
        manager command (or direct config edit) is the right place to
        disable autostart when that's what the operator wants.
        """
        proc = self._processes.get(project_name)
        if not proc or proc.poll() is not None:
            self._processes.pop(project_name, None)
            self._delete_pidfile(project_name)
            return False
        _terminate_process_tree(proc)
        self._processes.pop(project_name, None)
        self._log_threads.pop(project_name, None)
        self._delete_pidfile(project_name)
        logger.info("Stopped %s", project_name)
        return True

    def reap_orphans(self) -> list[str]:
        """Scan pidfiles left behind by a previous manager.

        Each `<name>.pid` either:
          - points at a dead pid → delete the stale file
          - points at a live pid → adopt it into self._processes so a
            subsequent stop() can terminate it cleanly

        Returns the list of names adopted (ordered by filename for test
        determinism). Adopted entries lack stdout/log capture (the original
        manager owned the pipes), so `logs(name)` returns an empty buffer
        until next start().
        """
        adopted: list[str] = []
        if not self._run_dir.exists():
            return adopted
        for pidfile in sorted(self._run_dir.glob("*.pid")):
            name = self._key_from_pidfile_basename(pidfile.stem)
            try:
                pid = int(pidfile.read_text().strip())
            except (OSError, ValueError):
                logger.warning("removing unreadable pidfile %s", pidfile)
                self._delete_pidfile(name)
                continue
            if not self._pid_alive(pid):
                self._delete_pidfile(name)
                continue
            if name in self._processes and self._processes[name].poll() is None:
                # Already managed in this session — nothing to adopt.
                continue
            self._processes[name] = _AdoptedProc(pid)
            self._logs[name] = collections.deque(maxlen=200)
            logger.warning(
                "adopted orphan project bot %s (pid=%d) from prior manager",
                name, pid,
            )
            adopted.append(name)
        return adopted

    def status(self, project_name: str) -> str:
        proc = self._processes.get(project_name)
        if proc and proc.poll() is None:
            return "running"
        self._processes.pop(project_name, None)
        return "stopped"

    def logs(self, project_name: str, n: int = 50) -> str:
        buf = self._logs.get(project_name)
        if not buf:
            return "(no logs)"
        return "\n".join(list(buf)[-n:])

    def list_all(self) -> list[tuple[str, str]]:
        return [(name, self.status(name)) for name in self._load_projects()]

    def list_running(self) -> list[str]:
        """All currently-running keys (project names AND ``team:NAME:ROLE``).

        ``list_all`` only iterates configured projects, so it misses team-bot
        subprocesses that live in self._processes under ``team:NAME:ROLE``
        keys. Callers that need to act on every live bot (e.g. user-mutation
        restart) iterate this instead.
        """
        return [
            name for name, proc in list(self._processes.items())
            if proc.poll() is None
        ]

    def restart(self, key: str) -> bool:
        """Stop and re-spawn a bot, dispatching on key prefix.

        ``team:NAME:ROLE`` keys go through ``start_team(NAME, ROLE)``;
        anything else through ``start(name)``. Returns True iff the start
        leg succeeded after the stop. Callers (notably the manager's
        ``_restart_running_bots_for_user_mutation``) use this so they don't
        have to know how the key is shaped.
        """
        self.stop(key)
        if key.startswith("team:"):
            try:
                _, team_name, role = key.split(":", 2)
            except ValueError:
                logger.warning("malformed team key %r passed to restart()", key)
                return False
            return self.start_team(team_name, role)
        return self.start(key)

    def start_all(self) -> int:
        return sum(1 for name in self._load_projects() if self.start(name))

    def stop_all(self) -> int:
        return sum(1 for name in list(self._processes) if self.stop(name))

    def start_autostart(self) -> int:
        """Start all projects and team bots that have autostart=true in config.

        Teams whose ``group_chat_id`` is still the ``0`` sentinel (group not yet
        captured after ``/create_team``) are skipped — starting them would
        produce a bot with no group to attach to.

        For every project with a resolved google_chat configuration, a Google
        Chat subprocess is also spawned — this does NOT honor the per-project
        ``autostart`` flag (google_chat bots run if configured at all). This
        intentional asymmetry mirrors how the manager's webhook gateway model
        differs from the poll-based Telegram client: a Google Chat bot only
        receives traffic while its HTTP listener is up, so "configured" is
        treated as the operator's standing intent to receive on that port.
        """
        count = 0
        config = load_config(self._project_config_path) if self._project_config_path else load_config()
        for name, proj in self._load_projects().items():
            if proj.get("autostart") and self.start(name):
                count += 1
            # Also spawn a Google Chat bot for any project that has one configured.
            if resolve_project_google_chat(name, config) is not None:
                self.start_google_chat_subprocess(name)
        for team_name, team in config.teams.items():
            if not team.group_chat_id:
                continue
            for role, bot in team.bots.items():
                if bot.autostart and self.start_team(team_name, role):
                    count += 1
        return count

    def _set_autostart(self, project_name: str, value: bool) -> None:
        if self._project_config_path:
            set_project_autostart(project_name, value, self._project_config_path)
        else:
            set_project_autostart(project_name, value)

    def _set_team_bot_autostart(self, team_name: str, role: str, value: bool) -> None:
        if self._project_config_path:
            set_team_bot_autostart(team_name, role, value, self._project_config_path)
        else:
            set_team_bot_autostart(team_name, role, value)

    def rename(self, old_name: str, new_name: str) -> None:
        for store in (self._processes, self._logs, self._log_threads):
            if old_name in store:
                store[new_name] = store.pop(old_name)
