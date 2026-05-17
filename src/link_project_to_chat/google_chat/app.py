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
        try:
            async with asyncio.timeout(FAST_ACK_BUDGET_SECONDS):
                try:
                    verified = await asyncio.to_thread(verifier, headers)
                except GoogleChatAuthError as exc:
                    logger.warning("Google Chat request rejected: %s", exc)
                    return JSONResponse({"error": "unauthorized"}, status_code=401)
                payload = await request.json()
                await transport.enqueue_verified_event(payload, verified, headers=headers)
        except TimeoutError:
            # The fast-ack budget was missed. Return 200 so Google Chat
            # does not retry the event (which would risk dupes); the
            # dropped event is logged and surfaced as a metric.
            transport.note_fast_ack_timeout()
            return JSONResponse({}, status_code=200)
        return JSONResponse({}, status_code=200)

    return app
