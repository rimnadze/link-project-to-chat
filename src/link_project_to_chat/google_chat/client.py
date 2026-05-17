from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class GoogleChatAPIError(Exception):
    """The Google Chat REST API returned a non-2xx response."""

    def __init__(self, status_code: int, body: str, endpoint: str):
        self.status_code = status_code
        self.body = body
        self.endpoint = endpoint
        super().__init__(
            f"Google Chat API {endpoint} returned {status_code}: {body[:500]}"
        )


def _safe_json(response, *, endpoint: str) -> dict:
    """Decode a Google Chat REST response, surfacing the actual status + body
    on any non-2xx response so callers see the real server message instead of
    a JSONDecodeError stack.

    Backward-compatible with test fakes that don't set `status_code`: if the
    attribute is missing, the response is assumed to be a 2xx and `.json()`
    is called directly.
    """
    status = getattr(response, "status_code", None)
    if status is not None and not (200 <= status < 300):
        body = ""
        try:
            body = response.text
        except Exception:  # noqa: BLE001
            try:
                body = response.content.decode("utf-8", errors="replace")
            except Exception:
                body = "<unreadable response body>"
        logger.warning(
            "Google Chat API non-2xx response: status=%s endpoint=%s body=%s",
            status, endpoint, body[:500],
        )
        raise GoogleChatAPIError(status, body, endpoint)
    try:
        return response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        body = getattr(response, "text", "") or ""
        logger.warning(
            "Google Chat API non-JSON response: status=%s endpoint=%s body=%s",
            status, endpoint, body[:500],
        )
        raise GoogleChatAPIError(status if status is not None else -1, body, endpoint) from exc


class GoogleChatClient:
    def __init__(self, *, http) -> None:
        self._http = http

    async def create_message(
        self,
        space: str,
        body: dict,
        *,
        thread_name: str | None = None,
        request_id: str | None = None,
        message_reply_option: str | None = None,
    ) -> dict:
        params: dict[str, object] = {}
        if request_id:
            params["requestId"] = request_id
        if message_reply_option:
            params["messageReplyOption"] = message_reply_option
        if thread_name:
            body = dict(body)
            body["thread"] = {"name": thread_name}
        endpoint = f"/v1/{space}/messages"
        response = await self._http.post(endpoint, json=body, params=params)
        return _safe_json(response, endpoint=endpoint)

    async def update_message(
        self,
        message_name: str,
        body: dict,
        *,
        update_mask: str,
        allow_missing: bool = False,
    ) -> dict:
        params = {"updateMask": update_mask, "allowMissing": allow_missing}
        endpoint = f"/v1/{message_name}"
        response = await self._http.patch(endpoint, json=body, params=params)
        return _safe_json(response, endpoint=endpoint)

    async def upload_attachment(
        self,
        space: str,
        path: Path,
        *,
        mime_type: str | None,
        max_bytes: int = 25_000_000,
        display_name: str | None = None,
    ) -> dict:
        """Upload using Google Chat media.upload.

        This endpoint requires a user-authenticated HTTP client with Chat
        message scopes. The default transport uses service-account app auth
        (`chat.bot`) and therefore does not call this helper.
        """
        if max_bytes <= 0:
            raise ValueError("max_bytes must be > 0")
        size_bytes = path.stat().st_size
        if size_bytes > max_bytes:
            raise ValueError(f"Google Chat attachment exceeds max_bytes={max_bytes}")

        metadata = {"filename": display_name or path.name}
        with path.open("rb") as fh:
            response = await self._http.post(
                f"/upload/v1/{space}/attachments:upload",
                params={"uploadType": "multipart"},
                files={
                    "metadata": (None, json.dumps(metadata), "application/json; charset=UTF-8"),
                    "file": (path.name, fh, mime_type or "application/octet-stream"),
                },
            )
        return response.json()

    async def download_attachment(
        self,
        resource_name: str,
        destination: Path,
        *,
        max_bytes: int = 25_000_000,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be > 0")

        destination.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        try:
            async with self._http.stream("GET", f"/v1/media/{resource_name}?alt=media") as response:
                if hasattr(response, "raise_for_status"):
                    response.raise_for_status()
                with destination.open("wb") as fh:
                    async for chunk in response.aiter_bytes():
                        written += len(chunk)
                        if written > max_bytes:
                            raise ValueError(f"Google Chat attachment exceeds max_bytes={max_bytes}")
                        fh.write(chunk)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
