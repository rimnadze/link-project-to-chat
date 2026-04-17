# link-project-to-chat

Chat with Claude about a project via Telegram. Links a local directory to a Telegram bot — send messages, get responses with full project context.

## Security warning

This tool runs `claude --dangerously-skip-permissions` and exposes a `/run` command for arbitrary shell execution. It is a **remote shell** on your machine. Only use it with a bot token you control and never share the token.

Access is restricted to a list of allowed users by Telegram username. On first contact, the bot locks in that user's numeric Telegram ID — subsequent requests are validated by ID, not username.

## Requirements

- Python 3.11+
- [Claude Code](https://claude.ai/code) installed and authenticated (`claude` on PATH)
- A Telegram bot token — create a bot via [@BotFather](https://t.me/BotFather) on Telegram

## Install

```bash
pipx install link-project-to-chat
```

## Usage

### Quick start (no config file)

```bash
link-project-to-chat start --path /path/to/project --token YOUR_BOT_TOKEN --username your_telegram_username
```

### With config

```bash
# Add a project
link-project-to-chat projects add --name myproject --path /path/to/project --token YOUR_BOT_TOKEN

# Start the bot
link-project-to-chat start
```

### Multiple projects

Each project needs its own bot token. Start them in separate terminals:

```bash
link-project-to-chat start --project project-a
link-project-to-chat start --project project-b
```

## Example session

```
You: what does the auth module do?
Claude: The auth module handles JWT token validation and...

You: /run pytest tests/auth/ -x
Running 12 tests...
12 passed in 3.81s

You: add a test for expired token handling
Claude: I'll add a test for that. [edits file]...

You: /tasks
+ #1 [command] pytest
+ #2 [claude] add a test for expired token...
```

## How it works

Claude messages and `/run` commands execute in a **sequential task queue** — tasks are serialized so Claude always has a consistent view of the project. Claude messages share the same session context, so responses build on each other.

## Commands

| Command | Description |
|---|---|
| (message) | Chat with Claude in the project context |
| `/run <cmd>` | Run a shell command in the project directory |
| `/tasks` | List active tasks with per-task buttons (log, cancel) |
| `/model haiku/sonnet/opus` | Set Claude model |
| `/effort low/medium/high/max` | Set Claude thinking depth |
| `/permissions <mode>` | Set permission mode |
| `/compact` | Compress session context |
| `/reset` | Clear the Claude session |
| `/status` | Show bot status |
| `/help` | Show available commands |

## Multi-user access

Each project can have multiple allowed users with different roles:

```json
"allowed_users": [
  {"username": "alice", "role": "executor"},
  {"username": "bob", "role": "viewer"}
]
```

| Role | Capabilities |
|---|---|
| `executor` | Full access — chat with Claude, run commands, change settings |
| `viewer` | Read-only — sees group chat context, can open diff viewer |

Users can be managed via the manager bot's Users panel without restarting.

## Group chat

Add the bot to a Telegram group and disable privacy mode via BotFather (`/setprivacy → Disable`), then remove and re-add the bot to the group.

In groups the bot only responds when **@mentioned** or when a message is a **reply to the bot**. It includes the replied-to message and recent discussion as context for the LLM.

## CLI reference

```
link-project-to-chat configure [--username USER] [--manager-token TOKEN]

link-project-to-chat projects list
link-project-to-chat projects add --name NAME --path PATH --token TOKEN
                                   [--username USER] [--model MODEL]
                                   [--permission-mode MODE] [--dangerously-skip-permissions]
link-project-to-chat projects remove <name>
link-project-to-chat projects edit <name> <field> <value>

link-project-to-chat start [--project NAME] [--path PATH] [--token TOKEN]
                            [--username USER] [--session-id ID] [--model MODEL]
                            [--permission-mode MODE] [--allowed-tools TOOLS]
                            [--disallowed-tools TOOLS] [--dangerously-skip-permissions]
link-project-to-chat start-manager
```

Config is stored at `~/.link-project-to-chat/config.json`.

## Manager

The manager bot controls multiple project bots from a single Telegram chat — start, stop, view logs, add/remove projects, and manage users without touching the terminal.

### Setup

```bash
link-project-to-chat configure --username your_telegram_username --manager-token MANAGER_TOKEN
link-project-to-chat projects add --name myproject --path /path/to/project --token PROJECT_BOT_TOKEN
link-project-to-chat start-manager
```

### Manager bot commands

| Command | Description |
|---|---|
| `/projects` | List all projects with status and start/stop/logs/remove buttons |
| `/start_all` | Start all projects |
| `/stop_all` | Stop all projects |
| `/add_project` | Add a project interactively |
| `/help` | Show available commands |

## Plugin architecture

Plugins extend bot functionality without modifying core code. They are Python packages registered via an entry point and declared per-project in `config.json`.

### Declaring plugins

```json
"plugins": [
  {"name": "in-app-web-server"},
  {"name": "diff-reviewer"}
]
```

Plugins start in declaration order, respecting `depends_on` dependencies.

### Writing a plugin

```python
from link_project_to_chat.plugin import Plugin, PluginContext, BotCommand

class MyPlugin(Plugin):
    name = "my-plugin"
    depends_on = ["in-app-web-server"]  # optional — starts after these

    async def start(self) -> None:
        """Called when bot starts. Set up resources here."""

    async def stop(self) -> None:
        """Called when bot stops. Clean up resources here."""

    async def on_task_complete(self, task) -> None:
        """Called after each Claude or command task finishes."""

    async def on_tool_use(self, tool: str, path: str | None) -> None:
        """Called when Claude uses a tool (e.g. Write, Edit)."""

    def commands(self) -> list[BotCommand]:
        """Additional Telegram commands this plugin registers."""
        return [BotCommand(command="mycommand", description="Do something", handler=self._on_mycommand)]

    def callbacks(self) -> dict:
        """Inline keyboard callback handlers, keyed by callback_data prefix."""
        return {"myprefix_": self._on_callback}
```

### PluginContext

Each plugin receives a `PluginContext` with shared state:

| Field | Description |
|---|---|
| `bot_name` | Project name from config |
| `project_path` | Absolute path to the project directory |
| `bot_token` | Telegram bot token |
| `bot_username` | Bot's Telegram username |
| `allowed_user_ids` | List of numeric Telegram IDs of allowed users |
| `public_url` | HTTPS public URL (set by `in-app-web-server`) |
| `register_in_app_web_handler` | Register a web handler at `{public_url}/{owner}/{page}` |

Send a message from a plugin:

```python
await self._ctx.send_message(chat_id, "Hello from plugin")
```

### Registering via entry point

In `pyproject.toml`:

```toml
[project.entry-points."lptc.plugins"]
my-plugin = "my_package:MyPlugin"
```

### Available plugins

**`in-app-web-server`** — Embedded aiohttp server with a Cloudflare quick tunnel, serving a public HTTPS URL. Authenticates users via Telegram Mini App `initData` (HMAC-SHA256). Required by plugins that serve web content.

**`diff-reviewer`** — Git diff viewer in Telegram's in-app browser. Shows per-file collapsible diffs with syntax highlighting. In group chats, opens via a deep link to the private chat.

Requires `cloudflared` on PATH for the web server tunnel.

## Planned features

- **Discord support** — same interface over Discord instead of Telegram
- **Voice commands** — transcribe voice messages and forward as text prompts
- **Other coding agents** — pluggable backend to support agents beyond Claude Code

Contributions welcome.

## License

MIT
