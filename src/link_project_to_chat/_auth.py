from __future__ import annotations

import collections
import logging
import threading
import time

logger = logging.getLogger(__name__)

_config_write_lock = threading.Lock()


class AuthMixin:
    """Username + user_id based auth with rate limiting."""

    _allowed_users: list = []  # list of {"username": str, "user_id": int | None, "role": str}
    _MAX_MESSAGES_PER_MINUTE: int = 30

    def _init_auth(self) -> None:
        self._rate_limits: dict[int, collections.deque] = {}

    def _reload_if_needed(self) -> None:
        """Override in subclasses to hot-reload allowed_users from config."""

    def _on_user_identified(self, user) -> None:
        """Called when user_id is learned for the first time. Override to persist."""

    def _find_role(self, username: str, user) -> tuple[bool, str | None]:
        """Search current allowed_users. Returns (found, role). role=None means rejected."""
        for u in self._allowed_users:
            if u["username"] != username:
                continue
            stored_id = u.get("user_id")
            if stored_id and stored_id != user.id:
                return True, None
            if not stored_id:
                u["user_id"] = user.id
                self._on_user_identified(user)
            return True, u.get("role", "viewer")
        return False, None

    def _get_role(self, user) -> str | None:
        """Returns the user's role ('viewer' or 'executor'), or None if unauthorized."""
        username = (user.username or "").lower().lstrip("@")
        found, role = self._find_role(username, user)
        if not found:
            self._reload_if_needed()
            _, role = self._find_role(username, user)
        return role

    def _auth(self, user) -> bool:
        return self._get_role(user) is not None

    def _is_executor(self, user) -> bool:
        return self._get_role(user) == "executor"

    def _rate_limited(self, user_id: int) -> bool:
        now = time.monotonic()
        timestamps = self._rate_limits.setdefault(user_id, collections.deque())
        while timestamps and now - timestamps[0] > 60:
            timestamps.popleft()
        if len(timestamps) >= self._MAX_MESSAGES_PER_MINUTE:
            return True
        timestamps.append(now)
        return False
