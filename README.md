# link-project-to-chat

Chat with Claude about a project via Telegram. Links a local directory to a Telegram bot — send messages, get responses with full project context.

## Security warning

This tool runs `claude --dangerously-skip-permissions` and exposes a `/run` command for arbitrary shell execution. It is a **remote shell** on your machine, protected only by a Telegram username check. Only use it with a bot token you control, restrict access to your username, and never share the token.

## Requirements

- Python 3.11+
- [Claude Code](https://claude.ai/code) installed and authenticated (`claude` on PATH)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

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
# Link a project
link-project-to-chat link /path/to/project --token YOUR_BOT_TOKEN --username your_telegram_username

# Start the bot
link-project-to-chat start --project my-project
```

### Multiple projects

Each project needs its own bot token. Start them in separate terminals:

```bash
link-project-to-chat start --project project-a
link-project-to-chat start --project project-b
```

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
link-project-to-chat link <path> --token TOKEN [--name NAME] [--username USER] [--model MODEL]
link-project-to-chat unlink <name>
link-project-to-chat list
link-project-to-chat start [--project NAME] [--path PATH] [--token TOKEN] [--model MODEL] [--username USER] [--session-id ID]
```

Config is stored at `~/.link-project-to-chat/config.json`.

## License

MIT
