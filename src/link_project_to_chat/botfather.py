from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

try:
    from telethon import TelegramClient
    from telethon.tl.types import User
except ImportError:
    TelegramClient = None  # type: ignore[assignment, misc]

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"\d{7,15}:[A-Za-z0-9_-]{20,50}")
_BOTFATHER = "BotFather"
# Matches BotFather throttle replies like "Sorry, too many attempts. Please
# try again in 8 seconds." or "…try again in 1 minute."
_RATE_LIMIT_RE = re.compile(
    r"try again in (\d+)\s*(second|minute|hour)s?", re.IGNORECASE,
)


class BotFatherRateLimit(Exception):
    """BotFather is throttling /newbot (or similar). Caller should back off.

    ``retry_after`` is the server-hinted wait in seconds (defaults to 60 if
    the reply mentions throttling without a concrete duration).
    """

    def __init__(self, message: str, retry_after: float = 60.0) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class BotFatherTimeout(Exception):
    """BotFather (or Telegram itself) didn't respond within the per-call
    timeout. Caller should surface "BotFather is slow — try again." instead
    of letting the wizard hang indefinitely (findings 4+9)."""


_TELETHON_TIMEOUT_SECONDS = 30.0


async def _bf_call(coro, step: str):
    """Wrap a single Telethon awaitable in asyncio.wait_for so a hung
    Telegram session can't pin the whole wizard. Converts TimeoutError to
    a domain-specific BotFatherTimeout that the caller can surface."""
    try:
        return await asyncio.wait_for(coro, timeout=_TELETHON_TIMEOUT_SECONDS)
    except asyncio.TimeoutError as exc:
        raise BotFatherTimeout(
            f"BotFather call timed out at {step} after "
            f"{_TELETHON_TIMEOUT_SECONDS:.0f}s — try again."
        ) from exc


def _parse_rate_limit(text: str) -> float | None:
    """Return the hinted retry_after in seconds if ``text`` is a BotFather
    throttle reply, else None. Falls back to 60s when the text signals
    throttling but doesn't include a concrete duration.
    """
    if not text:
        return None
    lowered = text.lower()
    if "too many attempts" not in lowered and "please try again" not in lowered:
        return None
    match = _RATE_LIMIT_RE.search(text)
    if not match:
        return 60.0
    n = int(match.group(1))
    unit = match.group(2).lower()
    if unit.startswith("min"):
        return n * 60.0
    if unit.startswith("hour"):
        return n * 3600.0
    return float(n)


# Telegram requires bot usernames end in "bot". Pre-v1.0 used "_claude_bot",
# which baked the backend into the public handle — wrong now that the backend
# is abstracted (Claude / Codex / Gemini are interchangeable behind /backend).
# Newly-created bots get the generic "_bot" suffix; existing bots keep their
# BotFather-registered handles (Telegram doesn't let us rename bot usernames
# without recreating).
_BOT_USERNAME_SUFFIX = "_bot"
_BOT_USERNAME_MAX = 32  # Telegram cap


def sanitize_bot_username(name: str) -> str:
    """Convert a project name to a valid Telegram bot username.

    Telegram requires the username to start with a letter, be 5–32 chars,
    contain only Latin letters/digits/underscores, and end with "bot".
    Without the leading-letter guard, digit-leading project names like
    "2024-foo" produce "2024_foo_bot" which BotFather rejects with
    "Sorry, this username is invalid." — we prefix "p_" to keep it valid.
    """
    clean = re.sub(r"[^a-z0-9_]", "_", name.lower().replace("-", "_"))
    clean = re.sub(r"_+", "_", clean).strip("_")
    if not clean:
        clean = "project"
    elif not clean[0].isalpha():
        clean = "p_" + clean
    max_prefix = _BOT_USERNAME_MAX - len(_BOT_USERNAME_SUFFIX)
    if len(clean) > max_prefix:
        clean = clean[:max_prefix].rstrip("_")
    return f"{clean}{_BOT_USERNAME_SUFFIX}"


def extract_token(text: str) -> str | None:
    """Extract a bot token from BotFather's response text."""
    match = _TOKEN_RE.search(text)
    return match.group(0) if match else None


class BotFatherClient:
    def __init__(
        self,
        api_id: int,
        api_hash: str,
        session_path: Path,
        client: TelegramClient | None = None,
    ):
        if TelegramClient is None:
            raise ImportError(
                "telethon is required for BotFather automation. "
                "Install with: pip install link-project-to-chat[create]"
            )
        self._api_id = api_id
        self._api_hash = api_hash
        self._session_path = session_path
        # If an external client is provided, share it (don't disconnect in dispose).
        self._client: TelegramClient | None = client
        self._owns_client = client is None

    async def _ensure_client(self) -> TelegramClient:
        if self._client is None:
            self._client = TelegramClient(
                str(self._session_path), self._api_id, self._api_hash
            )
        if not self._client.is_connected():
            await self._client.connect()
        return self._client

    @property
    def is_authenticated(self) -> bool:
        return self._session_path.exists()

    async def authenticate(self, phone: str, code_callback, password_callback=None) -> None:
        client = await self._ensure_client()
        if not self._session_path.exists():
            self._session_path.touch(mode=0o600)
        else:
            self._session_path.chmod(0o600)
        await client.start(phone=phone, code_callback=code_callback, password=password_callback)

    async def create_bot(self, display_name: str, username: str) -> str:
        # Findings 4+9: every Telethon call wrapped in asyncio.wait_for so a
        # hung Telegram session can't pin the whole wizard. Timeouts surface
        # as BotFatherTimeout for the caller to render a clean error.
        client = await self._ensure_client()
        if not await _bf_call(client.is_user_authorized(), "is_user_authorized"):
            raise Exception("Not authenticated. Run /setup first.")
        entity = await _bf_call(client.get_entity(_BOTFATHER), "get_entity")

        async def _latest_text() -> str:
            msgs = await _bf_call(client.get_messages(entity, limit=1), "get_messages")
            if not msgs:
                return ""
            return msgs[0].text or ""

        def _raise_if_throttled(response_text: str, step: str) -> None:
            wait = _parse_rate_limit(response_text)
            if wait is not None:
                raise BotFatherRateLimit(
                    f"BotFather throttled at {step}: {response_text[:200]}",
                    retry_after=wait,
                )

        await _bf_call(client.send_message(entity, "/newbot"), "send_message /newbot")
        await asyncio.sleep(1.5)
        # BotFather rate-limits `/newbot` itself (not just username picks) —
        # if we don't check here we keep firing display_name + username at a
        # BotFather that has fallen out of the new-bot flow, then misread its
        # generic help text as an "unexpected" failure and burn suffix retries.
        _raise_if_throttled(await _latest_text(), "/newbot")

        await _bf_call(
            client.send_message(entity, display_name), "send_message display_name",
        )
        await asyncio.sleep(1.5)
        _raise_if_throttled(await _latest_text(), "display_name")

        max_retries = 3
        for attempt in range(max_retries + 1):
            trial_username = username if attempt == 0 else f"{username.rstrip('bot')}_{attempt + 1}_bot"
            if not trial_username.endswith("bot"):
                trial_username += "_bot"
            await _bf_call(
                client.send_message(entity, trial_username), "send_message username",
            )
            await asyncio.sleep(2)
            messages = await _bf_call(
                client.get_messages(entity, limit=1), "get_messages",
            )
            if not messages:
                continue
            response_text = messages[0].text or ""
            token = extract_token(response_text)
            if token:
                logger.info("Created bot @%s", trial_username)
                return token
            # Throttle — surface as a distinct error so the caller can back off
            # rather than spinning through suffixed retries (which only make it worse).
            rate_wait = _parse_rate_limit(response_text)
            if rate_wait is not None:
                raise BotFatherRateLimit(
                    f"BotFather throttled after @{trial_username}: {response_text[:200]}",
                    retry_after=rate_wait,
                )
            if "not available" in response_text.lower() or "already" in response_text.lower():
                logger.info("Username @%s taken, retrying...", trial_username)
                if attempt < max_retries:
                    await asyncio.sleep(3 * (attempt + 1))
                    await _bf_call(
                        client.send_message(entity, "/newbot"), "send_message /newbot",
                    )
                    await asyncio.sleep(1.5)
                    await _bf_call(
                        client.send_message(entity, display_name),
                        "send_message display_name",
                    )
                    await asyncio.sleep(1.5)
                continue
            raise Exception(f"Unexpected BotFather response: {response_text[:200]}")
        raise Exception(f"Failed to create bot after {max_retries} retries. All username variants of '{username}' were taken.")

    async def delete_bot(self, bot_username: str) -> None:
        """Send /deletebot to BotFather, select the bot, confirm with the magic phrase.

        Raises on unexpected BotFather reply so the caller can report.
        """
        client = await self._ensure_client()
        entity = await _bf_call(client.get_entity(_BOTFATHER), "get_entity")
        await _bf_call(client.send_message(entity, "/deletebot"), "send_message /deletebot")
        await asyncio.sleep(1.5)
        await _bf_call(
            client.send_message(entity, f"@{bot_username}"), "send_message @username",
        )
        await asyncio.sleep(1.5)
        # BotFather requires the literal phrase "Yes, I am totally sure." to confirm.
        await _bf_call(
            client.send_message(entity, "Yes, I am totally sure."),
            "send_message confirm",
        )
        await asyncio.sleep(1.5)
        messages = await _bf_call(client.get_messages(entity, limit=1), "get_messages")
        if messages:
            text = (messages[0].text or "").lower()
            if "done" in text or "deleted" in text or "gone" in text:
                logger.info("Deleted bot @%s", bot_username)
                return
            if "try again" in text or _parse_rate_limit(messages[0].text or "") is not None:
                raise BotFatherRateLimit(
                    f"BotFather throttled /deletebot for @{bot_username}",
                    retry_after=_parse_rate_limit(messages[0].text or "") or 60.0,
                )
            logger.warning(
                "Unexpected BotFather reply after deleting @%s: %s",
                bot_username, messages[0].text[:200] if messages[0].text else "",
            )

    async def disable_privacy(self, bot_username: str) -> None:
        """Send /setprivacy to BotFather, select the bot by @username, tap Disable.

        Needed so group-mode bots receive non-command messages from group members
        (Telegram filters them by default).
        """
        client = await self._ensure_client()
        entity = await _bf_call(client.get_entity(_BOTFATHER), "get_entity")
        await _bf_call(
            client.send_message(entity, "/setprivacy"), "send_message /setprivacy",
        )
        await asyncio.sleep(1.5)
        await _bf_call(
            client.send_message(entity, f"@{bot_username}"), "send_message @username",
        )
        await asyncio.sleep(1.5)
        await _bf_call(
            client.send_message(entity, "Disable"), "send_message Disable",
        )

    async def disconnect(self) -> None:
        # Only disconnect if we own the client — a shared external client is
        # the caller's responsibility to manage.
        if self._client and self._client.is_connected() and self._owns_client:
            await self._client.disconnect()
