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

logger = logging.getLogger(__name__)

NOTIFY_AFTER = 30


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
    notified: bool = False
    _proc: subprocess.Popen | None = field(default=None, repr=False)
    _asyncio_task: asyncio.Task | None = field(default=None, repr=False)
    _log: collections.deque = field(default_factory=lambda: collections.deque(maxlen=100), repr=False)

    def tail(self, n: int = 10) -> str:
        return "\n".join(list(self._log)[-n:])

    @property
    def elapsed(self) -> float | None:
        if self.started_at is None:
            return None
        end = self.finished_at or time.monotonic()
        return round(end - self.started_at, 1)

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
    def __init__(self, model: str, project_path: Path,
                 on_complete: OnTaskEvent, on_long_running: OnTaskEvent,
                 on_claude_started: OnTaskEvent):
        self.project_path = project_path
        self._on_complete = on_complete
        self._on_long_running = on_long_running
        self._on_claude_started = on_claude_started
        self._next_id = 1
        self._tasks: dict[int, Task] = {}
        self._claude = ClaudeClient(model, project_path)
        self._claude_queue: asyncio.Queue[Task] = asyncio.Queue()
        self._claude_worker: asyncio.Task | None = None

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
        self._claude_queue.put_nowait(task)
        self._ensure_claude_worker()
        return task

    def run_command(self, chat_id: int, message_id: int, command: str,
                    name: str | None = None) -> Task:
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

    # -- Claude queue (sequential) --

    def _ensure_claude_worker(self) -> None:
        if self._claude_worker is None or self._claude_worker.done():
            self._claude_worker = asyncio.create_task(self._process_claude_queue())

    async def _process_claude_queue(self) -> None:
        while True:
            task = await self._claude_queue.get()
            if task.status == TaskStatus.CANCELLED:
                self._claude_queue.task_done()
                continue

            task.status = TaskStatus.RUNNING
            task.started_at = time.monotonic()

            notify_handle = asyncio.get_event_loop().call_later(
                NOTIFY_AFTER, lambda t=task: asyncio.create_task(self._notify_long(t))
            )

            await self._safe_callback(self._on_claude_started, task)

            try:
                task.result = await self._claude.chat(task.input)
                task.status = TaskStatus.DONE
            except asyncio.CancelledError:
                task.status = TaskStatus.CANCELLED
            except Exception as e:
                logger.exception("Claude task #%d failed", task.id)
                task.status = TaskStatus.FAILED
                task.error = str(e)
            finally:
                task.finished_at = time.monotonic()
                notify_handle.cancel()
                self._claude_queue.task_done()

            if task.status != TaskStatus.CANCELLED:
                await self._safe_callback(self._on_complete, task)

    # -- Command execution (parallel) --

    async def _exec_command(self, task: Task) -> None:
        proc = subprocess.Popen(
            task.input, shell=True,
            cwd=str(self.project_path),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        task._proc = proc
        logger.info("task #%d started pid=%d: %s", task.id, proc.pid, task.input)

        notify_handle = asyncio.get_event_loop().call_later(
            NOTIFY_AFTER, lambda: asyncio.create_task(self._notify_long(task))
        )

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
            notify_handle.cancel()
            return

        notify_handle.cancel()
        task.finished_at = time.monotonic()
        task._proc = None

        if task.status == TaskStatus.CANCELLED:
            return

        task.result = "\n".join(stdout_lines)
        task.error = "\n".join(stderr_lines) or None
        task.exit_code = proc.returncode
        task.status = TaskStatus.DONE if proc.returncode == 0 else TaskStatus.FAILED
        logger.info("task #%d %s in %.1fs (exit %d)",
                     task.id, task.status.value, task.elapsed, proc.returncode)

        await self._safe_callback(self._on_complete, task)

    # -- Shared helpers --

    async def _notify_long(self, task: Task) -> None:
        if task.status == TaskStatus.RUNNING and not task.notified:
            task.notified = True
            await self._safe_callback(self._on_long_running, task)

    async def _safe_callback(self, cb: OnTaskEvent, task: Task) -> None:
        try:
            await cb(task)
        except Exception:
            logger.exception("callback failed for task #%d", task.id)

    def get(self, task_id: int) -> Task | None:
        return self._tasks.get(task_id)

    def find_by_message(self, message_id: int) -> list[Task]:
        return [
            t for t in self._tasks.values()
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
        if task.type == TaskType.CLAUDE and task.status == TaskStatus.RUNNING:
            self._claude.cancel()
        return task.cancel()

    def cancel_all(self) -> int:
        count = 0
        for task in list(self._tasks.values()):
            if task.type == TaskType.CLAUDE and task.status == TaskStatus.RUNNING:
                self._claude.cancel()
            if task.cancel():
                count += 1
        return count

    @property
    def running_count(self) -> int:
        return sum(1 for t in self._tasks.values() if t.status == TaskStatus.RUNNING)

    @property
    def waiting_count(self) -> int:
        return sum(1 for t in self._tasks.values() if t.status == TaskStatus.WAITING)
