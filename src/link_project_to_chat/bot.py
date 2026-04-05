from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import (
    Config,
    DEFAULT_CONFIG,
    clear_session,
    load_sessions,
    save_session,
    save_project_trusted_user_id,
    save_trusted_user_id,
)
from ._auth import AuthMixin
from .formatting import md_to_telegram, split_html, strip_html
from .claude_client import EFFORT_LEVELS, MODELS, PERMISSION_MODES
from .stream import StreamEvent, TextDelta, ThinkingDelta, ToolUse
from .task_manager import Task, TaskManager, TaskStatus, TaskType

logger = logging.getLogger(__name__)

COMMANDS = [
    ("run", "Run a background command"),
    ("tasks", "List all tasks"),
    ("model", "Set Claude model (haiku/sonnet/opus)"),
    ("effort", "Set thinking depth (low/medium/high/max)"),
    ("permissions", "Set permission mode"),
    ("compact", "Compress session context"),
    ("status", "Bot status"),
    ("reset", "Clear Claude session"),
    ("help", "Show available commands"),
]


class ProjectBot(AuthMixin):
    def __init__(
        self,
        name: str,
        path: Path,
        token: str,
        allowed_username: str,
        trusted_user_id: int | None = None,
        on_trust: Callable[[int], None] | None = None,
        skip_permissions: bool = False,
        permission_mode: str | None = None,
        allowed_tools: list[str] | None = None,
        disallowed_tools: list[str] | None = None,
    ):
        self.name = name
        self.path = path.resolve()
        self.token = token
        self._allowed_username = allowed_username
        self._trusted_user_id = trusted_user_id
        self._on_trust_fn = on_trust
        self._started_at = time.monotonic()
        self._app = None
        self._typing_tasks: dict[int, asyncio.Task] = {}
        self._stream_messages: dict[int, tuple[int, float]] = {}
        self._stream_text: dict[int, str] = {}
        self._init_auth()
        self.task_manager = TaskManager(
            project_path=self.path,
            on_complete=self._on_task_complete,
            on_task_started=self._on_task_started,
            on_stream_event=self._on_stream_event,
            skip_permissions=skip_permissions,
            permission_mode=permission_mode,
            allowed_tools=allowed_tools,
            disallowed_tools=disallowed_tools,
        )

    def _on_trust(self, user_id: int) -> None:
        if self._on_trust_fn:
            self._on_trust_fn(user_id)
        else:
            save_trusted_user_id(user_id)

    async def _on_task_started(self, task: Task) -> None:
        chat = await self._app.bot.get_chat(task.chat_id)
        self._typing_tasks[task.id] = asyncio.create_task(self._keep_typing(chat))

    async def _on_stream_event(self, task: Task, event: StreamEvent) -> None:
        if isinstance(event, ThinkingDelta):
            if task.id not in self._stream_messages:
                try:
                    msg = await self._app.bot.send_message(
                        task.chat_id,
                        "💭 Thinking...",
                        reply_to_message_id=task.message_id,
                    )
                    self._stream_messages[task.id] = (msg.message_id, time.time())
                except Exception:
                    logger.warning("Failed to send thinking placeholder", exc_info=True)

        elif isinstance(event, TextDelta):
            self._stream_text.setdefault(task.id, "")
            self._stream_text[task.id] += event.text

            if task.id not in self._stream_messages:
                text = self._stream_text[task.id]
                html = md_to_telegram(text).replace("\x00", "")
                try:
                    msg = await self._app.bot.send_message(
                        task.chat_id,
                        html or "...",
                        parse_mode="HTML",
                        reply_to_message_id=task.message_id,
                    )
                    self._stream_messages[task.id] = (msg.message_id, time.time())
                except Exception:
                    logger.warning(
                        "Failed to send initial stream message", exc_info=True
                    )
            else:
                msg_id, last_edit = self._stream_messages[task.id]
                now = time.time()
                if now - last_edit >= 2.0:
                    text = self._stream_text[task.id]
                    html = md_to_telegram(text).replace("\x00", "")
                    try:
                        await self._app.bot.edit_message_text(
                            html or "...",
                            chat_id=task.chat_id,
                            message_id=msg_id,
                            parse_mode="HTML",
                        )
                        self._stream_messages[task.id] = (msg_id, now)
                    except BadRequest as e:
                        if "Message is not modified" not in str(e):
                            logger.debug("Stream edit failed", exc_info=True)
                    except Exception:
                        logger.debug("Stream edit failed", exc_info=True)

        elif isinstance(event, ToolUse):
            if event.path and self._is_image(event.path):
                await self._send_image(
                    task.chat_id, event.path, reply_to=task.message_id
                )

    async def _on_task_complete(self, task: Task) -> None:
        typing = self._typing_tasks.pop(task.id, None)
        if typing:
            typing.cancel()

        if task.type == TaskType.CLAUDE:
            if self.task_manager.claude.session_id:
                save_session(self.name, self.task_manager.claude.session_id)
            if task._compact:
                text = (
                    "Session compacted."
                    if task.status == TaskStatus.DONE
                    else f"Compact failed: {task.error}"
                )
                await self._send_to_chat(task.chat_id, text, reply_to=task.message_id)
            elif task.id in self._stream_messages:
                msg_id, _ = self._stream_messages.pop(task.id)
                self._stream_text.pop(task.id, None)
                if task.status == TaskStatus.DONE:
                    text = task.result or "[No output]"
                    html = md_to_telegram(text).replace("\x00", "")
                    for i, chunk in enumerate(split_html(html)):
                        try:
                            if i == 0:
                                await self._app.bot.edit_message_text(
                                    chunk,
                                    chat_id=task.chat_id,
                                    message_id=msg_id,
                                    parse_mode="HTML",
                                )
                            else:
                                await self._app.bot.send_message(
                                    task.chat_id,
                                    chunk,
                                    parse_mode="HTML",
                                    reply_to_message_id=task.message_id,
                                )
                        except BadRequest as e:
                            if "Message is not modified" not in str(e):
                                logger.warning(
                                    "Final stream edit failed", exc_info=True
                                )
                        except Exception:
                            logger.warning("Final stream edit failed", exc_info=True)
                            plain = strip_html(chunk).replace("\x00", "")
                            if plain.strip():
                                await self._app.bot.send_message(
                                    task.chat_id,
                                    plain[:4096],
                                    reply_to_message_id=task.message_id,
                                )
                else:
                    try:
                        await self._app.bot.edit_message_text(
                            f"Error: {task.error}",
                            chat_id=task.chat_id,
                            message_id=msg_id,
                        )
                    except Exception:
                        await self._send_to_chat(
                            task.chat_id,
                            f"Error: {task.error}",
                            reply_to=task.message_id,
                        )
            else:
                text = (
                    task.result
                    if task.status == TaskStatus.DONE
                    else f"Error: {task.error}"
                )
                await self._send_to_chat(task.chat_id, text, reply_to=task.message_id)
        else:
            output = (
                (task.result or "").rstrip()
                or (task.error or "").rstrip()
                or "(no output)"
            )
            if len(output) > 3000:
                output = output[:3000] + "\n... (truncated, use /log)"
            if task.status != TaskStatus.DONE:
                await self._send_raw(
                    task.chat_id, f"[exit {task.exit_code}]\n\n{output}"
                )
            else:
                await self._send_raw(task.chat_id, f"{output}\n[exit 0]")

    async def _on_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update.effective_user):
            return await update.effective_message.reply_text("Unauthorized.")
        cmd_list = "\n".join(f"/{name} - {desc}" for name, desc in COMMANDS)
        await update.effective_message.reply_text(
            f"Project: {self.name}\nPath: {self.path}\n\n"
            f"Send a message to chat with Claude.\n{cmd_list}"
        )

    async def _on_text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if not msg:
            return
        if not self._auth(update.effective_user):
            return await msg.reply_text("Unauthorized.")
        if self._rate_limited(update.effective_user.id):
            return await msg.reply_text("Rate limited. Try again shortly.")

        for prev in self.task_manager.find_by_message(msg.message_id):
            self.task_manager.cancel(prev.id)
            typing = self._typing_tasks.pop(prev.id, None)
            if typing:
                typing.cancel()

        prompt = msg.text
        if msg.reply_to_message and msg.reply_to_message.text:
            prompt = f"[Replying to: {msg.reply_to_message.text}]\n\n{prompt}"
        self.task_manager.submit_claude(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            prompt=prompt,
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

    _TASK_ICONS = {
        TaskStatus.WAITING: "~",
        TaskStatus.RUNNING: ">",
        TaskStatus.DONE: "+",
        TaskStatus.FAILED: "!",
        TaskStatus.CANCELLED: "x",
    }

    def _tasks_markup(self, chat_id: int) -> InlineKeyboardMarkup | None:
        all_tasks = self.task_manager.list_tasks(chat_id=chat_id, limit=100)
        active = [t for t in all_tasks if t.status in (TaskStatus.WAITING, TaskStatus.RUNNING)]
        finished = [t for t in all_tasks if t.status not in (TaskStatus.WAITING, TaskStatus.RUNNING)][:5]
        tasks = active + finished
        if not tasks:
            return None
        buttons = []
        for t in tasks:
            icon = self._TASK_ICONS.get(t.status, "?")
            elapsed = f" {t.elapsed_human}" if t.elapsed_human else ""
            label = t.name if t.type == TaskType.COMMAND else t.input[:40]
            buttons.append([InlineKeyboardButton(f"{icon} #{t.id}{elapsed} {label}", callback_data=f"task_info_{t.id}")])
        return InlineKeyboardMarkup(buttons)

    async def _render_tasks(self, chat_id: int, edit_query) -> None:
        markup = self._tasks_markup(chat_id)
        await edit_query.edit_message_text("Tasks:" if markup else "No tasks.", reply_markup=markup)

    async def _on_tasks(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_message or not update.effective_chat:
            return
        if not self._auth(update.effective_user):
            return
        markup = self._tasks_markup(update.effective_chat.id)
        await update.effective_message.reply_text("Tasks:" if markup else "No tasks.", reply_markup=markup)

    async def _on_model(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update.effective_user):
            return
        if not ctx.args:
            current = (
                self.task_manager.claude.model_display or self.task_manager.claude.model
            )
            return await update.effective_message.reply_text(
                f"Current: {current}\nUsage: /model {{{'/'.join(MODELS)}}}"
            )
        name = ctx.args[0].lower()
        if name not in MODELS:
            return await update.effective_message.reply_text(
                f"Invalid. Choose: {', '.join(MODELS)}"
            )
        self.task_manager.claude.model = name
        self.task_manager.claude.model_display = None
        await update.effective_message.reply_text(f"Model: {name}")

    async def _on_effort(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update.effective_user):
            return
        if not ctx.args:
            current = self.task_manager.claude.effort
            return await update.effective_message.reply_text(
                f"Current: {current}\nUsage: /effort {{{'/'.join(EFFORT_LEVELS)}}}"
            )
        level = ctx.args[0].lower()
        if level not in EFFORT_LEVELS:
            return await update.effective_message.reply_text(
                f"Invalid. Choose: {', '.join(EFFORT_LEVELS)}"
            )
        self.task_manager.claude.effort = level
        await update.effective_message.reply_text(f"Effort: {level}")

    _PERMISSION_OPTIONS = (
        *PERMISSION_MODES,
        "dangerously-skip-permissions",
    )

    async def _on_permissions(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update.effective_user):
            return
        if not ctx.args:
            claude = self.task_manager.claude
            if claude.skip_permissions:
                current = "dangerously-skip-permissions"
            elif claude.permission_mode:
                current = claude.permission_mode
            else:
                current = "default"
            options = "\n".join(f"  {o}" for o in self._PERMISSION_OPTIONS)
            return await update.effective_message.reply_text(
                f"Current: {current}\n\nOptions:\n{options}\n\n"
                f"Usage: /permissions <mode>"
            )
        raw = ctx.args[0]
        mode_lower = raw.lower()
        mode_map = {m.lower(): m for m in self._PERMISSION_OPTIONS}
        mode = mode_map.get(mode_lower)
        if mode is None:
            return await update.effective_message.reply_text(
                f"Invalid. Choose: {', '.join(self._PERMISSION_OPTIONS)}"
            )
        if mode == "dangerously-skip-permissions":
            self.task_manager.claude.skip_permissions = True
            self.task_manager.claude.permission_mode = None
            await update.effective_message.reply_text("Permissions: dangerously-skip-permissions")
        elif mode in PERMISSION_MODES:
            self.task_manager.claude.skip_permissions = False
            self.task_manager.claude.permission_mode = mode if mode != "default" else None
            await update.effective_message.reply_text(f"Permissions: {mode}")

    async def _on_compact(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update.effective_user):
            return
        if not self.task_manager.claude.session_id:
            return await update.effective_message.reply_text("No active session.")
        self.task_manager.submit_compact(
            chat_id=update.effective_chat.id,
            message_id=update.effective_message.message_id,
        )

    async def _on_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_message:
            return
        if not self._auth(update.effective_user):
            return await update.effective_message.reply_text("Unauthorized.")
        cmd_list = "\n".join(f"/{name} - {desc}" for name, desc in COMMANDS)
        await update.effective_message.reply_text(cmd_list)

    async def _on_reset(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_message:
            return
        if not self._auth(update.effective_user):
            return
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Yes, reset", callback_data="reset_confirm"),
                    InlineKeyboardButton("Cancel", callback_data="reset_cancel"),
                ]
            ]
        )
        await update.effective_message.reply_text(
            "Are you sure? This will clear the Claude session.",
            reply_markup=keyboard,
        )

    async def _on_callback(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        if not query or not query.data:
            return
        if not self._auth(query.from_user):
            await query.answer("Unauthorized.")
            return
        await query.answer()

        if query.data == "reset_confirm":
            self.task_manager.cancel_all()
            self.task_manager.claude.session_id = None
            clear_session(self.name)
            await query.edit_message_text("Session reset.")
        elif query.data == "reset_cancel":
            await query.edit_message_text("Reset cancelled.")
        elif query.data.startswith("task_info_"):
            task_id = int(query.data.split("_")[-1])
            task = self.task_manager.get(task_id)
            if not task:
                await query.edit_message_text(f"Task #{task_id} not found.")
                return
            elapsed = f" | {task.elapsed_human}" if task.elapsed_human else ""
            text = f"#{task.id} [{task.type.value}] {task.status.value}{elapsed}\n{task.input[:200]}"
            rows = []
            if task.status in (TaskStatus.WAITING, TaskStatus.RUNNING):
                rows.append([InlineKeyboardButton("Cancel", callback_data=f"task_cancel_{task_id}")])
            if task.status in (TaskStatus.RUNNING, TaskStatus.DONE, TaskStatus.FAILED):
                rows.append([InlineKeyboardButton("Log", callback_data=f"task_log_{task_id}")])
            rows.append([InlineKeyboardButton("« Back", callback_data="tasks_back")])
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows))
        elif query.data == "tasks_back":
            await self._render_tasks(query.message.chat_id, edit_query=query)
        elif query.data.startswith("task_cancel_"):
            task_id = int(query.data.split("_")[-1])
            if self.task_manager.cancel(task_id):
                typing = self._typing_tasks.pop(task_id, None)
                if typing:
                    typing.cancel()
                await query.edit_message_text(f"#{task_id} cancelled.")
            else:
                await query.edit_message_text(f"#{task_id} not found or already finished.")
        elif query.data.startswith("task_log_"):
            task_id = int(query.data.split("_")[-1])
            task = self.task_manager.get(task_id)
            if not task:
                await query.edit_message_text(f"Task #{task_id} not found.")
                return
            output = task.result or task.error or "(no output)"
            if len(output) > 3000:
                output = output[:3000] + f"\n... (truncated, {len(task.result or '')} chars total)"
            rows = [[InlineKeyboardButton("« Back", callback_data=f"task_info_{task_id}")]]
            await query.edit_message_text(f"#{task_id} log:\n{output}", reply_markup=InlineKeyboardMarkup(rows))

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
            f"Model: {self.task_manager.claude.model_display or self.task_manager.claude.model}",
            f"Uptime: {h}h {m}m {s}s",
            f"Session: {st['session_id'] or 'none'}",
            f"Claude: {'RUNNING' if st['running'] else 'idle'}",
            f"Running tasks: {self.task_manager.running_count}",
            f"Waiting: {self.task_manager.waiting_count}",
        ]
        await update.effective_message.reply_text("\n".join(lines))

    async def _on_file(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if not msg or not update.effective_chat:
            return
        if not self._auth(update.effective_user):
            return await msg.reply_text("Unauthorized.")
        if self._rate_limited(update.effective_user.id):
            return await msg.reply_text("Rate limited. Try again shortly.")

        uploads_dir = self.path / "uploads"
        uploads_dir.mkdir(exist_ok=True)

        if msg.photo:
            photo = msg.photo[-1]
            file = await photo.get_file()
            filename = f"photo_{int(time.monotonic() * 1000)}.jpg"
        elif msg.document:
            file = await msg.document.get_file()
            raw_name = msg.document.file_name or f"file_{int(time.monotonic() * 1000)}"
            filename = "".join(
                c
                for c in raw_name.replace("/", "_").replace("\\", "_")
                if c.isalnum() or c in "._- "
            )[:200]
        else:
            return await msg.reply_text("Unsupported file type.")

        dest = uploads_dir / filename
        if dest.exists():
            stem = dest.stem
            suffix = dest.suffix
            counter = 2
            while dest.exists():
                dest = uploads_dir / f"{stem}_{counter}{suffix}"
                counter += 1
            filename = dest.name

        await file.download_to_drive(str(dest))

        caption = msg.caption or ""
        prompt = f"[User uploaded uploads/{filename}]"
        if caption:
            prompt += f"\n\n{caption}"

        self.task_manager.submit_claude(
            chat_id=update.effective_chat.id,
            message_id=msg.message_id,
            prompt=prompt,
        )

    async def _on_unsupported(
        self, update: Update, ctx: ContextTypes.DEFAULT_TYPE
    ) -> None:
        msg = update.effective_message
        if not msg:
            return
        if not self._auth(update.effective_user):
            return await msg.reply_text("Unauthorized.")

        if msg.voice or msg.video_note:
            text = "Voice messages aren't supported yet. Please type your message."
        elif msg.sticker:
            text = "Stickers aren't supported. Please type your message."
        elif msg.video:
            text = "Video messages aren't supported. Please type your message."
        else:
            text = "This message type isn't supported. Please type your message or send a file."

        await msg.reply_text(text)

    IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}

    def _is_image(self, path: str) -> bool:
        from pathlib import PurePosixPath

        return PurePosixPath(path).suffix.lower() in self.IMAGE_EXTENSIONS

    async def _send_image(
        self, chat_id: int, file_path: str, reply_to: int | None = None
    ) -> None:
        path = (
            self.path / file_path if not file_path.startswith("/") else Path(file_path)
        )
        if not path.exists():
            logger.warning("Image file not found: %s", path)
            return
        try:
            size = path.stat().st_size
            suffix = path.suffix.lower()
            with path.open("rb") as f:
                data = f.read()
            if suffix == ".svg" or size > 10 * 1024 * 1024:
                await self._app.bot.send_document(
                    chat_id,
                    data,
                    filename=path.name,
                    reply_to_message_id=reply_to,
                )
            else:
                await self._app.bot.send_photo(
                    chat_id,
                    data,
                    caption=path.name,
                    reply_to_message_id=reply_to,
                )
        except Exception:
            logger.warning("Failed to send image %s", path, exc_info=True)

    async def _send_to_chat(
        self, chat_id: int, text: str, reply_to: int | None = None
    ) -> None:
        text = text or "[No output]"
        html = md_to_telegram(text).replace("\x00", "")
        for chunk in split_html(html):
            try:
                await self._app.bot.send_message(
                    chat_id, chunk, parse_mode="HTML", reply_to_message_id=reply_to
                )
            except Exception:
                logger.warning("HTML send failed, falling back to plain", exc_info=True)
                plain = strip_html(chunk).replace("\x00", "")
                if plain.strip():
                    await self._app.bot.send_message(
                        chat_id,
                        plain[:4096] if len(plain) > 4096 else plain,
                        reply_to_message_id=reply_to,
                    )

    async def _send_raw(
        self, chat_id: int, text: str, reply_to: int | None = None
    ) -> None:
        text = text or "[No output]"
        escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        html = f"<pre>{escaped}</pre>"
        for chunk in split_html(html):
            try:
                await self._app.bot.send_message(
                    chat_id, chunk, parse_mode="HTML", reply_to_message_id=reply_to
                )
            except Exception:
                logger.warning("Raw send failed, falling back to plain", exc_info=True)
                if text.strip():
                    await self._app.bot.send_message(
                        chat_id,
                        text[:4096] if len(text) > 4096 else text,
                        reply_to_message_id=reply_to,
                    )

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
        err = str(ctx.error)
        if "Conflict" in err:
            logger.warning(
                "Conflict error (another instance?): %s | update=%s", err, update
            )
        else:
            logger.error("Update error: %s | update=%s", ctx.error, update)

    async def _post_init(self, app) -> None:
        result = await app.bot.delete_webhook(drop_pending_updates=True)
        logger.info("delete_webhook result=%s (drop_pending_updates=True)", result)
        await app.bot.set_my_commands(COMMANDS)
        if self._trusted_user_id:
            try:
                await app.bot.send_message(
                    self._trusted_user_id,
                    f"Bot started.\nProject: {self.name}\nPath: {self.path}",
                )
            except Exception:
                logger.error("Failed to send startup message", exc_info=True)

    def build(self):
        app = (
            ApplicationBuilder()
            .token(self.token)
            .concurrent_updates(True)
            .post_init(self._post_init)
            .build()
        )
        self._app = app
        handlers = {
            "start": self._on_start,
            "run": self._on_run,
            "tasks": self._on_tasks,
            "model": self._on_model,
            "effort": self._on_effort,
            "permissions": self._on_permissions,
            "compact": self._on_compact,
            "reset": self._on_reset,
            "status": self._on_status,
            "help": self._on_help,
        }
        for name, handler in handlers.items():
            app.add_handler(CommandHandler(name, handler))
        text_filter = (
            (filters.UpdateType.MESSAGE | filters.UpdateType.EDITED_MESSAGE)
            & filters.TEXT
            & ~filters.COMMAND
        )
        app.add_handler(MessageHandler(text_filter, self._on_text))

        file_filter = filters.Document.ALL | filters.PHOTO
        app.add_handler(MessageHandler(file_filter, self._on_file))

        unsupported_filter = (
            filters.VOICE
            | filters.VIDEO_NOTE
            | filters.Sticker.ALL
            | filters.VIDEO
            | filters.LOCATION
            | filters.CONTACT
            | filters.AUDIO
        )
        app.add_handler(MessageHandler(unsupported_filter, self._on_unsupported))

        app.add_error_handler(self._on_error)
        app.add_handler(CallbackQueryHandler(self._on_callback))
        return app


def run_bot(
    name: str,
    path: Path,
    token: str,
    username: str,
    session_id: str | None = None,
    model: str | None = None,
    skip_permissions: bool = False,
    permission_mode: str | None = None,
    allowed_tools: list[str] | None = None,
    disallowed_tools: list[str] | None = None,
    trusted_user_id: int | None = None,
    on_trust: Callable[[int], None] | None = None,
) -> None:
    if not username:
        raise SystemExit(
            "No allowed username configured. Use --username or run 'configure --username'."
        )
    if session_id:
        save_session(name, session_id)
    bot = ProjectBot(
        name, path, token, username,
        trusted_user_id=trusted_user_id,
        on_trust=on_trust,
        skip_permissions=skip_permissions,
        permission_mode=permission_mode,
        allowed_tools=allowed_tools,
        disallowed_tools=disallowed_tools,
    )
    bot.task_manager.claude.session_id = session_id or load_sessions().get(name)
    if model:
        bot.task_manager.claude.model = model
    app = bot.build()
    logger.info(
        "Bot '%s' started at %s (trusted_user_id=%s)", name, path, trusted_user_id
    )
    app.run_polling()


def run_bots(
    config: Config,
    model: str | None = None,
    skip_permissions: bool = False,
    permission_mode: str | None = None,
    allowed_tools: list[str] | None = None,
    disallowed_tools: list[str] | None = None,
    config_path: Path | None = None,
) -> None:
    if len(config.projects) == 1:
        name, proj = next(iter(config.projects.items()))
        effective_username = proj.allowed_username or config.allowed_username
        if proj.allowed_username:
            effective_trusted_id = proj.trusted_user_id
        else:
            effective_trusted_id = proj.trusted_user_id if proj.trusted_user_id is not None else config.trusted_user_id
        on_trust = None
        if config_path:
            _name = name
            _path = config_path
            on_trust = lambda uid: save_project_trusted_user_id(_name, uid, _path)
        run_bot(
            name,
            Path(proj.path),
            proj.telegram_bot_token,
            effective_username,
            model=model,
            skip_permissions=skip_permissions,
            permission_mode=permission_mode,
            allowed_tools=allowed_tools,
            disallowed_tools=disallowed_tools,
            trusted_user_id=effective_trusted_id,
            on_trust=on_trust,
        )
    else:
        names = ", ".join(config.projects.keys())
        raise SystemExit(
            f"Multiple projects configured ({names}). "
            f"Start each separately: link-project-to-chat start --project NAME"
        )
