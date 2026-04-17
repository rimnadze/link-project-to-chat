from __future__ import annotations

import asyncio
import collections
import logging
import time
from collections.abc import Callable
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
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
    patch_project,
    resolve_permissions,
    save_session,
)
from ._auth import AuthMixin, _config_write_lock
from .formatting import md_to_telegram, split_html, strip_html
from .claude_client import EFFORT_LEVELS, MODELS, PERMISSION_MODES
from .plugin import Plugin, PluginContext, load_plugin
from .stream import StreamEvent, TextDelta, ThinkingDelta, ToolUse
from .task_manager import Task, TaskManager, TaskStatus, TaskType

logger = logging.getLogger(__name__)


def _topo_sort(plugins: list) -> list:
    by_name = {p.name: p for p in plugins}
    result, visited = [], set()
    def visit(p):
        if p.name in visited:
            return
        visited.add(p.name)
        for dep in p.depends_on:
            if dep in by_name:
                visit(by_name[dep])
        result.append(p)
    for p in plugins:
        visit(p)
    return result


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

_CMD_HELP = "\n".join(f"/{name} - {desc}" for name, desc in COMMANDS)


def _parse_task_id(data: str) -> int:
    return int(data.split("_")[-1])


class ProjectBot(AuthMixin):
    def __init__(
        self,
        name: str,
        path: Path,
        token: str,
        allowed_users: list[dict],
        skip_permissions: bool = False,
        permission_mode: str | None = None,
        allowed_tools: list[str] | None = None,
        disallowed_tools: list[str] | None = None,
        plugins: list[dict] | None = None,
    ):
        self.name = name
        self.path = path.resolve()
        self.token = token
        self._allowed_users = allowed_users or []
        self._started_at = time.monotonic()
        self._app = None
        self._bot_username: str = ""
        self._bot_id: int = 0
        self._typing_tasks: dict[int, asyncio.Task] = {}
        self._stream_text: dict[int, str] = {}
        self._thinking_buf: dict[int, str] = {}   # task_id → accumulated thinking
        self._thinking_store: dict[int, str] = {}  # result_msg_id → thinking text
        self._chat_history: dict[int, collections.deque] = {}   # chat_id → deque of msg dicts
        self._last_llm_msg_id: dict[int, int] = {}              # chat_id → triggering msg_id of last LLM call
        self._plugins: list[Plugin] = []
        self._plugin_callbacks: dict[str, Callable] = {}
        self._plugin_configs: list[dict] = plugins or []
        self._shared_ctx: PluginContext | None = None
        self._last_reload: float = 0.0
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

    def _on_user_identified(self, user) -> None:
        """Persist the newly discovered user_id to config and update web server auth."""
        try:
            from .config import DEFAULT_CONFIG
            import json
            cfg_path = DEFAULT_CONFIG
            with _config_write_lock:
                raw = json.loads(cfg_path.read_text())
                projects = raw.get("projects", {})
                proj = projects.get(self.name, {})
                for u in proj.get("allowed_users", raw.get("allowed_users", [])):
                    if u.get("username") == (user.username or "").lower().lstrip("@") and not u.get("user_id"):
                        u["user_id"] = user.id
                cfg_path.write_text(json.dumps(raw, indent=2))
        except Exception:
            logger.warning("failed to persist user_id for %s", user.username, exc_info=True)
        if self._shared_ctx is not None:
            self._shared_ctx.allowed_user_ids = [
                u["user_id"] for u in self._allowed_users if u.get("user_id")
            ]
        logger.info("Learned user_id %d for @%s", user.id, user.username)

    def _reload_if_needed(self) -> None:
        now = time.monotonic()
        if now - self._last_reload < 5.0:
            return
        self._last_reload = now
        try:
            from .config import load_config
            config = load_config()
            proj = config.projects.get(self.name)
            if proj:
                users = proj.allowed_users or config.allowed_users
                self._allowed_users = [
                    {"username": u.username, "user_id": u.user_id, "role": u.role}
                    for u in users
                ]
                if self._shared_ctx is not None:
                    self._shared_ctx.allowed_user_ids = [
                        u["user_id"] for u in self._allowed_users if u.get("user_id")
                    ]
        except Exception:
            logger.warning("hot-reload of allowed_users failed", exc_info=True)

    def _record_message(self, chat_id: int, username: str, text: str, msg_id: int) -> None:
        history = self._chat_history.setdefault(chat_id, collections.deque(maxlen=200))
        history.append({"from": username, "text": text, "msg_id": msg_id})

    def _context_since_last_llm(self, chat_id: int, before_msg_id: int) -> str:
        history = self._chat_history.get(chat_id, collections.deque())
        last_id = self._last_llm_msg_id.get(chat_id, 0)
        msgs = [m for m in history if last_id < m["msg_id"] < before_msg_id]
        if not msgs:
            return ""
        lines = "\n".join(f"{m['from']}: {m['text']}" for m in msgs)
        return f"[Recent discussion]\n{lines}\n\n"

    async def _on_task_started(self, task: Task) -> None:
        chat = await self._app.bot.get_chat(task.chat_id)
        self._typing_tasks[task.id] = asyncio.create_task(self._keep_typing(chat))

    async def _on_stream_event(self, task: Task, event: StreamEvent) -> None:
        if isinstance(event, ThinkingDelta):
            self._thinking_buf.setdefault(task.id, "")
            if self._thinking_buf[task.id]:
                self._thinking_buf[task.id] += "\n\n"
            self._thinking_buf[task.id] += event.text
        elif isinstance(event, TextDelta):
            self._stream_text.setdefault(task.id, "")
            self._stream_text[task.id] += event.text
        elif isinstance(event, ToolUse):
            if event.path and self._is_image(event.path):
                await self._send_image(
                    task.chat_id, event.path, reply_to=task.message_id
                )
            for plugin in self._plugins:
                try:
                    await plugin.on_tool_use(event.tool, event.path)
                except Exception:
                    logger.warning("plugin %s on_tool_use failed", plugin.name, exc_info=True)

    async def _send_html(self, chat_id: int, html: str, reply_to: int | None = None, reply_markup=None) -> int | None:
        """Send HTML message(s), attaching reply_markup to the last chunk. Returns last message ID."""
        chunks = split_html(html)
        last_id = None
        for i, chunk in enumerate(chunks):
            is_last = i == len(chunks) - 1
            km = reply_markup if is_last else None
            try:
                msg = await self._app.bot.send_message(
                    chat_id, chunk, parse_mode="HTML", reply_to_message_id=reply_to, reply_markup=km
                )
                last_id = msg.message_id
            except Exception:
                logger.warning("HTML send failed, falling back to plain", exc_info=True)
                plain = strip_html(chunk).replace("\x00", "")
                if plain.strip():
                    msg = await self._app.bot.send_message(
                        chat_id,
                        plain[:4096] if len(plain) > 4096 else plain,
                        reply_to_message_id=reply_to,
                        reply_markup=km,
                    )
                    last_id = msg.message_id
        return last_id

    async def _send_to_chat(self, chat_id: int, text: str, reply_to: int | None = None) -> None:
        html = md_to_telegram(text or "[No output]").replace("\x00", "")
        await self._send_html(chat_id, html, reply_to)

    async def _send_raw(self, chat_id: int, text: str, reply_to: int | None = None) -> None:
        escaped = (text or "[No output]").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        await self._send_html(chat_id, f"<pre>{escaped}</pre>", reply_to)

    async def _finalize_claude_task(self, task: Task) -> None:
        self._stream_text.pop(task.id, None)
        thinking = self._thinking_buf.pop(task.id, None)

        if task._compact:
            text = "Session compacted." if task.status == TaskStatus.DONE else f"Compact failed: {task.error}"
            await self._send_to_chat(task.chat_id, text, reply_to=task.message_id)
            return

        if task.status == TaskStatus.DONE:
            if thinking:
                self._thinking_store[task.id] = thinking
            await self._send_to_chat(task.chat_id, task.result, reply_to=task.message_id)
        else:
            await self._send_to_chat(task.chat_id, f"Error: {task.error}", reply_to=task.message_id)

    async def _finalize_command_task(self, task: Task) -> None:
        output = (task.result or "").rstrip() or (task.error or "").rstrip() or "(no output)"
        if len(output) > 3000:
            output = output[:3000] + "\n... (truncated, use /log)"
        if task.status == TaskStatus.DONE:
            await self._send_raw(task.chat_id, f"{output}\n[exit 0]")
        else:
            await self._send_raw(task.chat_id, f"[exit {task.exit_code}]\n\n{output}")

    async def _on_task_complete(self, task: Task) -> None:
        typing = self._typing_tasks.pop(task.id, None)
        if typing:
            typing.cancel()

        if task.type == TaskType.CLAUDE and task.status != TaskStatus.CANCELLED:
            self._last_llm_msg_id[task.chat_id] = task.message_id

        if task.type == TaskType.CLAUDE:
            if self.task_manager.claude.session_id:
                save_session(self.name, self.task_manager.claude.session_id)
            await self._finalize_claude_task(task)
        else:
            await self._finalize_command_task(task)

        for plugin in self._plugins:
            try:
                await plugin.on_task_complete(task)
            except Exception:
                logger.warning("plugin %s on_task_complete failed", plugin.name, exc_info=True)

    async def _on_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update.effective_user):
            return await update.effective_message.reply_text("Unauthorized.")
        if ctx.args:
            payload = ctx.args[0]
            for plugin in self._plugins:
                for cmd in plugin.commands():
                    if cmd.command == payload:
                        return await cmd.handler(update, ctx)
        await update.effective_message.reply_text(
            f"Project: {self.name}\nPath: {self.path}\n\n"
            f"Send a message to chat with Claude.\n{_CMD_HELP}"
        )

    async def _on_text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if not msg or not update.effective_chat:
            return
        user = update.effective_user
        if not user or not self._auth(user):
            return await msg.reply_text("Unauthorized.")
        if self._rate_limited(user.id):
            return await msg.reply_text("Rate limited. Try again shortly.")

        chat_id = update.effective_chat.id
        text = msg.text or ""
        username = (user.username or str(user.id))
        self._record_message(chat_id, username, text, msg.message_id)

        chat_type = update.effective_chat.type
        if chat_type in ("group", "supergroup"):
            bot_mention = f"@{self._bot_username}"
            is_mention = bot_mention.lower() in text.lower()
            is_reply_to_bot = (
                msg.reply_to_message
                and msg.reply_to_message.from_user
                and msg.reply_to_message.from_user.id == self._bot_id
            )
            if not is_mention and not is_reply_to_bot:
                return
            prompt = text.replace(bot_mention, "").strip() if is_mention else text
            if msg.reply_to_message:
                reply_text = msg.reply_to_message.text or msg.reply_to_message.caption or ""
                if reply_text and msg.reply_to_message.from_user:
                    ru = msg.reply_to_message.from_user
                    reply_name = ru.username or str(ru.id)
                    prompt = f"[Replying to {reply_name}: {reply_text}]\n\n{prompt}"
        else:
            prompt = text
            if msg.reply_to_message and msg.reply_to_message.text:
                prompt = f"[Replying to: {msg.reply_to_message.text}]\n\n{prompt}"

        for prev in self.task_manager.find_by_message(msg.message_id):
            self.task_manager.cancel(prev.id)
            typing = self._typing_tasks.pop(prev.id, None)
            if typing:
                typing.cancel()

        if not self._is_executor(user):
            return await msg.reply_text("View-only access.")

        context = self._context_since_last_llm(chat_id, msg.message_id)
        self.task_manager.submit_claude(
            chat_id=chat_id,
            message_id=msg.message_id,
            prompt=context + prompt,
        )

    async def _on_run(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_executor(update.effective_user):
            return await update.effective_message.reply_text("Unauthorized." if not self._auth(update.effective_user) else "View-only access.")
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
            return await update.effective_message.reply_text("Unauthorized.")
        markup = self._tasks_markup(update.effective_chat.id)
        await update.effective_message.reply_text("Tasks:" if markup else "No tasks.", reply_markup=markup)

    def _model_markup(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(m, callback_data=f"model_set_{m}")]
            for m in MODELS
        ])

    def _current_model(self) -> str:
        return self.task_manager.claude.model_display or self.task_manager.claude.model

    async def _on_model(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_executor(update.effective_user):
            return await update.effective_message.reply_text("Unauthorized." if not self._auth(update.effective_user) else "View-only access.")
        await update.effective_message.reply_text(
            f"Current: {self._current_model()}",
            reply_markup=self._model_markup(),
        )

    def _effort_markup(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(e, callback_data=f"effort_set_{e}")]
            for e in EFFORT_LEVELS
        ])

    def _current_effort(self) -> str:
        return self.task_manager.claude.effort

    async def _on_effort(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_executor(update.effective_user):
            return await update.effective_message.reply_text("Unauthorized." if not self._auth(update.effective_user) else "View-only access.")
        await update.effective_message.reply_text(
            f"Current: {self._current_effort()}",
            reply_markup=self._effort_markup(),
        )

    _PERMISSION_OPTIONS = (
        *PERMISSION_MODES,
        "dangerously-skip-permissions",
    )

    def _permissions_markup(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(o, callback_data=f"permissions_set_{o}")]
            for o in self._PERMISSION_OPTIONS
        ])

    def _current_permission(self) -> str:
        claude = self.task_manager.claude
        if claude.skip_permissions:
            return "dangerously-skip-permissions"
        return claude.permission_mode or "default"

    async def _on_permissions(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_executor(update.effective_user):
            return await update.effective_message.reply_text("Unauthorized." if not self._auth(update.effective_user) else "View-only access.")
        await update.effective_message.reply_text(
            f"Current: {self._current_permission()}",
            reply_markup=self._permissions_markup(),
        )

    async def _on_compact(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_executor(update.effective_user):
            return await update.effective_message.reply_text("Unauthorized." if not self._auth(update.effective_user) else "View-only access.")
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
        await update.effective_message.reply_text(_CMD_HELP)

    async def _on_reset(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_message:
            return
        if not self._is_executor(update.effective_user):
            return await update.effective_message.reply_text("Unauthorized." if not self._auth(update.effective_user) else "View-only access.")
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

        for prefix, handler in self._plugin_callbacks.items():
            if query.data.startswith(prefix):
                await handler(query)
                return

        if query.data.startswith("model_set_"):
            name = query.data[len("model_set_"):]
            if name in MODELS:
                self.task_manager.claude.model = name
                self.task_manager.claude.model_display = None
                patch_project(self.name, {"model": name})
            await query.edit_message_text(
                f"Model: {self._current_model()}",
                reply_markup=self._model_markup(),
            )
        elif query.data.startswith("effort_set_"):
            level = query.data[len("effort_set_"):]
            if level in EFFORT_LEVELS:
                self.task_manager.claude.effort = level
                patch_project(self.name, {"effort": level})
            await query.edit_message_text(
                f"Effort: {self._current_effort()}",
                reply_markup=self._effort_markup(),
            )
        elif query.data.startswith("permissions_set_"):
            mode = query.data[len("permissions_set_"):]
            if mode == "dangerously-skip-permissions" or mode in PERMISSION_MODES:
                skip, pm = resolve_permissions(mode)
                self.task_manager.claude.skip_permissions = skip
                self.task_manager.claude.permission_mode = pm
                patch_project(self.name, {"permissions": mode if mode != "default" else None})
            await query.edit_message_text(
                f"Permissions: {self._current_permission()}",
                reply_markup=self._permissions_markup(),
            )
        elif query.data == "reset_confirm":
            self.task_manager.cancel_all()
            self.task_manager.claude.session_id = None
            clear_session(self.name)
            await query.edit_message_text("Session reset.")
        elif query.data == "reset_cancel":
            await query.edit_message_text("Reset cancelled.")
        elif query.data.startswith("task_info_"):
            task_id = _parse_task_id(query.data)
            task = self.task_manager.get(task_id)
            if not task:
                await query.edit_message_text(f"Task #{task_id} not found.")
                return
            elapsed = f" | {task.elapsed_human}" if task.elapsed_human else ""
            text = f"#{task.id} [{task.type.value}] {task.status.value}{elapsed}\n{task.input[:200]}"
            rows = []
            if task.status in (TaskStatus.WAITING, TaskStatus.RUNNING) and self._is_executor(query.from_user):
                rows.append([InlineKeyboardButton("Cancel", callback_data=f"task_cancel_{task_id}")])
            if task.status in (TaskStatus.RUNNING, TaskStatus.DONE, TaskStatus.FAILED):
                rows.append([InlineKeyboardButton("Log", callback_data=f"task_log_{task_id}")])
            if task_id in self._thinking_store:
                rows.append([InlineKeyboardButton("Thinking", callback_data=f"show_thinking_{task_id}")])
            rows.append([InlineKeyboardButton("« Back", callback_data="tasks_back")])
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(rows))
        elif query.data == "tasks_back":
            await self._render_tasks(query.message.chat_id, edit_query=query)
        elif query.data.startswith("task_cancel_"):
            if not self._is_executor(query.from_user):
                await query.answer("View-only access.")
                return
            task_id = _parse_task_id(query.data)
            if self.task_manager.cancel(task_id):
                typing = self._typing_tasks.pop(task_id, None)
                if typing:
                    typing.cancel()
                await query.edit_message_text(f"#{task_id} cancelled.")
            else:
                await query.edit_message_text(f"#{task_id} not found or already finished.")
        elif query.data.startswith("task_log_"):
            task_id = _parse_task_id(query.data)
            task = self.task_manager.get(task_id)
            if not task:
                await query.edit_message_text(f"Task #{task_id} not found.")
                return
            output = task.result or task.error or "(no output)"
            if len(output) > 3000:
                output = output[:3000] + f"\n... (truncated, {len(task.result or '')} chars total)"
            rows = [[InlineKeyboardButton("« Back", callback_data=f"task_info_{task_id}")]]
            await query.edit_message_text(f"#{task_id} log:\n{output}", reply_markup=InlineKeyboardMarkup(rows))
        elif query.data.startswith("show_thinking_"):
            try:
                task_id = int(query.data[len("show_thinking_"):])
            except ValueError:
                await query.answer("Invalid thinking reference.")
                return
            thinking = self._thinking_store.get(task_id)
            if not thinking:
                await query.answer("Thinking not available.")
                return
            await self._send_to_chat(query.message.chat_id, f"💭 {thinking}")

    async def _on_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._auth(update.effective_user):
            return await update.effective_message.reply_text("Unauthorized.")

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
        user = update.effective_user
        if not user or not self._auth(user):
            return await msg.reply_text("Unauthorized.")
        if self._rate_limited(user.id):
            return await msg.reply_text("Rate limited. Try again shortly.")

        chat_type = update.effective_chat.type
        if chat_type in ("group", "supergroup"):
            bot_mention = f"@{self._bot_username}"
            caption = msg.caption or ""
            is_mention = bot_mention.lower() in caption.lower()
            is_reply_to_bot = (
                msg.reply_to_message
                and msg.reply_to_message.from_user
                and msg.reply_to_message.from_user.id == self._bot_id
            )
            if not is_mention and not is_reply_to_bot:
                return

        if not self._is_executor(user):
            return await msg.reply_text("View-only access.")

        uploads_dir = Path("/tmp/link-project-to-chat") / self.name / "uploads"
        uploads_dir.mkdir(parents=True, exist_ok=True)

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

        caption = (msg.caption or "").replace(f"@{self._bot_username}", "").strip()
        prompt = f"[User uploaded {dest}]"
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

        bot_info = await app.bot.get_me()
        self._bot_username = bot_info.username or ""
        self._bot_id = bot_info.id

        await self.task_manager.start()

        shared_ctx = PluginContext(
            bot_name=self.name,
            project_path=self.path,
            bot_token=self.token,
            bot_username=self._bot_username,
            trusted_user_id=next((u.get("user_id") for u in self._allowed_users if u.get("user_id")), None),
            allowed_user_ids=[u["user_id"] for u in self._allowed_users if u.get("user_id")],
            _send=app.bot.send_message,
        )
        self._shared_ctx = shared_ctx

        for cfg in self._plugin_configs:
            pname = cfg.get("name", "")
            if not pname:
                continue
            plugin = load_plugin(pname, shared_ctx, cfg)
            if plugin:
                self._plugins.append(plugin)

        for plugin in self._plugins:
            for prefix, handler in plugin.callbacks().items():
                self._plugin_callbacks[prefix] = handler
            cmds = plugin.commands()
            if cmds:
                for bc in cmds:
                    app.add_handler(CommandHandler(bc.command, bc.handler))

        for plugin in _topo_sort(self._plugins):
            await plugin.start()

        all_commands = list(COMMANDS)
        for plugin in self._plugins:
            for bc in plugin.commands():
                all_commands.append((bc.command, bc.description))
        await app.bot.set_my_commands(all_commands)

        for u in self._allowed_users:
            uid = u.get("user_id")
            if uid:
                try:
                    await app.bot.send_message(uid, f"Bot started.\nProject: {self.name}\nPath: {self.path}")
                except Exception:
                    logger.error("Failed to send startup message to %s", uid, exc_info=True)

    async def _post_shutdown(self, app) -> None:
        await self.task_manager.stop()
        for plugin in reversed(self._plugins):
            try:
                await plugin.stop()
            except Exception:
                logger.warning("plugin %s stop failed", plugin.name, exc_info=True)

    def build(self):
        app = (
            ApplicationBuilder()
            .token(self.token)
            .concurrent_updates(True)
            .post_init(self._post_init)
            .post_shutdown(self._post_shutdown)
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
        private = filters.ChatType.PRIVATE
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

        unsupported_filter = private & (
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
    allowed_users: list[dict],
    session_id: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    skip_permissions: bool = False,
    permission_mode: str | None = None,
    allowed_tools: list[str] | None = None,
    disallowed_tools: list[str] | None = None,
    plugins: list[dict] | None = None,
) -> None:
    if not allowed_users:
        raise SystemExit(
            "No allowed users configured. Add allowed_users to config.json."
        )
    if session_id:
        save_session(name, session_id)
    bot = ProjectBot(
        name, path, token, allowed_users,
        skip_permissions=skip_permissions,
        permission_mode=permission_mode,
        allowed_tools=allowed_tools,
        disallowed_tools=disallowed_tools,
        plugins=plugins,
    )
    bot.task_manager.claude.session_id = session_id or load_sessions().get(name)
    if model:
        bot.task_manager.claude.model = model
    if effort:
        bot.task_manager.claude.effort = effort
    app = bot.build()
    logger.info(
        "Bot '%s' started at %s (users=%s)", name, path, [u["username"] for u in allowed_users]
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
        effective_users = proj.allowed_users or config.allowed_users
        allowed_users = [{"username": u.username, "user_id": u.user_id, "role": u.role} for u in effective_users]
        proj_skip, proj_pm = resolve_permissions(proj.permissions)
        run_bot(
            name,
            Path(proj.path),
            proj.telegram_bot_token,
            allowed_users,
            model=model or proj.model,
            effort=proj.effort,
            skip_permissions=skip_permissions or proj_skip,
            permission_mode=permission_mode or proj_pm,
            allowed_tools=allowed_tools,
            disallowed_tools=disallowed_tools,
            plugins=proj.plugins or None,
        )
    else:
        names = ", ".join(config.projects.keys())
        raise SystemExit(
            f"Multiple projects configured ({names}). "
            f"Start each separately: link-project-to-chat start --project NAME"
        )
