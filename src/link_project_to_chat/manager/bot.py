from __future__ import annotations

import importlib.metadata
import logging
import time
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from .config import load_project_configs, save_project_configs
from .process import ProcessManager
from ..config import DEFAULT_CONFIG, save_trusted_user_id
from .._auth import AuthMixin

logger = logging.getLogger(__name__)

COMMANDS = [
    ("projects", "List all projects"),
    ("start_all", "Start all projects"),
    ("stop_all", "Stop all projects"),
    ("add_project", "Add a new project"),
    ("edit_project", "Edit a project"),
    ("help", "Show commands"),
]

_EDITABLE_FIELDS = ("name", "path", "token", "username", "model", "permissions")
# Fields shown as edit buttons (subset — simpler types only)
_BUTTON_EDIT_FIELDS = ("name", "path", "token", "username", "model", "permissions")


def _parse_edit_callback(data: str) -> tuple[str, str] | None:
    """Parse 'proj_efld_<field>_<name>' → (field, name). Field comes first."""
    suffix = data[len("proj_efld_"):]
    for field in _EDITABLE_FIELDS:
        if suffix.startswith(field + "_"):
            name = suffix[len(field) + 1:]
            return field, name
    return None


class ManagerBot(AuthMixin):
    _MAX_MESSAGES_PER_MINUTE = 20

    def __init__(
        self,
        token: str,
        process_manager: ProcessManager,
        allowed_username: str,
        trusted_user_id: int | None = None,
        project_config_path: Path | None = None,
    ):
        self._token = token
        self._pm = process_manager
        self._allowed_username = allowed_username
        self._trusted_user_id = trusted_user_id
        self._started_at = time.monotonic()
        self._app = None
        self._project_config_path = project_config_path
        self._init_auth()

    def _on_trust(self, user_id: int) -> None:
        path = self._project_config_path or DEFAULT_CONFIG
        save_trusted_user_id(user_id, path)

    def _load_projects(self) -> dict[str, dict]:
        path = self._project_config_path
        return load_project_configs(path) if path else load_project_configs()

    def _save_projects(self, projects: dict[str, dict]) -> None:
        path = self._project_config_path
        if path:
            save_project_configs(projects, path)
        else:
            save_project_configs(projects)

    async def _guard(self, update: Update) -> bool:
        """Returns True if the user is authorized and not rate-limited."""
        user = update.effective_user
        if not user or not self._auth(user):
            await update.effective_message.reply_text("Unauthorized.")
            return False
        if self._rate_limited(user.id):
            await update.effective_message.reply_text("Rate limited. Try again shortly.")
            return False
        return True

    def _projects_text(self) -> str:
        projects = self._pm.list_all()
        running = sum(1 for _, st in projects if st == "running")
        return f"Projects ({running}/{len(projects)} running):"

    def _list_markup(self) -> InlineKeyboardMarkup | None:
        projects = self._pm.list_all()
        if not projects:
            return None
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"{'[+]' if status == 'running' else '[-]'} {name}",
                callback_data=f"proj_info_{name}",
            )]
            for name, status in projects
        ])

    async def _on_projects(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        markup = self._list_markup()
        await update.effective_message.reply_text(
            self._projects_text() if markup else "No projects configured.",
            reply_markup=markup,
        )

    async def _on_start_all(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        count = self._pm.start_all()
        await update.effective_message.reply_text(f"Started {count} project(s).")

    async def _on_stop_all(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        count = self._pm.stop_all()
        await update.effective_message.reply_text(f"Stopped {count} project(s).")

    async def _on_help(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        await update.effective_message.reply_text(
            "\n".join(f"/{name} - {desc}" for name, desc in COMMANDS)
        )

    # ConversationHandler states for /add_project
    ADD_NAME, ADD_PATH, ADD_TOKEN, ADD_USERNAME, ADD_MODEL = range(5)

    async def _on_add_project(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        if not await self._guard(update):
            return ConversationHandler.END
        ctx.user_data["new_project"] = {}
        await update.effective_message.reply_text("Let's add a new project.\n\nWhat is the project name?")
        return self.ADD_NAME

    async def _add_name(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        name = update.message.text.strip()
        if name in self._load_projects():
            await update.effective_message.reply_text(f"Project '{name}' already exists. Try a different name:")
            return self.ADD_NAME
        ctx.user_data["new_project"]["name"] = name
        await update.effective_message.reply_text("Enter the project path:")
        return self.ADD_PATH

    async def _add_path(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        path = update.message.text.strip()
        if not Path(path).exists():
            await update.effective_message.reply_text(f"Path does not exist: {path}\nTry again:")
            return self.ADD_PATH
        ctx.user_data["new_project"]["path"] = path
        await update.effective_message.reply_text("Enter the Telegram bot token (or /skip):")
        return self.ADD_TOKEN

    async def _add_token(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        text = update.message.text.strip()
        if text != "/skip":
            ctx.user_data["new_project"]["telegram_bot_token"] = text
        await update.effective_message.reply_text("Enter the allowed username (or /skip):")
        return self.ADD_USERNAME

    async def _add_username(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        text = update.message.text.strip()
        if text != "/skip":
            ctx.user_data["new_project"]["username"] = text
        await update.effective_message.reply_text("Enter the model name (or /skip):")
        return self.ADD_MODEL

    async def _add_model(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
        text = update.message.text.strip()
        if text != "/skip":
            ctx.user_data["new_project"]["model"] = text
        data = ctx.user_data.pop("new_project", {})
        name = data.pop("name", None)
        if not name:
            await update.effective_message.reply_text("Something went wrong. Try again.")
            return ConversationHandler.END
        projects = self._load_projects()
        projects[name] = data
        self._save_projects(projects)
        await update.effective_message.reply_text(f"Added project '{name}'.")
        return ConversationHandler.END

    async def _on_edit_project(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._guard(update):
            return
        if not ctx.args or len(ctx.args) < 3:
            return await update.effective_message.reply_text(
                f"Usage: /edit_project <name> <field> <value>\nFields: {', '.join(_EDITABLE_FIELDS)}"
            )
        name, field, value = ctx.args[0], ctx.args[1], " ".join(ctx.args[2:])
        await self._apply_edit(update, name, field, value)

    async def _apply_edit(self, update: Update, name: str, field: str, value: str) -> None:
        """Apply a field edit and send a confirmation reply."""
        projects = self._load_projects()
        if name not in projects:
            await update.effective_message.reply_text(f"Project '{name}' not found.")
            return

        if field == "path":
            if not Path(value).exists():
                await update.effective_message.reply_text(f"Path does not exist: {value}")
                return
            projects[name]["path"] = value
            self._save_projects(projects)
            await update.effective_message.reply_text(f"Updated '{name}' path to {value}.")
        elif field == "name":
            if value in projects:
                await update.effective_message.reply_text(f"Project '{value}' already exists.")
                return
            projects[value] = projects.pop(name)
            self._save_projects(projects)
            self._pm.rename(name, value)
            await update.effective_message.reply_text(f"Renamed '{name}' to '{value}'.")
        elif field == "token":
            projects[name]["telegram_bot_token"] = value
            self._save_projects(projects)
            await update.effective_message.reply_text(f"Updated '{name}' token.")
        elif field in ("username", "model", "permissions"):
            projects[name][field] = value
            self._save_projects(projects)
            await update.effective_message.reply_text(f"Updated '{name}' {field} to {value}.")
        else:
            await update.effective_message.reply_text(
                f"Unknown field. Use: {', '.join(_EDITABLE_FIELDS)}"
            )

    async def _edit_field_save(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        pending = ctx.user_data.get("pending_edit")
        if not pending:
            return
        if not self._auth(update.effective_user):
            return
        ctx.user_data.pop("pending_edit")
        await self._apply_edit(update, pending["name"], pending["field"], update.message.text.strip())

    @staticmethod
    async def _edit_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        ctx.user_data.pop("pending_edit", None)
        await update.effective_message.reply_text("Edit cancelled.")

    def _proj_detail_markup(self, name: str, status: str) -> InlineKeyboardMarkup:
        rows = []
        if status == "running":
            rows.append([InlineKeyboardButton("Stop", callback_data=f"proj_stop_{name}")])
            rows.append([InlineKeyboardButton("Logs", callback_data=f"proj_logs_{name}")])
        else:
            rows.append([InlineKeyboardButton("Start", callback_data=f"proj_start_{name}")])
        rows.append([InlineKeyboardButton("Plugins", callback_data=f"proj_plugins_{name}")])
        rows.append([InlineKeyboardButton("Edit", callback_data=f"proj_edit_{name}")])
        rows.append([InlineKeyboardButton("Remove", callback_data=f"proj_remove_{name}")])
        rows.append([InlineKeyboardButton("« Back", callback_data="proj_back")])
        return InlineKeyboardMarkup(rows)

    def _available_plugins(self) -> list[str]:
        eps = importlib.metadata.entry_points(group="lptc.plugins")
        return sorted(ep.name for ep in eps)

    def _plugins_markup(self, name: str) -> InlineKeyboardMarkup:
        projects = self._load_projects()
        active = {p["name"] for p in projects.get(name, {}).get("plugins", [])}
        available = self._available_plugins()
        rows = []
        for plugin_name in available:
            label = f"✓ {plugin_name}" if plugin_name in active else f"+ {plugin_name}"
            rows.append([InlineKeyboardButton(label, callback_data=f"proj_ptog_{plugin_name}|{name}")])
        rows.append([InlineKeyboardButton("« Back", callback_data=f"proj_info_{name}")])
        return InlineKeyboardMarkup(rows)

    async def _on_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query or not query.data:
            return
        if not self._auth(query.from_user):
            await query.answer("Unauthorized.")
            return
        await query.answer()
        # Any button press cancels a pending inline edit
        ctx.user_data.pop("pending_edit", None)

        data = query.data

        if data.startswith("proj_info_"):
            name = data[len("proj_info_"):]
            status = self._pm.status(name)
            await query.edit_message_text(
                f"{name}: {status}", reply_markup=self._proj_detail_markup(name, status)
            )

        elif data == "proj_back":
            markup = self._list_markup()
            await query.edit_message_text(
                self._projects_text() if markup else "No projects configured.", reply_markup=markup
            )

        elif data.startswith("proj_start_"):
            name = data[len("proj_start_"):]
            self._pm.start(name)
            status = self._pm.status(name)
            await query.edit_message_text(
                f"{name}: {status}", reply_markup=self._proj_detail_markup(name, status)
            )

        elif data.startswith("proj_stop_"):
            name = data[len("proj_stop_"):]
            self._pm.stop(name)
            status = self._pm.status(name)
            await query.edit_message_text(
                f"{name}: {status}", reply_markup=self._proj_detail_markup(name, status)
            )

        elif data.startswith("proj_logs_"):
            name = data[len("proj_logs_"):]
            output = self._pm.logs(name)
            if len(output) > 3500:
                output = output[-3500:]
            escaped = output.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            rows = [[InlineKeyboardButton("« Back", callback_data=f"proj_info_{name}")]]
            await query.edit_message_text(
                f"<pre>{escaped}</pre>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows)
            )

        elif data.startswith("proj_edit_"):
            name = data[len("proj_edit_"):]
            rows = [
                [InlineKeyboardButton(field.capitalize().replace("_", " "), callback_data=f"proj_efld_{field}_{name}")]
                for field in _BUTTON_EDIT_FIELDS
            ]
            rows.append([InlineKeyboardButton("« Back", callback_data=f"proj_info_{name}")])
            await query.edit_message_text(
                f"Edit '{name}' — choose field:", reply_markup=InlineKeyboardMarkup(rows)
            )

        elif data.startswith("proj_efld_"):
            parsed = _parse_edit_callback(data)
            if parsed:
                field, name = parsed
                ctx.user_data["pending_edit"] = {"name": name, "field": field}
                await query.edit_message_text(
                    f"Enter new value for {field} of '{name}':\n(/cancel to abort)"
                )

        elif data.startswith("proj_plugins_"):
            name = data[len("proj_plugins_"):]
            available = self._available_plugins()
            if not available:
                await query.edit_message_text(
                    f"No plugins installed.\n\nInstall the link-project-to-chat-plugins package to add plugins.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data=f"proj_info_{name}")]]),
                )
            else:
                await query.edit_message_text(
                    f"Plugins for '{name}':\n✓ = active, + = available\n\nRestart required after changes.",
                    reply_markup=self._plugins_markup(name),
                )

        elif data.startswith("proj_ptog_"):
            suffix = data[len("proj_ptog_"):]
            plugin_name, name = suffix.rsplit("|", 1)
            projects = self._load_projects()
            if name in projects:
                plugins = projects[name].get("plugins", [])
                active_names = [p["name"] for p in plugins]
                if plugin_name in active_names:
                    plugins = [p for p in plugins if p["name"] != plugin_name]
                else:
                    plugins = plugins + [{"name": plugin_name}]
                projects[name]["plugins"] = plugins
                self._save_projects(projects)
            was_running = self._pm.status(name) == "running"
            if was_running:
                self._pm.stop(name)
                self._pm.start(name)
            note = " Project restarted." if was_running else " Restart project to apply."
            await query.edit_message_text(
                f"Plugins for '{name}':\n✓ = active, + = available\n\n{note}",
                reply_markup=self._plugins_markup(name),
            )

        elif data.startswith("proj_remove_"):
            name = data[len("proj_remove_"):]
            projects = self._load_projects()
            if name in projects:
                self._pm.stop(name)
                del projects[name]
                self._save_projects(projects)
            markup = self._list_markup()
            await query.edit_message_text(
                self._projects_text() if markup else "No projects configured.", reply_markup=markup
            )

    @staticmethod
    async def _post_init(app) -> None:
        await app.bot.delete_webhook(drop_pending_updates=True)
        await app.bot.set_my_commands(COMMANDS)

    def build(self):
        app = (
            ApplicationBuilder()
            .token(self._token)
            .post_init(self._post_init)
            .build()
        )
        self._app = app
        for name, handler in {
            "projects": self._on_projects,
            "start_all": self._on_start_all,
            "stop_all": self._on_stop_all,
            "help": self._on_help,
            "edit_project": self._on_edit_project,
        }.items():
            app.add_handler(CommandHandler(name, handler))

        app.add_handler(ConversationHandler(
            entry_points=[CommandHandler("add_project", self._on_add_project)],
            states={
                self.ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, self._add_name)],
                self.ADD_PATH: [MessageHandler(filters.TEXT & ~filters.COMMAND, self._add_path)],
                self.ADD_TOKEN: [
                    CommandHandler("skip", self._add_token),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self._add_token),
                ],
                self.ADD_USERNAME: [
                    CommandHandler("skip", self._add_username),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self._add_username),
                ],
                self.ADD_MODEL: [
                    CommandHandler("skip", self._add_model),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self._add_model),
                ],
            },
            fallbacks=[],
        ))

        app.add_handler(CommandHandler("cancel", self._edit_cancel))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._edit_field_save))
        app.add_handler(CallbackQueryHandler(self._on_callback))
        return app
