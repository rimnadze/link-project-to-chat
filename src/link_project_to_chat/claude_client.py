from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time
from collections.abc import AsyncGenerator, Callable
from pathlib import Path

from .stream import Error, Result, StreamEvent, parse_stream_line

logger = logging.getLogger(__name__)

EFFORT_LEVELS = ("low", "medium", "high", "max")
MODELS = ("haiku", "sonnet", "opus")
PERMISSION_MODES = ("default", "acceptEdits", "bypassPermissions", "dontAsk", "plan", "auto")
DEFAULT_MODEL = "sonnet"


SYSTEM_PROMPT = "<important>Only make changes or run commands when explicitly asked to modify a specific file or perform a specific task. For questions, analysis, or discussion — answer only, do not act. If you identify something that could be fixed or improved, describe what and why, then ask for approval before doing anything. Do not run, start, stop, or restart anything unless explicitly asked. Do not install packages, run scripts, or restart services unless explicitly asked in the current message.</important>"


class ClaudeClient:
    def __init__(
        self,
        project_path: Path,
        model: str = DEFAULT_MODEL,
        skip_permissions: bool = True,
        permission_mode: str | None = None,
        allowed_tools: list[str] | None = None,
        disallowed_tools: list[str] | None = None,
    ):
        self.model = model
        self.model_display: str | None = None
        self.project_path = project_path
        self.effort: str = "medium"
        self.skip_permissions: bool = skip_permissions
        self.permission_mode: str | None = permission_mode
        self.allowed_tools: list[str] = allowed_tools or []
        self.disallowed_tools: list[str] = disallowed_tools or []
        self.session_id: str | None = None
        self._proc: subprocess.Popen | None = None
        self._started_at: float | None = None
        self._last_message: str | None = None
        self._last_duration: float | None = None
        self._total_requests: int = 0

    async def chat_stream(
        self,
        user_message: str,
        on_proc: Callable[[subprocess.Popen[bytes]], None] | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        cmd = [
            "claude",
            "-p",
            "--model",
            self.model,
            "--output-format",
            "stream-json",
            "--verbose",
            "--effort",
            self.effort,
        ]

        if self.skip_permissions:
            cmd.append("--dangerously-skip-permissions")

        if self.permission_mode:
            cmd.extend(["--permission-mode", self.permission_mode])

        if self.allowed_tools:
            cmd.extend(["--allowedTools", ",".join(self.allowed_tools)])

        if self.disallowed_tools:
            cmd.extend(["--disallowedTools", ",".join(self.disallowed_tools)])

        if self.session_id:
            cmd.extend(["--resume", self.session_id])

        cmd.extend(["--append-system-prompt", SYSTEM_PROMPT])

        cmd.extend(["--", user_message])

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
        if on_proc:
            on_proc(proc)
        logger.info("claude stream subprocess started pid=%s", proc.pid)

        def _read_lines():
            lines = []
            for raw_line in proc.stdout:
                lines.append(raw_line.decode("utf-8", errors="replace").rstrip("\n"))
            return lines

        try:
            all_lines = await asyncio.to_thread(_read_lines)
            for line in all_lines:
                if not line.strip():
                    continue
                for event in parse_stream_line(line):
                    if isinstance(event, Result):
                        self.session_id = event.session_id or self.session_id
                        if event.model:
                            self.model_display = event.model
                    yield event

            stderr_bytes = await asyncio.to_thread(proc.stderr.read)
            await asyncio.to_thread(proc.wait)

            if proc.returncode != 0:
                err = stderr_bytes.decode("utf-8", errors="replace").strip()
                yield Error(message=err or f"exit code {proc.returncode}")
        finally:
            self._last_duration = time.monotonic() - started_at
            if self._proc is proc:
                self._started_at = None
                self._proc = None
            logger.info("claude stream pid=%s done, code=%s", proc.pid, proc.returncode)

    async def chat(self, user_message: str, on_proc=None) -> str:
        result_text = ""
        async for event in self.chat_stream(user_message, on_proc=on_proc):
            if isinstance(event, Result):
                result_text = event.text
            elif isinstance(event, Error):
                return f"Error: {event.message}"
        return result_text or "[No response]"

    @property
    def status(self) -> dict:
        running = self._proc is not None and self._proc.poll() is None
        info = {
            "running": running,
            "pid": self._proc.pid if running else None,
            "session_id": self.session_id,
            "total_requests": self._total_requests,
            "last_message": self._last_message,
            "last_duration": round(self._last_duration, 1)
            if self._last_duration
            else None,
        }
        if running and self._started_at:
            info["elapsed"] = round(time.monotonic() - self._started_at, 1)
        return info

    def cancel(self) -> bool:
        if self._proc and self._proc.poll() is None:
            self._proc.kill()
            return True
        return False
