# link-project-to-chat

Chat with an LLM agent about a project via Telegram or a local web UI. Links a local directory to a chat surface — send messages, get responses with full project context. Claude is the default backend; Codex is available via `/backend codex`.

## Security warning

This tool exposes agent CLIs and a `/run` command for shell execution. It is effectively a **remote shell** on your machine. On Telegram, only use it with bot tokens you control and never share them. With the web UI, bind to `127.0.0.1` and don't expose the port to the public internet without your own auth in front.

On Telegram, access is restricted to configured usernames. On first contact, the bot locks each user's numeric Telegram ID — subsequent requests are validated by ID, not username, so a username change cannot bypass access. Multiple users are supported.

## Requirements

- Python 3.11+
- [Claude Code](https://claude.ai/code) installed and authenticated (`claude` on PATH) for the default backend; optional Codex CLI (`codex` on PATH) for `/backend codex`
- For Telegram: a Telegram bot token — create a bot via [@BotFather](https://t.me/BotFather) on Telegram
- For local Web UI: nothing extra beyond the `[web]` install extra
- For Google Chat: the `[google-chat]` install extra and a readable Google service-account JSON file

## Install

```bash
pipx install link-project-to-chat
```

Optional extras:

| Extra | Pulls in | Use it for |
|---|---|---|
| `[create]` | `httpx`, `telethon` | `/create_project`, `/create_team`, BotFather automation |
| `[voice]` | `openai` | OpenAI Whisper API voice transcription |
| `[web]` | `fastapi[standard]`, `jinja2`, `aiosqlite` | Local browser UI transport |
| `[google-chat]` | `fastapi[standard]`, `httpx`, `google-auth` | Google Chat HTTP endpoint transport |
| `[all]` | all of the above | Everything |

Example:

```bash
pipx install "link-project-to-chat[all]"
```

## Quick start

The ad-hoc `--path`/`--token` flow bypasses the config file, so the allowed user
must be supplied on the same command line via `--username`:

```bash
link-project-to-chat start --path /path/to/project --token YOUR_BOT_TOKEN --username your_telegram_username
```

## Setup with config

```bash
# Add your Telegram username (defaults to executor role)
link-project-to-chat configure --add-user your_telegram_username

# Add a project
link-project-to-chat projects add --name myproject --path /path/to/project --token YOUR_BOT_TOKEN

# Start the bot
link-project-to-chat start
```

## Web UI (alternative to Telegram)

Run the same project bot in your browser instead of Telegram. No bot token required.

```bash
pipx install "link-project-to-chat[web]"
link-project-to-chat start --project myproject --transport web --port 8080
# then open http://localhost:8080
```

The web UI is local-only — bind to `127.0.0.1` and never expose it to the public internet without your own auth in front, since the same `/run` shell-execution surface is exposed.

### Google Chat transport

Google Chat bots are managed by the existing `link-project-to-chat.service`
manager unit. To add a Google Chat bot for a project:

1. Create a Cloud Console Chat app on a GCP project and download the
   service-account JSON. One Chat app per GCP project is a Google constraint,
   so each project needs its own GCP project + Chat app + service-account JSON.
2. In the manager Telegram bot, open the project view and tap
   `[Add Google Chat]`. The wizard collects the SA path, port, public URL,
   and slash-command ID.
3. The wizard prints a ready-to-paste nginx vhost snippet. Deploy it on your
   reverse proxy, run certbot for the new subdomain, then `sudo nginx -t && sudo systemctl reload nginx`.
4. From the project's Google Chat space, DM the bot. The reply arrives via
   the manager-spawned google_chat subprocess on the configured port.

Per-project config lives under `projects.<name>.google_chat`. Operational
defaults (host, TTLs, byte caps, audience type) come from the top-level
`google_chat` block. Each project still requires its own GCP project +
Chat app + service-account JSON; one Chat app per GCP project is a Google
constraint.

`rebuild.sh` restarts the manager, which in turn restarts every supervised
google_chat subprocess — no separate `link-project-to-chat-gchat-*.service`
units anymore.

Google Chat v1.2 supports text, slash commands (`/lp2c ...`), card buttons with
HMAC-signed callbacks, thread-aware replies, attachment download
(uploaded-content, capped by `attachment_max_bytes`), prompt dialogs with
form-input submissions, and both `endpoint_url` and `project_number` audience
verification modes.

Known v1.2 limitations (carried forward from the v1 design spec):

- The HMAC secret for callback tokens is per-process (`secrets.token_bytes(32)`
  at start). Any card or prompt posted before a bot restart becomes
  unverifiable afterward. Re-trigger the prompt on the user's next message.
- Prompt submissions are space-bound by default. The transport supports
  sender-binding via `expected_sender_native_id` (`open_prompt` keyword), but
  bot.py wiring to thread the originating user through is a follow-up.
- The duplicate-event cache is in-memory only; a restart resets the seen-event
  set, so a Google retry that arrives across a restart could double-dispatch.
- Native inline `REQUEST_DIALOG` (where the bot returns a dialog synchronously
  from the HTTP route) is intentionally deferred because it conflicts with the
  fast-ack queue model. v1.2 uses card-button + `SUBMIT_DIALOG` instead.
- Outbound file and voice upload is deferred. Google Chat `media.upload`
  requires user OAuth scopes, while this transport uses service-account app
  auth (`chat.bot`); `send_file` and `send_voice` return a thread-aware text
  fallback instead.

## Example session

```
You: what does the auth module do?
Agent: The auth module handles JWT token validation and...

You: /run npm run dev
(runs in background, check output with /tasks → Log)

You: add a test for expired token handling
Agent: I'll add a test for that. [edits file]...

You: /tasks
> #1 npm run dev
+ #2 [claude] add a test for expired token...
```

## How it works

`/run` commands execute in parallel with agent work. Agent turns within a single bot session are **serialized** so they keep one consistent shared session and can't step on each other during interactive follow-ups.

## Project bot commands

| Command | Description |
|---|---|
| (message) | Chat with the active backend in the project context |
| `/run <cmd>` | Run a shell command in the background |
| `/tasks` | List tasks with per-task buttons (log, cancel) |
| `/backend [claude\|codex]` | Show or switch the active backend |
| `/model [name]` | Set backend model |
| `/effort low/medium/high/xhigh/max` | Set backend reasoning depth |
| `/thinking on/off` | Stream live reasoning when the active backend supports it |
| `/context [on\|off\|N]` | Show or set per-chat conversation history depth |
| `/permissions <mode>` | Set backend permission / sandbox mode |
| `/skills` | List available skills |
| `/use [skill]` | Activate a skill (system prompt) — or pick from list |
| `/stop_skill` | Deactivate current skill |
| `/create_skill <name>` | Create a new skill (project or global) |
| `/delete_skill <name>` | Delete a skill |
| `/persona [name]` | Activate a persona (per-message) — or pick from list |
| `/stop_persona` | Deactivate current persona |
| `/create_persona <name>` | Create a new persona (project or global) |
| `/delete_persona <name>` | Delete a persona |
| `/voice` | Show voice transcription status |
| `/compact` | Compress session context |
| `/reset` | Clear the active backend session |
| `/status` | Show bot and backend status |
| `/version` | Show version |
| `/help` | Show available commands |

Claude remains the default backend. Codex is opt-in via `/backend codex` and supports `/model`, `/effort`, `/permissions`, session resume, and token usage reporting in `/status`. Some commands stay capability-gated: for example, live `/thinking`, `/compact`, skills, personas, and allowed/disallowed tool lists are Claude-only unless another backend exposes equivalent support.

## Skills

Skills are markdown files passed as system prompt via `--append-system-prompt`. Claude sees them as background context, like Claude Code's native skill handling. A skill and a persona can be active at the same time.

**Locations (highest priority first):**
1. Per-project: `<project_path>/.claude/skills/<name>.md`
2. App global: `~/.link-project-to-chat/skills/<name>.md`
3. Claude Code user skills: `~/.claude/skills/<name>.md` (including `<name>/SKILL.md` directories)

Higher-priority skills override lower ones with the same name. Claude Code user skills are automatically available.

Use `/create_skill <name>` to create — you'll choose project or global scope, then send the content. `/use` without arguments shows an inline picker.

## Personas

Personas are markdown files prepended to every message. Claude sees them with each message, good for enforcing a specific voice or role.

**Locations (highest priority first):**
1. Per-project: `<project_path>/.claude/personas/<name>.md`
2. App global: `~/.link-project-to-chat/personas/<name>.md`

Use `/create_persona <name>` to create. `/persona` without arguments shows an inline picker.

**Example:** Create `~/.link-project-to-chat/personas/reviewer.md`:
```markdown
You are a senior code reviewer. Focus on bugs, security issues,
and performance problems. Be direct and concise.
```

Then `/persona reviewer` to activate.

## Voice messages

Send voice messages in Telegram and they'll be transcribed and sent to the active backend as text.

### Setup with OpenAI Whisper API (recommended)

```bash
link-project-to-chat setup --stt-backend whisper-api --openai-api-key YOUR_KEY
```

### Setup with local whisper.cpp

Requires [whisper.cpp](https://github.com/ggerganov/whisper.cpp) and `ffmpeg` installed:

```bash
link-project-to-chat setup --stt-backend whisper-cli --whisper-model base
```

### Language hint

For better accuracy with non-English audio:

```bash
link-project-to-chat setup --whisper-language ka
```

### Install with voice extra

```bash
pipx install "link-project-to-chat[voice]"
```

## Multi-user support

Multiple Telegram users can access the same bot:

```bash
# Add users (defaults to executor; append `:viewer` for read-only role)
link-project-to-chat configure --add-user alice
link-project-to-chat configure --add-user bob:viewer

# Remove a user
link-project-to-chat configure --remove-user bob
```

Users can also be managed from the Manager Bot via `/add_user` and `/remove_user`.

## Plugins

Plugins extend the project bot with custom commands, message handlers,
task hooks, button handlers, and Claude prompt context. They are external
Python packages discovered via the `lptc.plugins` entry point group, and
they're **transport-portable**: the same plugin works on Telegram, on the
Web UI, and on any future Discord/Slack/Google Chat transport.

### Activating plugins for a project

Add them to the project's config entry:

```json
{
  "projects": {
    "myproject": {
      "path": "/path/to/project",
      "telegram_bot_token": "...",
      "plugins": [
        {"name": "in-app-web-server"},
        {"name": "diff-reviewer"}
      ]
    }
  }
}
```

Or toggle them in the manager bot: open a project → Plugins → tap a plugin.
Restart the bot after changes.

### Writing a plugin

```python
from link_project_to_chat.plugin import Plugin, BotCommand


class MyPlugin(Plugin):
    name = "my-plugin"
    depends_on = []

    async def start(self):
        ...

    async def stop(self):
        ...

    async def on_message(self, msg):
        # msg is an IncomingMessage — text, sender, chat, files all available
        return False  # True consumes; the agent (Claude/Codex) is skipped

    def get_context(self):
        # Only used when the active backend is Claude; ignored for Codex/Gemini.
        return "Extra system-prompt context"

    def commands(self):
        async def hello(invocation):
            # invocation is a CommandInvocation
            await self._ctx.transport.send_text(invocation.chat, "hi")
        return [BotCommand(command="hello", description="say hi", handler=hello)]
```

Expose it via your plugin package's `pyproject.toml`:

```toml
[project.entry-points."lptc.plugins"]
my-plugin = "my_package:MyPlugin"
```

### Role-based access

Set `allowed_users` on a project to enable per-user roles:

```json
"allowed_users": [
  {"username": "alice", "role": "executor"},
  {"username": "bob", "role": "viewer"}
]
```

Viewers can use `/tasks` (including the per-task **Log** button it surfaces),
`/status`, `/help`, `/version`, `/skills` (listing), `/context` (display), and
any plugin command flagged `viewer_ok`. Executors have the full command set.
`allowed_users` is the sole auth source — an empty list means no one is
authorized (fail-closed). The first request from each user atomically appends
their identity to `locked_identities` and writes it back to the config so
subsequent requests validate by native ID rather than username (preserves the
username-spoof protection from the pre-v1.0 model).

### Group support (Telegram)

By default a project bot only responds in private DMs. To let a project
bot respond in Telegram groups when explicitly addressed:

```bash
link-project-to-chat projects edit myproj respond_in_groups true
```

or, in the manager bot, open the project's detail keyboard and tap
"Respond in groups."

When enabled:

- The bot responds when `@<bot_username>` appears in a group message
  OR the message replies to one of the bot's prior messages.
- All other group messages are silently ignored — no auth check runs,
  nothing is logged.
- The `@<bot_username>` mention is stripped from the prompt before the
  agent sees it.
- `/commands` work in groups with the same role gate as DM. Telegram
  routes `/cmd@MyBot` only to the addressed bot in multi-bot rooms.
- The bot ignores all other bots' messages, including `@<bot_username>`
  from a peer bot (loop defense).

Restart the project bot for the flag change to take effect (the
`python-telegram-bot` filter is set once at startup).

### Safety prompt (v1.2.0+)

By default, the bot is given a guardrail asking it to **describe-and-ask
before destructive action** — no kills, no restarts, no `rm` without
your explicit go-ahead in the current message. This is the same
guardrail that ships with the GitLab fork.

Per-project knobs (`projects edit <name> safety_prompt <value>`):

| Value | Effect |
|---|---|
| `default` (or omit the key) | Use the built-in guardrail |
| Empty string `""` | Disable safety entirely — agent acts without asking |
| Any other string | Replace the built-in with your custom text |

CLI: `link-project-to-chat projects add ... --safety-prompt "..."`.
Manager bot: the field appears in the project-edit text wizard.

### Hot-reload of `allowed_users` (v1.2.0+)

When you `/add_user`, `/remove_user`, `/promote_user`, or
`/demote_user` from the manager bot, the running project bots pick up
the change within 5 s — no restart required. The reload is debounced
and clobber-safe; it never wipes in-flight first-contact identity
locks. Other config fields (token, model, `respond_in_groups`) still
need a bot restart to apply.

### Inter-message group context (v1.2.0+)

When `respond_in_groups=True` and the bot answers in a group, it sees
the chatter that happened between its previous LLM call and the
current one — `[Recent discussion]` is prepended to the prompt. Buffer
is per-chat, 200-message ring, in-memory only (cleared on restart).
Works for any room-kind chat across transports. The current local Web
UI is direct browser-to-bot chat and emits DM-kind messages, so room
context does not apply there until a multi-user Web room model exists.

### `meta_dir` config field (v1.2.0+)

Top-level `meta_dir` in `config.json` redirects per-bot / per-plugin
storage to a different filesystem volume. Default is
`~/.link-project-to-chat/meta/`. Operators upgrading to v1.2.0+ don't
need to migrate existing data; the field is only consulted when bots
start.

## Manager bot

The manager bot controls multiple project bots from a single Telegram chat — start, stop, view logs, add/remove projects, and create new projects automatically.

Manager-started bots inherit the active `--config` path, so custom config files work consistently for project bots, team bots, personas, and team relay bootstrap.

### Setup

```bash
link-project-to-chat configure --username your_telegram_username --manager-token MANAGER_TOKEN
link-project-to-chat start-manager
```

### Manager commands

| Command | Description |
|---|---|
| `/projects` | List projects with start/stop/logs/remove buttons |
| `/start_all` | Start all projects |
| `/stop_all` | Stop all projects |
| `/add_project` | Add a project interactively |
| `/create_project` | Create a project (GitHub repo + auto bot creation) |
| `/create_team` | Create a multi-bot team in a group chat (interactive wizard) |
| `/delete_team` | Delete a team and clean up its bots |
| `/setup` | Configure GitHub PAT and Telegram API credentials |
| `/users` | List authorized users |
| `/add_user <username>` | Add an authorized user |
| `/remove_user <username>` | Remove an authorized user |
| `/edit_project <name> <field> <value>` | Edit a project field |
| `/version` | Show version |
| `/help` | Show commands |

### Automated project creation

The `/create_project` command automates the full project setup:

1. Select a GitHub repo (browse your repos or paste a URL)
2. Automatically create a Telegram bot via BotFather
3. Clone the repo
4. Configure everything

When you later remove a project from the manager, manager-created projects are cleaned up on a best-effort basis: the manager will try to stop the bot, delete the owned repo checkout, and revoke the owned bot via BotFather. If the project path was changed after creation, the repo is left in place intentionally.

**Requirements:**
- GitHub: either `gh` CLI authenticated, or a GitHub PAT (set via `/setup`)
- BotFather automation: Telegram API credentials + Telethon session
- Team creation/deletion automation: install the optional create extras with `pipx install "link-project-to-chat[create]"`

**Setup credentials:**

```bash
# Interactive setup (recommended)
link-project-to-chat setup

# Or via the Manager Bot
/setup
```

The CLI `setup` command handles Telethon phone authentication interactively — this cannot be done through the bot due to Telegram security restrictions.

## CLI reference

```
link-project-to-chat configure --username USER [--remove-username USER] [--manager-token TOKEN]
link-project-to-chat setup [--github-pat PAT] [--telegram-api-id ID] [--telegram-api-hash HASH] [--phone PHONE]

link-project-to-chat projects list
link-project-to-chat projects add --name NAME --path PATH --token TOKEN [--username USER] [--model MODEL]
link-project-to-chat projects remove <name>
link-project-to-chat projects edit <name> <field> <value>

link-project-to-chat start [--project NAME] [--path PATH --token TOKEN] [--username USER] [--model MODEL]
                            [--permission-mode MODE] [--dangerously-skip-permissions]
                            [--allowed-tools TOOLS] [--disallowed-tools TOOLS]
                            [--transport telegram|web|google_chat] [--port PORT]
                            [--google-chat-host HOST] [--google-chat-port PORT]
                            [--google-chat-public-url URL]
link-project-to-chat start-manager
```

Config is stored at `~/.link-project-to-chat/config.json`.

## Deployment

### Run as a systemd service

```bash
sudo tee /etc/systemd/system/link-project-to-chat.service << 'EOF'
[Unit]
Description=Link Project to Chat Manager Bot
After=network.target

[Service]
User=botuser
ExecStart=/home/botuser/.local/bin/link-project-to-chat start-manager
Restart=always
RestartSec=5
Environment=HOME=/home/botuser

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl enable --now link-project-to-chat
```

## Roadmap

**Shipped:**
- Telegram transport
- Local Web UI transport (FastAPI + SSE + SQLite)
- Voice messages (Whisper API or local whisper.cpp)
- Multi-project manager bot, team chats, group/team relay
- Skills and personas
- Pluggable agent backend with `/backend` command and capability-gated commands. Two backends ship: Claude (default) and Codex (opt-in via `/backend codex`); backend abstraction phases 1–4 are complete.
- Provider-aware `/status`, `/context` cross-backend conversation history, Codex `/model`, Codex `/effort`, and Codex `/permissions`.

**Planned (designed, not yet implemented):**
- Discord transport
- Slack transport

Contributions welcome.

## License

MIT
