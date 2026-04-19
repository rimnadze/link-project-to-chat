from __future__ import annotations

import collections
import logging
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

from .config import load_project_configs, set_project_autostart

logger = logging.getLogger(__name__)


def _default_command_builder(project_name: str, project_config: dict) -> list[str]:
    cmd = ["link-project-to-chat", "start", "--project", project_name]

    permissions = project_config.get("permissions")
    if permissions == "dangerously-skip-permissions":
        cmd.append("--dangerously-skip-permissions")
    elif permissions and permissions != "default":
        cmd.extend(["--permission-mode", permissions])
    model = project_config.get("model")
    if model:
        cmd.extend(["--model", model])
    return cmd


class ProcessManager:
    def __init__(
        self,
        project_config_path: Path | None = None,
        command_builder: Callable[[str, dict], list[str]] | None = None,
    ):
        self._project_config_path = project_config_path
        self._command_builder = command_builder or _default_command_builder
        self._processes: dict[str, subprocess.Popen] = {}
        self._logs: dict[str, collections.deque] = {}
        self._log_threads: dict[str, threading.Thread] = {}

    def _load_projects(self) -> dict[str, dict]:
        if self._project_config_path is not None:
            return load_project_configs(self._project_config_path)
        return load_project_configs()

    def _capture_output(self, name: str, proc: subprocess.Popen) -> None:
        buf = self._logs[name]
        try:
            for raw_line in proc.stdout:
                buf.append(raw_line.decode("utf-8", errors="replace").rstrip("\n"))
        except (ValueError, OSError):
            pass

    def start(self, project_name: str) -> bool:
        if project_name in self._processes and self._processes[project_name].poll() is None:
            return False
        projects = self._load_projects()
        if project_name not in projects:
            return False
        cmd = self._command_builder(project_name, projects[project_name])
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        self._processes[project_name] = proc
        self._logs[project_name] = collections.deque(maxlen=200)
        thread = threading.Thread(target=self._capture_output, args=(project_name, proc), daemon=True)
        thread.start()
        self._log_threads[project_name] = thread
        logger.info("Started %s (pid=%d)", project_name, proc.pid)
        self._set_autostart(project_name, True)
        return True

    def stop(self, project_name: str) -> bool:
        proc = self._processes.get(project_name)
        if not proc or proc.poll() is not None:
            self._processes.pop(project_name, None)
            return False
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        self._processes.pop(project_name, None)
        self._log_threads.pop(project_name, None)
        logger.info("Stopped %s", project_name)
        self._set_autostart(project_name, False)
        return True

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

    def start_all(self) -> int:
        return sum(1 for name in self._load_projects() if self.start(name))

    def stop_all(self) -> int:
        running = {name: proc for name, proc in self._processes.items() if proc.poll() is None}
        for proc in running.values():
            proc.terminate()
        deadline = time.monotonic() + 5
        for proc in running.values():
            remaining = max(0.1, deadline - time.monotonic())
            try:
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        for name in running:
            self._processes.pop(name, None)
            self._log_threads.pop(name, None)
            logger.info("Stopped %s", name)
        return len(running)

    def start_autostart(self) -> int:
        """Start all projects that have autostart=true in config."""
        projects = self._load_projects()
        return sum(1 for name, proj in projects.items() if proj.get("autostart") and self.start(name))

    def _set_autostart(self, project_name: str, value: bool) -> None:
        if self._project_config_path:
            set_project_autostart(project_name, value, self._project_config_path)
        else:
            set_project_autostart(project_name, value)

    def rename(self, old_name: str, new_name: str) -> None:
        for store in (self._processes, self._logs, self._log_threads):
            if old_name in store:
                store[new_name] = store.pop(old_name)
