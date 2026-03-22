from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)

EFFORT_LEVELS = ("low", "medium", "high", "max")


class ClaudeClient:
    def __init__(self, model: str, project_path: Path):
        self.model = model
        self.project_path = project_path
        self.effort: str = "medium"
        self.session_id: str | None = None
        self._proc: subprocess.Popen | None = None
        self._started_at: float | None = None
        self._last_message: str | None = None
        self._last_duration: float | None = None
        self._total_requests: int = 0

    async def chat(self, user_message: str) -> str:
        cmd = [
            "claude", "-p",
            "--model", self.model,
            "--output-format", "json",
            "--effort", self.effort,
            "--dangerously-skip-permissions",
        ]

        if self.session_id:
            cmd.extend(["--resume", self.session_id])

        cmd.append(user_message)

        env = os.environ.copy()
        env.pop("CLAUDECODE", None)
        env.pop("CLAUDE_CODE_ENTRYPOINT", None)

        self._last_message = user_message[:80]
        started_at = time.monotonic()
        self._started_at = started_at
        self._total_requests += 1

        proc = subprocess.Popen(
            cmd,
            cwd=str(self.project_path),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        self._proc = proc
        logger.info("claude subprocess started pid=%s", proc.pid)

        try:
            stdout, stderr = await asyncio.to_thread(proc.communicate)
        finally:
            self._last_duration = time.monotonic() - started_at
            if self._proc is proc:
                self._started_at = None
                self._proc = None

        logger.info("claude pid=%s done, code=%s, %d bytes", proc.pid, proc.returncode, len(stdout))

        if stderr_text := stderr.decode("utf-8", errors="replace").strip():
            logger.warning("claude stderr: %s", stderr_text)

        if proc.returncode != 0:
            return f"Error: {stderr_text or f'exit code {proc.returncode}'}"

        raw = stdout.decode("utf-8", errors="replace").strip()
        if not raw:
            return "[No response]"

        try:
            data = json.loads(raw)
            self.session_id = data.get("session_id", self.session_id)
            return data.get("result", raw)
        except json.JSONDecodeError:
            return raw

    @property
    def status(self) -> dict:
        running = self._proc is not None and self._proc.poll() is None
        info = {
            "running": running,
            "pid": self._proc.pid if running else None,
            "session_id": self.session_id,
            "total_requests": self._total_requests,
            "last_message": self._last_message,
            "last_duration": round(self._last_duration, 1) if self._last_duration else None,
        }
        if running and self._started_at:
            info["elapsed"] = round(time.monotonic() - self._started_at, 1)
        return info

    def cancel(self) -> bool:
        """Kill the running claude process. Returns True if a process was killed."""
        if self._proc and self._proc.poll() is None:
            self._proc.kill()
            return True
        return False
