# link-project-to-chat

Chat with Claude about a project via Telegram. Links a local directory to a Telegram bot — send messages, get responses with full project context.

## Security warning

This tool runs `claude --dangerously-skip-permissions` and exposes a `/run` command for arbitrary shell execution. It is a **remote shell** on your machine. Only use it with a bot token you control and never share the token.

Access is restricted to a single Telegram username. On first contact, the bot locks in that user's numeric Telegram ID — subsequent requests are validated by ID, not username, so a username change cannot bypass access.

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
# Set your Telegram username (once)
link-project-to-chat configure --username your_telegram_username

# Link a project
link-project-to-chat link /path/to/project --token YOUR_BOT_TOKEN

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
[+ #1 pytest | 4s | exit 0]

Running 12 tests...
12 passed in 3.81s

You: add a test for expired token handling
Claude: I'll add a test for that. [edits file]...

You: /tasks
+ #1 [command] 4s pytest
+ #2 [claude] 12s add a test for expired token...
```

## How it works

Claude messages and `/run` commands both execute in **parallel** — they don't block each other. Claude messages share the same session context, so responses build on each other even when sent concurrently.

## Commands

| Command | Description |
|---|---|
| (message) | Chat with Claude in the project context |
| `/run <cmd>` | Run a shell command in the project directory |
| `/tasks` | List active tasks |
| `/log <id>` | Show task output |
| `/cancel [id\|all]` | Cancel tasks |
| `/effort low/medium/high/max` | Set Claude thinking depth |
| `/compact` | Compress session context |
| `/reset` | Clear the Claude session |
| `/status` | Show bot status |

## CLI reference

```
link-project-to-chat configure --username USER
link-project-to-chat link <path> --token TOKEN [--name NAME]
link-project-to-chat unlink <name>
link-project-to-chat list
link-project-to-chat start [--project NAME] [--path PATH] [--token TOKEN] [--username USER] [--session-id ID]
```

Config is stored at `~/.link-project-to-chat/config.json`.

## Planned features

- **Discord support** — same interface over Discord instead of Telegram
- **Voice commands** — transcribe voice messages via a speech-to-text service and forward as text prompts
- **Other coding agents** — pluggable backend to support agents beyond Claude Code (e.g. Aider)

Contributions welcome.

## License

MIT
