"""Telethon group operations for the /create_team flow."""
from __future__ import annotations

import asyncio
import logging

from telethon.errors import FloodWaitError
from telethon.tl.functions.channels import (
    CreateChannelRequest,
    DeleteChannelRequest,
    EditAdminRequest,
    InviteToChannelRequest,
)
from telethon.tl.types import ChatAdminRights

logger = logging.getLogger(__name__)

_FLOOD_WAIT_RETRY_THRESHOLD_SECONDS = 30

# Finding 9: bound every Telethon TL call so a hung Telegram session can't
# wedge the /create_team wizard indefinitely. 30s leaves room for the slow-
# but-honest API path while still surfacing real hangs to the operator.
_TL_TIMEOUT_SECONDS = 30.0


async def _wait_for_tl(coro):
    """Apply the standard Telethon TL timeout to a single awaitable."""
    return await asyncio.wait_for(coro, timeout=_TL_TIMEOUT_SECONDS)


async def _call_with_flood_retry(client, request):
    """Invoke a Telethon TL request, retrying once on short FloodWaits.

    Each underlying ``client(request)`` is wrapped in
    ``asyncio.wait_for(..., timeout=30.0)`` so a hung Telegram session can't
    pin the wizard. ``FloodWaitError`` is still re-raised when the server
    asks for a long wait — that's a separate failure mode from the
    transport-level timeout."""
    try:
        return await _wait_for_tl(client(request))
    except FloodWaitError as e:
        if e.seconds > _FLOOD_WAIT_RETRY_THRESHOLD_SECONDS:
            raise
        logger.info("FloodWait %ds, sleeping and retrying once", e.seconds)
        await asyncio.sleep(e.seconds + 1)
        return await _wait_for_tl(client(request))


async def create_supergroup(client, title: str) -> int:
    """Create a Telegram supergroup. Returns the Bot-API-style chat_id (-100...)."""
    resp = await _call_with_flood_retry(
        client,
        CreateChannelRequest(title=title, about="", megagroup=True),
    )
    raw_id = resp.chats[0].id
    return int(f"-100{raw_id}")


async def add_bot(client, chat_id: int, bot_username: str) -> None:
    """Invite a bot to the group."""
    channel = await _wait_for_tl(client.get_input_entity(chat_id))
    bot_entity = await _wait_for_tl(client.get_entity(bot_username))
    await _call_with_flood_retry(
        client,
        InviteToChannelRequest(channel=channel, users=[bot_entity]),
    )


async def promote_admin(client, chat_id: int, bot_username: str) -> None:
    """Promote a user/bot to admin with the rights group-mode bots need."""
    channel = await _wait_for_tl(client.get_input_entity(chat_id))
    entity = await _wait_for_tl(client.get_entity(bot_username))
    rights = ChatAdminRights(
        change_info=False,
        post_messages=True,
        edit_messages=True,
        delete_messages=True,
        ban_users=True,
        invite_users=True,
        pin_messages=True,
        add_admins=False,
        anonymous=False,
        manage_call=False,
        other=False,
    )
    await _call_with_flood_retry(
        client,
        EditAdminRequest(channel=channel, user_id=entity, admin_rights=rights, rank=""),
    )


async def invite_user(client, chat_id: int, username: str) -> None:
    """Invite a user (by @username, no @ prefix) to the group."""
    channel = await _wait_for_tl(client.get_input_entity(chat_id))
    user_entity = await _wait_for_tl(client.get_entity(username))
    await _call_with_flood_retry(
        client,
        InviteToChannelRequest(channel=channel, users=[user_entity]),
    )


async def delete_supergroup(client, chat_id: int) -> None:
    """Delete a supergroup. Caller must be the creator; admin-only is not enough."""
    channel = await _wait_for_tl(client.get_input_entity(chat_id))
    await _call_with_flood_retry(client, DeleteChannelRequest(channel=channel))
