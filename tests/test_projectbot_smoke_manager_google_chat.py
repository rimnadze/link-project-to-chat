"""End-to-end: manager discovers a config with one google_chat project,
spawns a real Google Chat subprocess, and a synthetic POST reaches the
configured port and dispatches through uvicorn.

The test deliberately spawns a real subprocess (no monkeypatching of
``subprocess.Popen``) — it is the only place in the suite that proves the
``--google-chat-config-json`` round-trip (Task 6) feeds a child that
actually binds a port. Earlier tests in ``test_process_manager_google_chat``
fake the spawn at the ``Popen`` boundary; this is the integration tier.

A minimally-valid service-account JSON (with a real RSA private key) is
materialized in tmp_path so ``service_account.Credentials.from_service_account_file``
inside the child does not reject the key payload.
"""
from __future__ import annotations

import asyncio
import json
import os
import site
import socket
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("httpx")
pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")
pytest.importorskip("google.oauth2")
pytest.importorskip("cryptography")

import httpx

from link_project_to_chat.config import (
    AllowedUser,
    Config,
    GoogleChatConfig,
    GoogleChatProjectOverride,
    ProjectConfig,
    save_config,
)


# Snapshot the parent process's user site-packages path at module-import time —
# the tests/conftest.py ``_isolate_home`` autouse fixture redirects ``$HOME``
# to ``tmp_path`` during every test, which makes ``site.getusersitepackages()``
# in any subprocess we spawn point at an empty directory (no httpx, no
# google.oauth2, etc.). The smoke test below feeds this value back via
# ``PYTHONPATH`` so the real Google Chat child sees the same packages the
# parent test loaded.
_PARENT_USER_SITE = site.getusersitepackages()


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _write_fake_service_account(path: Path) -> None:
    """Write a service-account JSON that ``google.oauth2`` will load without error.

    The validator only checks file existence; the credentials loader parses
    the PEM and refuses stub data. Generating a one-shot RSA key keeps the
    test self-contained — no network, no fixture files on disk.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    path.write_text(
        json.dumps(
            {
                "type": "service_account",
                "project_id": "test",
                "private_key_id": "fake",
                "private_key": pem,
                "client_email": "fake@test.iam.gserviceaccount.com",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        )
    )


@pytest.mark.asyncio
@pytest.mark.slow
async def test_manager_starts_google_chat_and_dispatches_synthetic_event(
    tmp_path, monkeypatch,
):
    """ProcessManager.start_google_chat_subprocess('alpha') must spawn a real
    uvicorn child that binds the configured port.

    The signal we accept as "server alive" is any of
    {400, 401, 405, 422}: an empty unauthenticated POST traverses the FastAPI
    app, fails ``verify_request`` (no headers), and the handler answers 401.
    The other codes cover routing edge cases (wrong method, validation error).
    """
    from link_project_to_chat.google_chat.resolver import resolve_project_google_chat
    from link_project_to_chat.manager.process import ProcessManager

    # Re-add the parent's user-site to PYTHONPATH so the child can import the
    # google-chat optional-deps stack (google.oauth2, httpx, fastapi, uvicorn).
    # Without this, the conftest ``_isolate_home`` autouse fixture's HOME
    # redirection makes the child's user-site point at an empty tmp directory.
    existing_pp = os.environ.get("PYTHONPATH", "")
    new_pp = (
        f"{_PARENT_USER_SITE}{os.pathsep}{existing_pp}" if existing_pp
        else _PARENT_USER_SITE
    )
    monkeypatch.setenv("PYTHONPATH", new_pp)

    sa_path = tmp_path / "fake-sa.json"
    _write_fake_service_account(sa_path)

    port = _pick_free_port()
    config = Config(
        projects={
            "alpha": ProjectConfig(
                path=str(tmp_path),
                telegram_bot_token="",  # no telegram bot
                # An allowed user must exist or the CLI bails out before
                # the transport ever binds — see cli.start's fail-closed
                # branch when both project and global allow-lists are empty.
                allowed_users=[AllowedUser(username="smoketester", role="executor")],
                google_chat=GoogleChatProjectOverride(
                    port=port,
                    service_account_file=str(sa_path),
                    public_url="https://alpha.example.test",
                    root_command_id=1,
                ),
            )
        },
        google_chat=GoogleChatConfig(host="127.0.0.1"),
    )

    resolved = resolve_project_google_chat("alpha", config)
    assert resolved is not None
    assert resolved.port == port
    assert resolved.service_account_file == str(sa_path)

    cfg_path = tmp_path / "config.json"
    save_config(config, cfg_path)

    pm = ProcessManager(project_config_path=cfg_path)
    started = pm.start_google_chat_subprocess("alpha")
    assert started is True
    proc = pm._google_chat_procs["alpha"]
    try:
        # uvicorn typically binds in <1s, but the child also imports
        # google.auth + httpx + fastapi cold, so allow up to 10s.
        bound = False
        for _ in range(40):
            if proc.poll() is not None:
                pytest.fail(
                    f"Google Chat child exited early with rc={proc.returncode} "
                    f"before binding port {port}"
                )
            try:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    r = await client.post(
                        f"http://127.0.0.1:{port}/google-chat/events",
                        json={},
                    )
                assert r.status_code in (400, 401, 405, 422), (
                    f"Unexpected status: {r.status_code} body={r.text!r}"
                )
                bound = True
                break
            except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError):
                await asyncio.sleep(0.25)
        if not bound:
            rc = proc.poll()
            pytest.fail(
                f"Google Chat subprocess never bound port {port} (poll={rc})"
            )
    finally:
        pm.stop_google_chat_subprocess("alpha")
        # Belt-and-suspenders: make sure the kill propagated so the next
        # smoke-test invocation can reuse the port range.
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
