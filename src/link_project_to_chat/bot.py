from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import Config, clear_session, load_sessions, save_session
from .formatting import md_to_telegram, split_html, strip_html
from .task_manager import Task, TaskManager, TaskStatus, TaskType

logger = logging.getLogger(__name__)

COMMANDS = [
    ("run", "Run a background command"),
    ("tasks", "List tasks"),
    ("log", "Show task output"),
    ("cancel", "Cancel a task"),
    ("status", "Bot status"),
    ("reset", "Clear Claude session"),
]


class ProjectBot:
    def __init__(self, name: str, path: Path, token: str, model: str,
                 allowed_username: str):
        self.name = name
        self.path = path.resolve()
        self.token = token
        self.model = model
        self.allowed_username = allowed_username
        self._started_at = time.monotonic()
        self._app = None
        self._typing_tasks: dict[int, asyncio.Task] = {}
        self.task_manager = TaskManager(
            model=model,
            project_path=self.path,
            on_complete=self._on_task_complete,
            on_long_running=self._on_task_long_running,
            on_claude_started=self._on_claude_started,
        )

    def _auth(self, user) -> bool:
        if not self.allowed_username:
            return True
        return (user.username or "").lower() == self.allowed_username

    # -- Task callbacks --

    async def _on_claude_started(self, task: Task) -> None:
        chat = await self._app.bot.get_chat(task.chat_id)
        self._typing_tasks[task.id] = asyncio.create_task(self._keep_typing(chat))

    async def _on_task_complete(self, task: Task) -> None:
        typing = self._typing_tasks.pop(task.id, None)
        if typing:
            typing.cancel()

        if task.type == TaskType.CLAUDE:
            text = task.result if task.status == TaskStatus.DONE else f"Error: {task.error}"
            if self.task_manager.claude.session_id:
                save_session(self.name, self.task_manager.claude.session_id)
            await self._send_to_chat(task.chat_id, text, reply_to=task.message_id)
        else:
            icon = "+" if task.status == TaskStatus.DONE else "!"
            header = f"[{icon} #{task.id} {task.name} | {task.elapsed}s | exit {task.exit_code}]"
            output = (task.result or "").rstrip() or (task.error or "").rstrip() or "(no output)"
            if len(output) > 3000:
                output = output[:3000] + "\n... (truncated, use /log)"
            await self._send_to_chat(task.chat_id, f"{header}\n\n{output}")

    async def _on_task_long_running(self, task: Task) -> None:
        if task.type == TaskType.CLAUDE:
            text = f"#{task.id} still running... ({task.elapsed}s)"
        else:
            text = f"#{task.id} ({task.name}) still running... ({task.elapsed}s)"
        await self._send_to_chat(task.chat_id, text)

    # -- Command handlers --

    async def _on_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update.effective_user):
            return await update.effective_message.reply_text("Unauthorized.")
        cmd_list = "\n".join(f"/{name} - {desc}" for name, desc in COMMANDS)
        await update.effective_message.reply_text(
            f"Project: {self.name}\nPath: {self.path}\nModel: {self.model}\n\n"
            f"Send a message to chat with Claude.\n{cmd_list}"
        )

    async def _on_text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if not msg:
            return
        if not self._auth(update.effective_user):
            return await msg.reply_text("Unauthorized.")

        # If edited message, cancel previous task for this message_id
        for prev in self.task_manager.find_by_message(msg.message_id):
            self.task_manager.cancel(prev.id)
            typing = self._typing_tasks.pop(prev.id, None)
            if typing:
                typing.cancel()

        self.task_manager.submit_claude(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            prompt=msg.text,
        )

    async def _on_run(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update.effective_user):
            return
        if not ctx.args:
            return await update.effective_message.reply_text("Usage: /run <command>")
        command = " ".join(ctx.args)
        self.task_manager.run_command(
            chat_id=update.effective_chat.id,
            message_id=update.effective_message.message_id,
            command=command,
        )

    async def _on_tasks(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/tasks [all] - list active tasks (or all with 'all')."""
        if not self._auth(update.effective_user):
            return
        show_all = ctx.args and ctx.args[0].lower() == "all"
        tasks = self.task_manager.list_tasks(chat_id=update.effective_chat.id)
        if not show_all:
            tasks = [t for t in tasks if t.status in (TaskStatus.WAITING, TaskStatus.RUNNING)]
        if not tasks:
            return await update.effective_message.reply_text("No active tasks.")

        icons = {
            TaskStatus.WAITING: "~",
            TaskStatus.RUNNING: ">",
            TaskStatus.DONE: "+",
            TaskStatus.FAILED: "!",
            TaskStatus.CANCELLED: "x",
        }
        lines = []
        for t in tasks:
            icon = icons.get(t.status, "?")
            elapsed = f" {t.elapsed}s" if t.elapsed else ""
            label = t.name if t.type == TaskType.COMMAND else t.input[:50]
            lines.append(f"{icon} #{t.id} [{t.type.value}]{elapsed} {label}")
        await update.effective_message.reply_text("\n".join(lines))

    async def _on_log(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update.effective_user):
            return
        if not ctx.args:
            return await update.effective_message.reply_text("Usage: /log <task_id>")
        try:
            task_id = int(ctx.args[0])
        except ValueError:
            return await update.effective_message.reply_text("Invalid task ID.")

        task = self.task_manager.get(task_id)
        if not task:
            return await update.effective_message.reply_text(f"Task #{task_id} not found.")

        lines = [f"Task #{task.id} | {task.type.value} | {task.status.value}"]
        if task.elapsed is not None:
            lines[0] += f" | {task.elapsed}s"
        lines.append(f"Input: {task.input[:200]}")

        if task.type == TaskType.COMMAND and task.exit_code is not None:
            lines.append(f"Exit: {task.exit_code}")

        if task.status == TaskStatus.RUNNING:
            tail = task.tail(10)
            if tail:
                lines.append(f"\n{tail}")
            else:
                lines.append(f"\nRunning for {task.elapsed}s...")
        elif task.result:
            lines.append(f"\n{task.result}")
        elif task.error:
            lines.append(f"\nError: {task.error}")
        elif task.status == TaskStatus.WAITING:
            lines.append("\nWaiting...")

        await self._send_to_chat(
            update.effective_chat.id, "\n".join(lines),
            reply_to=update.effective_message.message_id,
        )

    async def _on_cancel(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update.effective_user):
            return

        if not ctx.args:
            tasks = self.task_manager.list_tasks(chat_id=update.effective_chat.id)
            running = [t for t in tasks if t.status == TaskStatus.RUNNING]
            if running:
                t = running[0]
                self.task_manager.cancel(t.id)
                return await update.effective_message.reply_text(f"#{t.id} cancelled.")
            return await update.effective_message.reply_text("Nothing running.")

        arg = ctx.args[0].lower()
        if arg == "all":
            count = self.task_manager.cancel_all()
            msg = f"Cancelled {count} task(s)." if count else "Nothing to cancel."
        else:
            try:
                task_id = int(arg)
            except ValueError:
                return await update.effective_message.reply_text("Usage: /cancel [id|all]")
            msg = (f"#{task_id} cancelled."
                   if self.task_manager.cancel(task_id)
                   else f"#{task_id} not found or already finished.")
        await update.effective_message.reply_text(msg)

    async def _on_reset(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update.effective_user):
            return
        self.task_manager.cancel_all()
        self.task_manager.claude.session_id = None
        clear_session(self.name)
        await update.effective_message.reply_text("Session reset.")

    async def _on_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update.effective_user):
            return

        uptime = time.monotonic() - self._started_at
        h, rem = divmod(int(uptime), 3600)
        m, s = divmod(rem, 60)

        st = self.task_manager.claude.status
        lines = [
            f"Project: {self.name}",
            f"Path: {self.path}",
            f"Model: {self.model}",
            f"Uptime: {h}h {m}m {s}s",
            f"Session: {st['session_id'] or 'none'}",
            f"Claude: {'RUNNING' if st['running'] else 'idle'}",
            f"Running tasks: {self.task_manager.running_count}",
            f"Waiting: {self.task_manager.waiting_count}",
        ]
        await update.effective_message.reply_text("\n".join(lines))

    # -- Helpers --

    async def _send_to_chat(self, chat_id: int, text: str,
                            reply_to: int | None = None) -> None:
        text = text or "[No output]"
        html = md_to_telegram(text)
        for chunk in split_html(html):
            try:
                await self._app.bot.send_message(
                    chat_id, chunk, parse_mode="HTML",
                    reply_to_message_id=reply_to)
            except Exception:
                logger.debug("HTML send failed, falling back to plain", exc_info=True)
                plain = strip_html(chunk)
                await self._app.bot.send_message(
                    chat_id, plain[:4096] if len(plain) > 4096 else plain,
                    reply_to_message_id=reply_to)

    @staticmethod
    async def _keep_typing(chat) -> None:
        try:
            while True:
                try:
                    await chat.send_action(ChatAction.TYPING)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.debug("typing indicator failed", exc_info=True)
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            pass

    @staticmethod
    async def _on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        logger.error("Update error: %s", ctx.error)

    async def _post_init(self, app) -> None:
        await app.bot.set_my_commands(COMMANDS)

    def build(self):
        app = ApplicationBuilder().token(self.token).concurrent_updates(True).post_init(self._post_init).build()
        self._app = app
        handlers = {
            "start": self._on_start,
            "run": self._on_run,
            "tasks": self._on_tasks,
            "log": self._on_log,
            "cancel": self._on_cancel,
            "reset": self._on_reset,
            "status": self._on_status,
        }
        for name, handler in handlers.items():
            app.add_handler(CommandHandler(name, handler))
        text_filter = (filters.UpdateType.MESSAGE | filters.UpdateType.EDITED_MESSAGE) & filters.TEXT & ~filters.COMMAND
        app.add_handler(MessageHandler(text_filter, self._on_text))
        app.add_error_handler(self._on_error)
        return app


def run_bot(name: str, path: Path, token: str, model: str, username: str,
            session_id: str | None = None) -> None:
    if session_id:
        save_session(name, session_id)
    bot = ProjectBot(name, path, token, model, username)
    bot.task_manager.claude.session_id = session_id or load_sessions().get(name)
    app = bot.build()
    logger.info("Bot '%s' started at %s (model=%s)", name, path, model)
    app.run_polling()


def run_bots(config: Config) -> None:
    if len(config.projects) == 1:
        name, proj = next(iter(config.projects.items()))
        run_bot(name, Path(proj.path), proj.telegram_bot_token, config.model, config.allowed_username)
    else:
        names = ", ".join(config.projects.keys())
        raise SystemExit(
            f"Multiple projects configured ({names}). "
            f"Start each separately: link-project-to-chat start --project NAME"
        )
