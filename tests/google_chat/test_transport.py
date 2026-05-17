from __future__ import annotations

import json
import time
from collections import OrderedDict
from pathlib import Path

import pytest

from link_project_to_chat.config import Config, GoogleChatConfig
from link_project_to_chat.google_chat.cards import make_callback_token
from link_project_to_chat.google_chat.transport import GoogleChatTransport
from link_project_to_chat.transport.base import (
    Button,
    ButtonStyle,
    Buttons,
    ChatKind,
    ChatRef,
    Identity,
    MessageRef,
    PromptKind,
    PromptSpec,
)


FIXTURES = Path(__file__).parent / "fixtures"


def _card_click_payload(token: str, *, user: str = "users/111") -> dict:
    payload = json.loads((FIXTURES / "card_click.json").read_text())
    payload["user"]["name"] = user
    payload["action"]["parameters"][0]["value"] = token
    return payload


def _dialog_submit_payload(token: str, *, user: str = "users/111") -> dict:
    payload = json.loads((FIXTURES / "dialog_submit_text.json").read_text())
    payload["user"]["name"] = user
    payload["common"]["parameters"]["callback_token"] = token
    return payload


def test_google_chat_transport_has_expected_identity():
    cfg = GoogleChatConfig(service_account_file="/tmp/key.json", allowed_audiences=["https://x.test/google-chat/events"])
    transport = GoogleChatTransport(config=cfg)

    assert transport.transport_id == "google_chat"
    assert transport.self_identity == Identity(
        transport_id="google_chat",
        native_id="google_chat:app",
        display_name="Google Chat App",
        handle=None,
        is_bot=True,
    )


def test_google_chat_chat_refs_use_google_transport_id():
    chat = ChatRef("google_chat", "spaces/AAA", ChatKind.ROOM)
    assert chat.transport_id == "google_chat"


def test_verify_request_uses_project_number_when_audiences_empty(monkeypatch):
    from link_project_to_chat.google_chat import auth as auth_mod

    captured = {}

    def fake_verify_google_chat_request(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(auth_mod, "verify_google_chat_request", fake_verify_google_chat_request)
    transport = GoogleChatTransport(
        config=GoogleChatConfig(
            allowed_audiences=[],
            auth_audience_type="project_number",
            project_number="123",
        ),
    )

    transport.verify_request({"authorization": "Bearer token"})

    assert captured["mode"] == "project_number"
    assert captured["audiences"] == ["123"]


@pytest.mark.asyncio
async def test_message_event_normalizes_to_incoming_message():
    transport = GoogleChatTransport(config=GoogleChatConfig(allowed_audiences=["https://x.test/google-chat/events"]))
    seen = []
    transport.on_message(lambda msg: seen.append(msg))
    payload = json.loads((FIXTURES / "message_text.json").read_text())

    await transport.dispatch_event(payload)

    assert seen[0].chat.kind is ChatKind.ROOM
    assert seen[0].text == "hello"
    assert seen[0].message.native["thread_name"] == "spaces/AAA/threads/T1"


@pytest.mark.asyncio
async def test_duplicate_event_dispatches_only_once():
    cfg = GoogleChatConfig(allowed_audiences=["https://x.test/google-chat/events"])
    transport = GoogleChatTransport(config=cfg, serve=False)
    seen = []
    transport.on_message(lambda msg: seen.append(msg.text))
    payload = {
        "type": "MESSAGE",
        "eventTime": "2026-05-16T00:00:00Z",
        "space": {"name": "spaces/AAA", "spaceType": "GROUP_CHAT"},
        "message": {"name": "spaces/AAA/messages/1", "text": "hi"},
        "user": {"name": "users/111", "displayName": "R"},
    }

    await transport.dispatch_event(payload)
    await transport.dispatch_event(payload)
    await transport.dispatch_event(payload)

    assert seen == ["hi"]


@pytest.mark.asyncio
async def test_duplicate_event_dispatches_again_after_hit_moved_past_ttl(monkeypatch):
    cfg = GoogleChatConfig(allowed_audiences=["https://x.test/google-chat/events"])
    transport = GoogleChatTransport(config=cfg, serve=False)
    transport._seen_event_ttl_seconds = 10.0
    seen = []
    transport.on_message(lambda msg: seen.append(msg.text))
    payload_a = {
        "type": "MESSAGE",
        "eventTime": "2026-05-16T00:00:00Z",
        "space": {"name": "spaces/AAA", "spaceType": "GROUP_CHAT"},
        "message": {"name": "spaces/AAA/messages/1", "text": "hi"},
        "user": {"name": "users/111", "displayName": "R"},
    }
    payload_b = {
        "type": "MESSAGE",
        "eventTime": "2026-05-16T00:00:01Z",
        "space": {"name": "spaces/AAA", "spaceType": "GROUP_CHAT"},
        "message": {"name": "spaces/AAA/messages/2", "text": "there"},
        "user": {"name": "users/111", "displayName": "R"},
    }
    key_a = transport._event_idempotency_key(payload_a)
    key_b = transport._event_idempotency_key(payload_b)
    assert key_a is not None
    assert key_b is not None
    transport._seen_event_cache = OrderedDict([(key_a, 0.0), (key_b, 15.0)])

    monkeypatch.setattr("link_project_to_chat.google_chat.transport.time.monotonic", lambda: 9.5)
    assert transport._seen_event(key_a) is True
    assert list(transport._seen_event_cache) == [key_b, key_a]

    monkeypatch.setattr("link_project_to_chat.google_chat.transport.time.monotonic", lambda: 20.0)
    await transport.dispatch_event(payload_a)

    assert seen == ["hi"]


@pytest.mark.asyncio
async def test_command_event_uses_configured_root_command_id():
    transport = GoogleChatTransport(config=GoogleChatConfig(root_command_id=7, allowed_audiences=["https://x.test/google-chat/events"]))
    seen = []
    transport.on_command("help", lambda cmd: seen.append(cmd))
    payload = json.loads((FIXTURES / "app_command_help.json").read_text())

    await transport.dispatch_event(payload)

    assert seen[0].name == "help"
    assert seen[0].args == []


@pytest.mark.asyncio
async def test_app_command_runs_authorizer_and_drops_unauthorized():
    transport = GoogleChatTransport(
        config=GoogleChatConfig(
            root_command_id=7,
            allowed_audiences=["https://x.test/google-chat/events"],
        ),
    )
    transport.set_authorizer(lambda identity: False)

    fired = []
    transport.on_command("help", lambda cmd: fired.append(cmd))

    payload = json.loads((FIXTURES / "app_command_help.json").read_text())
    await transport.dispatch_event(payload)

    assert fired == []


@pytest.mark.asyncio
async def test_app_command_dropped_when_root_command_id_unset():
    transport = GoogleChatTransport(
        config=GoogleChatConfig(allowed_audiences=["https://x.test/google-chat/events"]),
    )
    fired = []
    transport.on_command("help", lambda cmd: fired.append(cmd))

    payload = json.loads((FIXTURES / "app_command_help.json").read_text())
    await transport.dispatch_event(payload)

    assert fired == []


@pytest.mark.asyncio
async def test_card_click_routes_to_button_handler(tmp_path):
    cfg = GoogleChatConfig(
        allowed_audiences=["https://x.test/google-chat/events"],
        callback_token_ttl_seconds=60,
        root_command_id=1,
    )
    transport = GoogleChatTransport(config=cfg, serve=False)

    seen = []
    transport.on_button(lambda click: seen.append(click.value))

    token = make_callback_token(
        secret=transport._callback_secret,
        payload={"space": "spaces/AAA", "kind": "button", "value": "run"},
        ttl_seconds=60,
        now=int(time.time()),
    )

    await transport.dispatch_event(_card_click_payload(token))
    assert seen == ["run"]


@pytest.mark.asyncio
async def test_card_click_prompt_rejects_wrong_sender():
    transport = GoogleChatTransport(
        config=GoogleChatConfig(
            allowed_audiences=["https://x.test/google-chat/events"],
            callback_token_ttl_seconds=60,
        ),
        serve=False,
    )
    chat = ChatRef("google_chat", "spaces/AAA", ChatKind.DM)
    seen = []
    transport.on_prompt_submit(lambda submission: seen.append(submission))

    prompt = await transport.open_prompt(
        chat,
        PromptSpec(key="confirm", title="Confirm", body="Continue?", kind=PromptKind.CONFIRM),
    )
    token = make_callback_token(
        secret=transport._callback_secret,
        payload={
            "space": "spaces/AAA",
            "sender": "users/222",
            "kind": "prompt",
            "prompt_id": prompt.native_id,
            "value": "yes",
        },
        ttl_seconds=60,
        now=int(time.time()),
    )

    await transport.dispatch_event(_card_click_payload(token, user="users/111"))

    assert seen == []
    assert prompt.native_id in transport._pending_prompts

    prompt_without_token_sender = await transport.open_prompt(
        chat,
        PromptSpec(key="confirm2", title="Confirm", body="Continue?", kind=PromptKind.CONFIRM),
    )
    transport._pending_prompts[prompt_without_token_sender.native_id].sender = Identity(
        "google_chat",
        "users/222",
        "Expected",
        None,
        False,
    )
    token_without_sender = make_callback_token(
        secret=transport._callback_secret,
        payload={
            "space": "spaces/AAA",
            "kind": "prompt",
            "prompt_id": prompt_without_token_sender.native_id,
            "value": "yes",
        },
        ttl_seconds=60,
        now=int(time.time()),
    )

    await transport.dispatch_event(_card_click_payload(token_without_sender, user="users/111"))

    assert seen == []
    assert prompt_without_token_sender.native_id in transport._pending_prompts


@pytest.mark.asyncio
async def test_card_click_prompt_drops_expired_pending_prompt():
    transport = GoogleChatTransport(
        config=GoogleChatConfig(
            allowed_audiences=["https://x.test/google-chat/events"],
            callback_token_ttl_seconds=60,
        ),
        serve=False,
    )
    chat = ChatRef("google_chat", "spaces/AAA", ChatKind.DM)
    seen = []
    transport.on_prompt_submit(lambda submission: seen.append(submission))
    prompt = await transport.open_prompt(
        chat,
        PromptSpec(key="confirm", title="Confirm", body="Continue?", kind=PromptKind.CONFIRM),
    )
    transport._pending_prompts[prompt.native_id].expires_at = time.monotonic() - 1
    token = make_callback_token(
        secret=transport._callback_secret,
        payload={
            "space": "spaces/AAA",
            "sender": "users/111",
            "kind": "prompt",
            "prompt_id": prompt.native_id,
            "value": "yes",
        },
        ttl_seconds=60,
        now=int(time.time()),
    )

    await transport.dispatch_event(_card_click_payload(token))

    assert seen == []
    assert prompt.native_id not in transport._pending_prompts


@pytest.mark.asyncio
async def test_card_click_prompt_consumes_pending_prompt_before_dispatch():
    transport = GoogleChatTransport(
        config=GoogleChatConfig(
            allowed_audiences=["https://x.test/google-chat/events"],
            callback_token_ttl_seconds=60,
        ),
        serve=False,
    )
    chat = ChatRef("google_chat", "spaces/AAA", ChatKind.DM)
    seen = []
    transport.on_prompt_submit(lambda submission: seen.append(submission.option))
    prompt = await transport.open_prompt(
        chat,
        PromptSpec(key="confirm", title="Confirm", body="Continue?", kind=PromptKind.CONFIRM),
    )
    token = make_callback_token(
        secret=transport._callback_secret,
        payload={
            "space": "spaces/AAA",
            "sender": "users/111",
            "kind": "prompt",
            "prompt_id": prompt.native_id,
            "value": "yes",
        },
        ttl_seconds=60,
        now=int(time.time()),
    )
    payload = _card_click_payload(token)

    await transport.dispatch_event(payload)
    await transport.dispatch_event(payload)

    assert seen == ["yes"]
    assert prompt.native_id not in transport._pending_prompts


class _FakeClient:
    def __init__(self):
        self.calls = []
        self._counter = 0

    async def create_message(self, space, body, *, thread_name=None, request_id=None, message_reply_option=None):
        self._counter += 1
        self.calls.append({"space": space, "body": body, "thread_name": thread_name, "request_id": request_id})
        return {"name": f"{space}/messages/{self._counter}"}

    async def update_message(self, message_name, body, *, update_mask, allow_missing=False):
        self.calls.append({"message_name": message_name, "body": body, "update_mask": update_mask})
        return {"name": message_name}

    async def upload_attachment(self, space, path, *, mime_type=None, max_bytes=25_000_000, display_name=None):
        self.calls.append(
            {
                "space": space,
                "path": path,
                "mime_type": mime_type,
                "max_bytes": max_bytes,
                "display_name": display_name,
            }
        )
        return {"attachmentDataRef": {"resourceName": f"{space}/attachments/1"}}


@pytest.mark.asyncio
async def test_update_prompt_edits_pending_prompt_message():
    fake = _FakeClient()
    transport = GoogleChatTransport(
        config=GoogleChatConfig(allowed_audiences=["https://x.test/google-chat/events"]),
        client=fake,
        serve=False,
    )
    chat = ChatRef("google_chat", "spaces/AAA", ChatKind.DM)

    display_prompt = await transport.open_prompt(
        chat,
        PromptSpec(key="status", title="Status", body="Working", kind=PromptKind.DISPLAY),
    )
    await transport.update_prompt(
        display_prompt,
        PromptSpec(key="status", title="Status", body="Still working", kind=PromptKind.DISPLAY),
    )

    display_update = fake.calls[-1]
    assert display_update["message_name"] == "spaces/AAA/messages/1"
    assert display_update["body"] == {"text": "Still working"}
    assert display_update["update_mask"] == "text"

    text_prompt = await transport.open_prompt(
        chat,
        PromptSpec(key="name", title="Name", body="Enter name", kind=PromptKind.TEXT),
    )
    await transport.update_prompt(
        text_prompt,
        PromptSpec(key="name", title="Name", body="Enter full name", kind=PromptKind.TEXT),
    )

    text_update = fake.calls[-1]
    assert text_update["message_name"] == "spaces/AAA/messages/2"
    assert text_update["body"]["text"] == "Enter full name"
    assert text_update["body"]["cardsV2"][0]["cardId"] == "lp2c-prompt"
    assert text_update["update_mask"] == "text,cardsV2"


@pytest.mark.asyncio
async def test_update_prompt_clears_cards_when_interactive_prompt_becomes_display():
    fake = _FakeClient()
    transport = GoogleChatTransport(
        config=GoogleChatConfig(allowed_audiences=["https://x.test/google-chat/events"]),
        client=fake,
        serve=False,
    )
    chat = ChatRef("google_chat", "spaces/AAA", ChatKind.DM)

    prompt = await transport.open_prompt(
        chat,
        PromptSpec(key="name", title="Name", body="Enter name", kind=PromptKind.TEXT),
    )
    await transport.update_prompt(
        prompt,
        PromptSpec(key="name", title="Name", body="Name saved", kind=PromptKind.DISPLAY),
    )

    update = fake.calls[-1]
    assert update["message_name"] == "spaces/AAA/messages/1"
    assert update["body"] == {"text": "Name saved", "cardsV2": []}
    assert update["update_mask"] == "text,cardsV2"


@pytest.mark.asyncio
async def test_dialog_submit_routes_to_prompt_submit_handler():
    transport = GoogleChatTransport(
        config=GoogleChatConfig(
            allowed_audiences=["https://x.test/google-chat/events"],
            callback_token_ttl_seconds=60,
        ),
        serve=False,
    )
    chat = ChatRef("google_chat", "spaces/AAA", ChatKind.DM)
    seen = []
    transport.on_prompt_submit(lambda submission: seen.append(submission.text))

    prompt = await transport.open_prompt(
        chat,
        PromptSpec(key="name", title="Name", body="Enter name", kind=PromptKind.TEXT),
    )
    token = make_callback_token(
        secret=transport._callback_secret,
        payload={
            "space": "spaces/AAA",
            "sender": "users/111",
            "kind": "prompt",
            "prompt_id": prompt.native_id,
        },
        ttl_seconds=60,
        now=int(time.time()),
    )

    await transport.dispatch_event(_dialog_submit_payload(token))

    assert seen == ["typed-answer"]
    assert prompt.native_id not in transport._pending_prompts


@pytest.mark.asyncio
async def test_dialog_submit_rejects_wrong_sender_when_bound():
    transport = GoogleChatTransport(
        config=GoogleChatConfig(
            allowed_audiences=["https://x.test/google-chat/events"],
            callback_token_ttl_seconds=60,
        ),
        serve=False,
    )
    chat = ChatRef("google_chat", "spaces/AAA", ChatKind.DM)
    seen = []
    transport.on_prompt_submit(lambda submission: seen.append(submission.text))

    prompt = await transport.open_prompt(
        chat,
        PromptSpec(key="name", title="Name", body="Enter name", kind=PromptKind.TEXT),
        expected_sender_native_id="users/111",
    )
    token = make_callback_token(
        secret=transport._callback_secret,
        payload={
            "space": "spaces/AAA",
            "sender": "users/111",
            "kind": "prompt",
            "prompt_id": prompt.native_id,
        },
        ttl_seconds=60,
        now=int(time.time()),
    )

    await transport.dispatch_event(_dialog_submit_payload(token, user="users/222"))

    assert seen == []
    assert prompt.native_id in transport._pending_prompts


@pytest.mark.asyncio
async def test_send_text_preserves_thread_name_in_reply_to_native():
    fake = _FakeClient()
    transport = GoogleChatTransport(
        config=GoogleChatConfig(allowed_audiences=["https://x.test/google-chat/events"]),
        client=fake,
    )
    chat = ChatRef("google_chat", "spaces/AAA", ChatKind.ROOM)
    reply_to = MessageRef(
        "google_chat",
        "spaces/AAA/messages/0",
        chat,
        native={"thread_name": "spaces/AAA/threads/T1"},
    )

    result = await transport.send_text(chat, "hello", reply_to=reply_to)

    assert fake.calls[0]["thread_name"] == "spaces/AAA/threads/T1"
    assert result.native["thread_name"] == "spaces/AAA/threads/T1"


@pytest.mark.asyncio
async def test_send_voice_fallback_preserves_reply_to_thread(tmp_path):
    fake = _FakeClient()
    transport = GoogleChatTransport(
        config=GoogleChatConfig(
            allowed_audiences=["https://x.test/google-chat/events"],
            attachment_max_bytes=123,
        ),
        client=fake,
    )
    chat = ChatRef("google_chat", "spaces/AAA", ChatKind.ROOM)
    path = tmp_path / "voice.opus"
    path.write_bytes(b"opus")
    reply_to = MessageRef(
        "google_chat",
        "spaces/AAA/messages/0",
        chat,
        native={"thread_name": "spaces/AAA/threads/T1"},
    )

    result = await transport.send_voice(chat, path, reply_to=reply_to)

    assert len(fake.calls) == 1
    message_call = fake.calls[0]
    assert message_call["thread_name"] == "spaces/AAA/threads/T1"
    assert "voice.opus" in message_call["body"]["text"]
    assert "attachment" not in message_call["body"]
    assert result.native["thread_name"] == "spaces/AAA/threads/T1"
    assert result.native["is_app_created"] is True


@pytest.mark.asyncio
async def test_send_file_falls_back_to_text_without_upload(tmp_path):
    fake = _FakeClient()
    transport = GoogleChatTransport(
        config=GoogleChatConfig(allowed_audiences=["https://x.test/google-chat/events"]),
        client=fake,
    )
    chat = ChatRef("google_chat", "spaces/AAA", ChatKind.ROOM)
    path = tmp_path / "tmp-random-name"
    path.write_bytes(b"content")

    result = await transport.send_file(chat, path, caption="See attached", display_name="report.txt")

    assert len(fake.calls) == 1
    message_call = fake.calls[0]
    assert message_call["body"] == {
        "text": "See attached\n\n[Google Chat file upload is not available with app authentication: report.txt]",
    }
    assert result.native_id == "spaces/AAA/messages/1"


@pytest.mark.asyncio
async def test_send_text_with_buttons_includes_cards_v2(tmp_path):
    sa = tmp_path / "key.json"
    sa.write_text("{}", encoding="utf-8")

    captured = {}

    class _FakeClient:
        async def create_message(self, space, body, *, thread_name=None, request_id=None, message_reply_option=None):
            captured["body"] = body
            return {"name": f"{space}/messages/1"}

    cfg = GoogleChatConfig(
        service_account_file=str(sa),
        allowed_audiences=["https://x.test/google-chat/events"],
        root_command_id=1,
    )
    transport = GoogleChatTransport(config=cfg, client=_FakeClient(), serve=False)

    chat = ChatRef("google_chat", "spaces/AAA", ChatKind.ROOM)
    buttons = Buttons(rows=[[Button("Run", "run", ButtonStyle.PRIMARY)]])
    await transport.send_text(chat, "hi", buttons=buttons)

    assert captured["body"]["text"] == "hi"
    assert captured["body"]["cardsV2"][0]["cardId"] == "lp2c-buttons"
    assert "cardsV2" not in captured["body"]["cardsV2"][0]


@pytest.mark.asyncio
async def test_text_prompt_reply_fallback_accepts_expected_sender_only():
    transport = GoogleChatTransport(config=GoogleChatConfig(allowed_audiences=["https://x.test/google-chat/events"]))
    chat = ChatRef("google_chat", "spaces/AAA", ChatKind.ROOM)
    sender = Identity("google_chat", "users/1", "R", "r@example.test", False)
    seen = []
    transport.on_prompt_submit(lambda submission: seen.append(submission))

    prompt = await transport.open_prompt(chat, PromptSpec(key="name", title="Name", body="Your name", kind=PromptKind.TEXT))
    await transport.inject_prompt_reply(prompt, sender=sender, text="R")

    assert seen[0].text == "R"
    assert seen[0].option is None


@pytest.mark.asyncio
async def test_unsupported_drive_attachment_sets_unsupported_media():
    transport = GoogleChatTransport(config=GoogleChatConfig(allowed_audiences=["https://x.test/google-chat/events"]))
    seen = []
    transport.on_message(lambda msg: seen.append(msg))
    payload = {
        "type": "MESSAGE",
        "space": {"name": "spaces/AAA", "spaceType": "DIRECT_MESSAGE"},
        "message": {"name": "spaces/AAA/messages/4", "attachment": [{"driveDataRef": {"driveFileId": "1"}}]},
        "user": {"name": "users/111", "displayName": "R"},
    }

    await transport.dispatch_event(payload)

    assert seen[0].has_unsupported_media is True


@pytest.mark.asyncio
async def test_uploaded_content_attachment_also_sets_unsupported_media():
    transport = GoogleChatTransport(config=GoogleChatConfig(allowed_audiences=["https://x.test/google-chat/events"]))
    seen = []
    transport.on_message(lambda msg: seen.append(msg))
    payload = json.loads((FIXTURES / "attachment_uploaded_content.json").read_text())

    await transport.dispatch_event(payload)

    assert seen[0].has_unsupported_media is True


@pytest.mark.asyncio
async def test_attachment_data_ref_downloads_into_files():
    class _FakeClient:
        async def download_attachment(self, resource_name, destination, *, max_bytes):
            assert resource_name == "spaces/AAA/messages/3/attachments/A1"
            assert destination.name == "report.txt"
            assert max_bytes == 123
            destination.write_bytes(b"downloaded")

    transport = GoogleChatTransport(
        config=GoogleChatConfig(
            allowed_audiences=["https://x.test/google-chat/events"],
            attachment_max_bytes=123,
        ),
        client=_FakeClient(),
    )
    seen = []
    captured_path = None

    def handler(msg):
        nonlocal captured_path
        seen.append(msg)
        captured_path = msg.files[0].path
        assert msg.files[0].path.read_bytes() == b"downloaded"

    transport.on_message(handler)
    payload = json.loads((FIXTURES / "attachment_uploaded_content.json").read_text())

    await transport.dispatch_event(payload)

    assert seen[0].has_unsupported_media is False
    assert seen[0].files[0].original_name == "report.txt"
    assert seen[0].files[0].mime_type == "text/plain"
    assert seen[0].files[0].size_bytes == len(b"downloaded")
    assert captured_path is not None
    assert not captured_path.exists()


@pytest.mark.asyncio
async def test_attachment_download_failure_delivers_unsupported_message():
    class _FakeClient:
        async def download_attachment(self, resource_name, destination, *, max_bytes):
            raise ValueError("too large")

    transport = GoogleChatTransport(
        config=GoogleChatConfig(allowed_audiences=["https://x.test/google-chat/events"]),
        client=_FakeClient(),
    )
    seen = []
    transport.on_message(lambda msg: seen.append(msg))
    payload = json.loads((FIXTURES / "attachment_uploaded_content.json").read_text())

    await transport.dispatch_event(payload)

    assert len(seen) == 1
    assert seen[0].files == []
    assert seen[0].has_unsupported_media is True


@pytest.mark.asyncio
async def test_mixed_downloadable_and_unsupported_attachment_delivers_no_files():
    downloaded_paths = []

    class _FakeClient:
        async def download_attachment(self, resource_name, destination, *, max_bytes):
            destination.write_bytes(b"downloaded")
            downloaded_paths.append(destination)

    transport = GoogleChatTransport(
        config=GoogleChatConfig(allowed_audiences=["https://x.test/google-chat/events"]),
        client=_FakeClient(),
    )
    seen = []
    transport.on_message(lambda msg: seen.append(msg))
    payload = json.loads((FIXTURES / "attachment_uploaded_content.json").read_text())
    payload["message"]["attachment"].append({"driveDataRef": {"driveFileId": "1"}})

    await transport.dispatch_event(payload)

    assert len(seen) == 1
    assert seen[0].files == []
    assert seen[0].has_unsupported_media is True
    assert downloaded_paths
    assert all(not path.exists() for path in downloaded_paths)


@pytest.mark.asyncio
async def test_on_ready_callbacks_fire_with_self_identity():
    transport = GoogleChatTransport(
        config=GoogleChatConfig(allowed_audiences=["https://x.test/google-chat/events"]),
    )
    fired_with = []
    transport.on_ready(lambda identity: fired_with.append(identity))

    await transport._fire_on_ready()

    assert fired_with == [transport.self_identity]


@pytest.mark.asyncio
async def test_start_fires_on_ready_callbacks_with_self_identity(tmp_path):
    service_account = tmp_path / "key.json"
    service_account.write_text("{}", encoding="utf-8")
    transport = GoogleChatTransport(
        config=GoogleChatConfig(
            service_account_file=str(service_account),
            allowed_audiences=["https://x.test/google-chat/events"],
            root_command_id=1,
        ),
        client=_FakeClient(),
    )
    fired_with = []
    transport.on_ready(lambda identity: fired_with.append(identity))

    await transport.start()

    assert fired_with == [transport.self_identity]


@pytest.mark.asyncio
async def test_send_typing_is_noop_and_does_not_raise():
    transport = GoogleChatTransport(
        config=GoogleChatConfig(allowed_audiences=["https://x.test/google-chat/events"]),
    )
    chat = ChatRef("google_chat", "spaces/AAA", ChatKind.ROOM)
    await transport.send_typing(chat)


@pytest.mark.asyncio
async def test_enqueue_drops_event_when_queue_full(caplog):
    import asyncio

    transport = GoogleChatTransport(
        config=GoogleChatConfig(allowed_audiences=["https://x.test/google-chat/events"]),
    )
    # Replace the production-sized queue with a tiny one so the overflow
    # path is reachable from a unit test without flooding 256 puts.
    transport._pending_events = asyncio.Queue(maxsize=2)

    verified = object()  # opaque; enqueue does not inspect VerifiedGoogleChatRequest shape

    await transport.enqueue_verified_event({"type": "MESSAGE", "i": 1}, verified, headers={})
    await transport.enqueue_verified_event({"type": "MESSAGE", "i": 2}, verified, headers={})
    assert transport.pending_event_count == 2
    assert transport._overflowed_events == 0

    with caplog.at_level("WARNING", logger="link_project_to_chat.google_chat.transport"):
        await transport.enqueue_verified_event({"type": "MESSAGE", "i": 3}, verified, headers={})

    assert transport.pending_event_count == 2  # queue stayed at maxsize, third event dropped
    assert transport._overflowed_events == 1
    assert any("pending-event queue full" in rec.message for rec in caplog.records)


# Workspace-add-on envelope unwrap -------------------------------------------


def _addon_envelope(chat_block: dict) -> dict:
    """Build a minimal Workspace-add-on envelope around a `chat` block."""
    return {
        "commonEventObject": {"hostApp": "CHAT", "platform": "WEB"},
        "authorizationEventObject": {"userOAuthToken": "redacted"},
        "chat": chat_block,
    }


def test_unwrap_addon_envelope_message_payload():
    transport = GoogleChatTransport(
        config=GoogleChatConfig(allowed_audiences=["https://x.test/google-chat/events"]),
    )
    envelope = _addon_envelope({
        "eventTime": "2026-05-17T20:00:00Z",
        "user": {"name": "users/111", "displayName": "Tester", "email": "t@x.test"},
        "messagePayload": {
            "space": {"name": "spaces/AAA", "spaceType": "DIRECT_MESSAGE"},
            "message": {"name": "spaces/AAA/messages/1", "text": "hi"},
        },
    })

    rewritten = transport._maybe_unwrap_addon_envelope(envelope)

    assert rewritten["type"] == "MESSAGE"
    assert rewritten["space"] == {"name": "spaces/AAA", "spaceType": "DIRECT_MESSAGE"}
    assert rewritten["message"] == {"name": "spaces/AAA/messages/1", "text": "hi"}
    assert rewritten["user"]["name"] == "users/111"
    assert rewritten["eventTime"] == "2026-05-17T20:00:00Z"


def test_unwrap_addon_envelope_app_command_payload():
    transport = GoogleChatTransport(
        config=GoogleChatConfig(allowed_audiences=["https://x.test/google-chat/events"]),
    )
    envelope = _addon_envelope({
        "eventTime": "2026-05-17T20:01:00Z",
        "user": {"name": "users/111", "displayName": "Tester"},
        "appCommandPayload": {
            "space": {"name": "spaces/AAA", "spaceType": "DIRECT_MESSAGE"},
            "message": {"name": "spaces/AAA/messages/2", "text": "/lp2c help"},
            "appCommandMetadata": {"appCommandId": 1, "appCommandType": "SLASH_COMMAND"},
        },
    })

    rewritten = transport._maybe_unwrap_addon_envelope(envelope)

    assert rewritten["type"] == "APP_COMMAND"
    assert rewritten["appCommandMetadata"]["appCommandId"] == 1
    assert rewritten["space"]["name"] == "spaces/AAA"
    assert rewritten["message"]["text"] == "/lp2c help"


def test_unwrap_addon_envelope_button_clicked_payload():
    transport = GoogleChatTransport(
        config=GoogleChatConfig(allowed_audiences=["https://x.test/google-chat/events"]),
    )
    envelope = _addon_envelope({
        "eventTime": "2026-05-17T20:02:00Z",
        "user": {"name": "users/111"},
        "buttonClickedPayload": {
            "space": {"name": "spaces/AAA", "spaceType": "DIRECT_MESSAGE"},
            "message": {"name": "spaces/AAA/messages/3"},
            "action": {"actionMethodName": "lp2c_button_click"},
            "common": {"parameters": {"callback_token": "tok"}},
        },
    })

    rewritten = transport._maybe_unwrap_addon_envelope(envelope)

    assert rewritten["type"] == "CARD_CLICKED"
    assert rewritten["action"]["actionMethodName"] == "lp2c_button_click"
    assert rewritten["common"]["parameters"]["callback_token"] == "tok"


def test_unwrap_addon_envelope_falls_back_to_chat_user_when_payload_missing_user():
    transport = GoogleChatTransport(
        config=GoogleChatConfig(allowed_audiences=["https://x.test/google-chat/events"]),
    )
    envelope = _addon_envelope({
        "eventTime": "2026-05-17T20:03:00Z",
        "user": {"name": "users/222", "displayName": "FromChatBlock"},
        "messagePayload": {
            "space": {"name": "spaces/AAA", "spaceType": "DIRECT_MESSAGE"},
            "message": {"name": "spaces/AAA/messages/4", "text": "hi"},
            # NB: no `user` key inside messagePayload — must inherit from chat block.
        },
    })

    rewritten = transport._maybe_unwrap_addon_envelope(envelope)

    assert rewritten["user"]["name"] == "users/222"
    assert rewritten["user"]["displayName"] == "FromChatBlock"


def test_unwrap_addon_envelope_no_op_for_standalone_payload():
    transport = GoogleChatTransport(
        config=GoogleChatConfig(allowed_audiences=["https://x.test/google-chat/events"]),
    )
    standalone = {
        "type": "MESSAGE",
        "space": {"name": "spaces/AAA", "spaceType": "DIRECT_MESSAGE"},
        "user": {"name": "users/111"},
        "message": {"name": "spaces/AAA/messages/1", "text": "hi"},
    }

    # No commonEventObject → not an add-on envelope → return unchanged.
    assert transport._maybe_unwrap_addon_envelope(standalone) is standalone


def test_unwrap_addon_envelope_passes_through_when_chat_block_has_no_payload():
    """Envelope with `commonEventObject` but a `chat` block we don't recognise
    (e.g. add-on lifecycle events Chat doesn't expose to us) should fall through
    so the outer dispatcher logs an unknown-type debug line rather than raise."""
    transport = GoogleChatTransport(
        config=GoogleChatConfig(allowed_audiences=["https://x.test/google-chat/events"]),
    )
    envelope = _addon_envelope({"eventTime": "2026-05-17T20:04:00Z", "user": {"name": "users/111"}})

    assert transport._maybe_unwrap_addon_envelope(envelope) is envelope
