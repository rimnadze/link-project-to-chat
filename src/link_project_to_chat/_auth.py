from __future__ import annotations

import collections
import logging
import time

logger = logging.getLogger(__name__)


class AuthMixin:
    """Username-based auth with brute-force protection and rate limiting."""

    _allowed_users: list = []  # list of {"username": str, "user_id": int | None}
    _MAX_MESSAGES_PER_MINUTE: int = 30

    def _init_auth(self) -> None:
        self._rate_limits: dict[int, collections.deque] = {}
        self._failed_auth_counts: dict[int, int] = {}

    def _reload_if_needed(self) -> None:
        """Override in subclasses to hot-reload allowed_users from config."""

    def _on_user_identified(self, user) -> None:
        """Called when a user is matched by username for the first time (user_id was unknown).
        Override in subclasses to persist the discovered user_id."""

    def _get_role(self, user) -> str | None:
        """Returns the user's role ('viewer' or 'executor'), or None if unauthorized."""
        if self._failed_auth_counts.get(user.id, 0) >= 5:
            return None
        username = (user.username or "").lower().lstrip("@")
        for u in self._allowed_users:
            if u["username"] == username:
                if not u.get("user_id"):
                    u["user_id"] = user.id
                    self._on_user_identified(user)
                return u.get("role", "viewer")
        # Not found — try reloading config once, then check again
        self._reload_if_needed()
        for u in self._allowed_users:
            if u["username"] == username:
                self._failed_auth_counts.pop(user.id, None)  # clear prior failures
                if not u.get("user_id"):
                    u["user_id"] = user.id
                    self._on_user_identified(user)
                return u.get("role", "viewer")
        self._failed_auth_counts[user.id] = self._failed_auth_counts.get(user.id, 0) + 1
        return None

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
