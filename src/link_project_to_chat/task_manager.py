from __future__ import annotations

import asyncio
import collections
import enum
import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from .claude_client import ClaudeClient
from .stream import Error, Result, StreamEvent, TextDelta, ThinkingDelta

logger = logging.getLogger(__name__)


class TaskType(enum.Enum):
    CLAUDE = "claude"
    COMMAND = "command"


class TaskStatus(enum.Enum):
    WAITING = "waiting"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Task:
    id: int
    chat_id: int
    message_id: int
    type: TaskType
    input: str
    name: str
    status: TaskStatus = TaskStatus.WAITING
    result: str | None = None
    error: str | None = None
    exit_code: int | None = None
    created_at: float = field(default_factory=time.monotonic)
    started_at: float | None = None
    finished_at: float | None = None
    _compact: bool = field(default=False, repr=False)
    _proc: subprocess.Popen | None = field(default=None, repr=False)
    _asyncio_task: asyncio.Task | None = field(default=None, repr=False)
    _log: collections.deque = field(
        default_factory=lambda: collections.deque(maxlen=100), repr=False
    )

    def tail(self, n: int = 10) -> str:
        return "\n".join(list(self._log)[-n:])

    @property
    def elapsed(self) -> float | None:
        if self.started_at is None:
            return None
        end = self.finished_at or time.monotonic()
        return round(end - self.started_at, 1)

    @property
    def elapsed_human(self) -> str | None:
        s = self.elapsed
        if s is None:
            return None
        s = int(s)
        if s < 60:
            return f"{s}s"
        m, s = divmod(s, 60)
        if m < 60:
            return f"{m}m {s}s" if s else f"{m}m"
        h, m = divmod(m, 60)
        return f"{h}h {m}m" if m else f"{h}h"

    def cancel(self) -> bool:
        if self.status == TaskStatus.WAITING:
            self.status = TaskStatus.CANCELLED
            self.finished_at = time.monotonic()
            return True
        if self.status == TaskStatus.RUNNING:
            if self._proc and self._proc.poll() is None:
                self._proc.kill()
            self.status = TaskStatus.CANCELLED
            self.finished_at = time.monotonic()
            if self._asyncio_task and not self._asyncio_task.done():
                self._asyncio_task.cancel()
            return True
        return False


OnTaskEvent = Callable[[Task], Awaitable[None]]


class TaskManager:
    def __init__(
        self,
        project_path: Path,
        on_complete: OnTaskEvent,
        on_task_started: OnTaskEvent,
        on_stream_event: Callable[[Task, StreamEvent], Awaitable[None]] | None = None,
        skip_permissions: bool = False,
        permission_mode: str | None = None,
        allowed_tools: list[str] | None = None,
        disallowed_tools: list[str] | None = None,
    ):
        self.project_path = project_path
        self._on_complete = on_complete
        self._on_task_started = on_task_started
        self._on_stream_event = on_stream_event
        self._next_id = 1
        self._tasks: dict[int, Task] = {}
        self._claude = ClaudeClient(
            project_path,
            skip_permissions=skip_permissions,
            permission_mode=permission_mode,
            allowed_tools=allowed_tools,
            disallowed_tools=disallowed_tools,
        )

    @property
    def claude(self) -> ClaudeClient:
        return self._claude

    def submit_claude(self, chat_id: int, message_id: int, prompt: str) -> Task:
        task = Task(
            id=self._next_id,
            chat_id=chat_id,
            message_id=message_id,
            type=TaskType.CLAUDE,
            input=prompt,
            name=prompt[:40],
        )
        self._next_id += 1
        self._tasks[task.id] = task
        task._asyncio_task = asyncio.create_task(self._exec_claude(task))
        return task

    def submit_compact(self, chat_id: int, message_id: int) -> Task:
        task = Task(
            id=self._next_id,
            chat_id=chat_id,
            message_id=message_id,
            type=TaskType.CLAUDE,
            input="/compact",
            name="compact",
            _compact=True,
        )
        self._next_id += 1
        self._tasks[task.id] = task
        task._asyncio_task = asyncio.create_task(self._exec_claude(task))
        return task

    def run_command(
        self, chat_id: int, message_id: int, command: str, name: str | None = None
    ) -> Task:
        task = Task(
            id=self._next_id,
            chat_id=chat_id,
            message_id=message_id,
            type=TaskType.COMMAND,
            input=command,
            name=name or command.split()[0] if command else f"task-{self._next_id}",
            status=TaskStatus.RUNNING,
            started_at=time.monotonic(),
        )
        self._next_id += 1
        self._tasks[task.id] = task
        task._asyncio_task = asyncio.create_task(self._exec_command(task))
        return task

    async def _exec_claude(self, task: Task) -> None:
        task.status = TaskStatus.RUNNING
        task.started_at = time.monotonic()
        await self._safe_callback(self._on_task_started, task)
        try:
            if task._compact:
                task.result = await self._do_compact()
            else:
                collected_text: list[str] = []
                collected_thinking: list[str] = []
                async for event in self._claude.chat_stream(
                    task.input,
                    on_proc=lambda p: setattr(task, "_proc", p),
                ):
                    if self._on_stream_event:
                        try:
                            await self._on_stream_event(task, event)
                        except Exception:
                            logger.exception(
                                "stream event callback failed for task #%d", task.id
                            )
                    if isinstance(event, TextDelta):
                        collected_text.append(event.text)
                    elif isinstance(event, ThinkingDelta):
                        collected_thinking.append(event.text)
                    elif isinstance(event, Result):
                        task.result = event.text
                    elif isinstance(event, Error):
                        raise RuntimeError(event.message)
                if task.result is None:
                    task.result = "".join(collected_text) or "".join(collected_thinking) or "[No response]"
            task.status = TaskStatus.DONE
        except asyncio.CancelledError:
            if task._proc and task._proc.poll() is None:
                task._proc.kill()
            task.status = TaskStatus.CANCELLED
        except Exception as e:
            logger.exception("Claude task #%d failed", task.id)
            task.status = TaskStatus.FAILED
            task.error = str(e)
        finally:
            task.finished_at = time.monotonic()
        if task.status != TaskStatus.CANCELLED:
            await self._safe_callback(self._on_complete, task)

    COMPACT_PROMPT = (
        "Summarize our entire conversation concisely. Include:\n"
        "- Key decisions and architectural choices\n"
        "- What was implemented or changed\n"
        "- Current project state\n"
        "- Pending items or next steps\n"
        "This summary will seed a new session to continue our work."
    )

    async def _do_compact(self) -> str:
        if not self._claude.session_id:
            return "No active session to compact."
        summary = await self._claude.chat(self.COMPACT_PROMPT)
        self._claude.session_id = None
        await self._claude.chat(
            f"Continue from this context summary of our previous session:\n\n{summary}"
        )
        return summary

    async def _exec_command(self, task: Task) -> None:
        await self._safe_callback(self._on_task_started, task)
        proc = subprocess.Popen(
            task.input,
            shell=True,
            cwd=str(self.project_path),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        task._proc = proc
        logger.info("task #%d started pid=%d: %s", task.id, proc.pid, task.input)

        stdout_lines: list[str] = []
        stderr_lines: list[str] = []

        def _read_stream(stream, lines: list[str]):
            for raw_line in stream:
                line = raw_line.decode("utf-8", errors="replace").rstrip("\n")
                lines.append(line)
                task._log.append(line)

        try:
            out_fut = asyncio.to_thread(_read_stream, proc.stdout, stdout_lines)
            err_fut = asyncio.to_thread(_read_stream, proc.stderr, stderr_lines)
            await asyncio.gather(out_fut, err_fut)
            await asyncio.to_thread(proc.wait)
        except asyncio.CancelledError:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            task.status = TaskStatus.CANCELLED
            task.finished_at = time.monotonic()
            return

        task.finished_at = time.monotonic()
        task._proc = None

        if task.status == TaskStatus.CANCELLED:
            return

        task.result = "\n".join(stdout_lines)
        task.error = "\n".join(stderr_lines) or None
        task.exit_code = proc.returncode
        task.status = TaskStatus.DONE if proc.returncode == 0 else TaskStatus.FAILED
        logger.info(
            "task #%d %s in %.1fs (exit %d)",
            task.id,
            task.status.value,
            task.elapsed,
            proc.returncode,
        )

        await self._safe_callback(self._on_complete, task)

    async def _safe_callback(self, cb: OnTaskEvent, task: Task) -> None:
        try:
            await cb(task)
        except Exception:
            logger.exception("callback failed for task #%d", task.id)

    def get(self, task_id: int) -> Task | None:
        return self._tasks.get(task_id)

    def find_by_message(self, message_id: int) -> list[Task]:
        return [
            t
            for t in self._tasks.values()
            if t.message_id == message_id
            and t.status in (TaskStatus.WAITING, TaskStatus.RUNNING)
        ]

    def list_tasks(self, chat_id: int | None = None, limit: int = 20) -> list[Task]:
        tasks = list(self._tasks.values())
        if chat_id is not None:
            tasks = [t for t in tasks if t.chat_id == chat_id]
        return sorted(tasks, key=lambda t: t.id, reverse=True)[:limit]

    def cancel(self, task_id: int) -> bool:
        task = self._tasks.get(task_id)
        if not task:
            return False
        return task.cancel()

    def cancel_all(self) -> int:
        return sum(1 for t in list(self._tasks.values()) if t.cancel())

    @property
    def running_count(self) -> int:
        return sum(1 for t in self._tasks.values() if t.status == TaskStatus.RUNNING)

    @property
    def waiting_count(self) -> int:
        return sum(1 for t in self._tasks.values() if t.status == TaskStatus.WAITING)
