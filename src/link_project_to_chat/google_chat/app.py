from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .auth import GoogleChatAuthError

logger = logging.getLogger(__name__)

FAST_ACK_BUDGET_SECONDS = 2.0


def create_google_chat_app(transport, request_verifier: Callable | None = None) -> FastAPI:
    app = FastAPI()

    @app.post(transport.config.endpoint_path)
    async def google_chat_events(request: Request):
        verifier = request_verifier or transport.verify_request
        headers = dict(request.headers)
        # [TRACE-v4] Log EVERY incoming POST at handler entry, before
        # verification, so we can tell if the button-click POST reaches
        # the bot at all. Strip once the dispatch bug is fixed.
        logger.warning(
            "[TRACE-v4] POST entry: user-agent=%r, content-length=%s, "
            "content-type=%s, host=%s",
            headers.get("user-agent"),
            headers.get("content-length"),
            headers.get("content-type"),
            headers.get("host"),
        )
        try:
            async with asyncio.timeout(FAST_ACK_BUDGET_SECONDS):
                try:
                    verified = await asyncio.to_thread(verifier, headers)
                except GoogleChatAuthError as exc:
                    logger.warning("Google Chat request rejected: %s", exc)
                    return JSONResponse({"error": "unauthorized"}, status_code=401)
                payload = await request.json()
                # [TRACE-v4] Log raw payload keys post-parse, pre-enqueue.
                logger.warning(
                    "[TRACE-v4] POST parsed: payload.keys=%s",
                    sorted(payload.keys()) if isinstance(payload, dict) else f"non-dict:{type(payload).__name__}",
                )
                await transport.enqueue_verified_event(payload, verified, headers=headers)
        except TimeoutError:
            # The fast-ack budget was missed. Return 200 so Google Chat
            # does not retry the event (which would risk dupes); the
            # dropped event is logged and surfaced as a metric.
            transport.note_fast_ack_timeout()
            return JSONResponse({}, status_code=200)
        return JSONResponse({}, status_code=200)

    return app
