from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from link_project_to_chat.google_chat.client import GoogleChatClient


@dataclass
class _Call:
    url: str
    json: dict | None
    params: dict
    files: dict | None = None


class _FakeResponse:
    def __init__(self, data: dict) -> None:
        self._data = data

    def json(self) -> dict:
        return self._data


class _FakeStreamResponse:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aenter__(self) -> "_FakeStreamResponse":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    def raise_for_status(self) -> None:
        return None

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk


class FakeHttpx:
    def __init__(self) -> None:
        self.calls: list[_Call] = []
        self.stream_calls: list[tuple[str, str]] = []
        self.stream_chunks: list[bytes] = []
        self.next_post_json: dict | None = None

    async def post(
        self,
        url: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
        files: dict | None = None,
    ) -> _FakeResponse:
        self.calls.append(_Call(url=url, json=json, params=params or {}, files=files))
        return _FakeResponse(self.next_post_json or {"name": f"{url}/messages/1"})

    async def patch(self, url: str, *, json: dict, params: dict | None = None) -> _FakeResponse:
        self.calls.append(_Call(url=url, json=json, params=params or {}))
        return _FakeResponse({"name": url})

    def stream(self, method: str, url: str) -> _FakeStreamResponse:
        self.stream_calls.append((method, url))
        return _FakeStreamResponse(self.stream_chunks)


@pytest.fixture
def fake_httpx() -> FakeHttpx:
    return FakeHttpx()


@pytest.mark.asyncio
async def test_create_message_sends_request_id(fake_httpx):
    client = GoogleChatClient(http=fake_httpx)

    await client.create_message("spaces/AAA", {"text": "hello"}, request_id="req-1")

    assert fake_httpx.calls[0].params["requestId"] == "req-1"


@pytest.mark.asyncio
async def test_update_message_requires_update_mask(fake_httpx):
    client = GoogleChatClient(http=fake_httpx)

    await client.update_message("spaces/AAA/messages/1", {"text": "new"}, update_mask="text")

    assert fake_httpx.calls[0].params["updateMask"] == "text"
    assert fake_httpx.calls[0].params.get("allowMissing") is False


@pytest.mark.asyncio
async def test_download_attachment_writes_bytes_under_size_cap(fake_httpx, tmp_path: Path):
    fake_httpx.stream_chunks = [b"abc", b"def"]
    client = GoogleChatClient(http=fake_httpx)
    destination = tmp_path / "report.txt"

    await client.download_attachment(
        "spaces/AAA/messages/3/attachments/A1",
        destination,
        max_bytes=6,
    )

    assert fake_httpx.stream_calls == [
        ("GET", "/v1/media/spaces/AAA/messages/3/attachments/A1?alt=media"),
    ]
    assert destination.read_bytes() == b"abcdef"


@pytest.mark.asyncio
async def test_upload_attachment_posts_multipart_with_resource_name(fake_httpx, tmp_path: Path):
    src = tmp_path / "report.txt"
    src.write_bytes(b"fake file bytes")
    fake_httpx.next_post_json = {
        "attachmentDataRef": {"resourceName": "spaces/AAA/attachments/X1"},
    }
    client = GoogleChatClient(http=fake_httpx)

    result = await client.upload_attachment("spaces/AAA", src, mime_type="text/plain")

    assert result["attachmentDataRef"]["resourceName"] == "spaces/AAA/attachments/X1"
    assert fake_httpx.calls[0].url == "/upload/v1/spaces/AAA/attachments:upload"
    assert fake_httpx.calls[0].params["uploadType"] == "multipart"


@pytest.mark.asyncio
async def test_upload_attachment_uses_display_name_in_metadata(fake_httpx, tmp_path: Path):
    src = tmp_path / "tmp-random-name"
    src.write_bytes(b"fake file bytes")
    client = GoogleChatClient(http=fake_httpx)

    await client.upload_attachment(
        "spaces/AAA",
        src,
        mime_type="text/plain",
        display_name="report.txt",
    )

    metadata = fake_httpx.calls[0].files["metadata"]
    assert json.loads(metadata[1]) == {"filename": "report.txt"}


@pytest.mark.asyncio
async def test_upload_attachment_rejects_oversize_files(fake_httpx, tmp_path: Path):
    src = tmp_path / "large.txt"
    src.write_bytes(b"123456")
    client = GoogleChatClient(http=fake_httpx)

    with pytest.raises(ValueError):
        await client.upload_attachment("spaces/AAA", src, mime_type="text/plain", max_bytes=5)

    assert fake_httpx.calls == []


# GoogleChatAPIError surfacing -----------------------------------------------


class _ErrorResponse:
    """Mimics the parts of httpx.Response we care about for error paths."""

    def __init__(self, status_code: int, text: str = "", invalid_json: bool = False):
        self.status_code = status_code
        self.text = text
        self._invalid_json = invalid_json

    def json(self) -> dict:
        if self._invalid_json:
            import json as _json
            raise _json.JSONDecodeError("Expecting value", self.text, 0)
        return {}


class _ErrorHttpx:
    def __init__(self, response):
        self._response = response

    async def post(self, *args, **kwargs):
        return self._response

    async def patch(self, *args, **kwargs):
        return self._response


@pytest.mark.asyncio
async def test_create_message_raises_google_chat_api_error_on_404(caplog):
    """404 from Google (e.g. an invalid space) must raise GoogleChatAPIError
    with the status, endpoint, and body — not a JSONDecodeError stack."""
    from link_project_to_chat.google_chat.client import GoogleChatAPIError

    response = _ErrorResponse(status_code=404, text="<h1>Not Found</h1>")
    client = GoogleChatClient(http=_ErrorHttpx(response))

    with caplog.at_level("WARNING", logger="link_project_to_chat.google_chat.client"):
        with pytest.raises(GoogleChatAPIError) as exc_info:
            await client.create_message("users/not-a-space", {"text": "hi"})

    assert exc_info.value.status_code == 404
    assert exc_info.value.endpoint == "/v1/users/not-a-space/messages"
    assert "<h1>Not Found</h1>" in exc_info.value.body
    assert any("non-2xx response" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_create_message_raises_google_chat_api_error_on_non_json_2xx(caplog):
    """A 200 with a non-JSON body still surfaces the body instead of
    a confusing JSONDecodeError."""
    from link_project_to_chat.google_chat.client import GoogleChatAPIError

    response = _ErrorResponse(status_code=200, text="<html>broken</html>", invalid_json=True)
    client = GoogleChatClient(http=_ErrorHttpx(response))

    with caplog.at_level("WARNING", logger="link_project_to_chat.google_chat.client"):
        with pytest.raises(GoogleChatAPIError) as exc_info:
            await client.create_message("spaces/AAA", {"text": "hi"})

    assert exc_info.value.status_code == 200
    assert "<html>broken</html>" in exc_info.value.body
    assert any("non-JSON response" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_update_message_raises_google_chat_api_error_on_403(caplog):
    """update_message goes through the same _safe_json wrapper."""
    from link_project_to_chat.google_chat.client import GoogleChatAPIError

    response = _ErrorResponse(status_code=403, text='{"error": "forbidden"}')
    client = GoogleChatClient(http=_ErrorHttpx(response))

    with caplog.at_level("WARNING", logger="link_project_to_chat.google_chat.client"):
        with pytest.raises(GoogleChatAPIError) as exc_info:
            await client.update_message(
                "spaces/AAA/messages/1", {"text": "edit"}, update_mask="text",
            )

    assert exc_info.value.status_code == 403
    assert exc_info.value.endpoint == "/v1/spaces/AAA/messages/1"
