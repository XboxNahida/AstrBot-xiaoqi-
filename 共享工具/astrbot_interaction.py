"""Standard-library script client; all interaction policy belongs to the caller."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from uuid import uuid4


class InteractionError(RuntimeError):
    """Report an API status and code without printing script credentials."""

    def __init__(self, status, code):
        self.status = status
        self.code = code
        super().__init__(f"Interaction API {status}: {code}")


class InteractionClient:
    """Call account-bound bot primitives from a user-authored Python script.

    Args:
        config_path: Local channel config written by the verified task account.
    """

    def __init__(self, config_path):
        config = json.loads(Path(config_path).read_text(encoding="utf-8-sig"))
        base = urllib.parse.urlsplit(config["base_url"])
        if (
            base.scheme != "http"
            or base.hostname != "127.0.0.1"
            or base.username
            or base.password
        ):
            raise ValueError("Interaction endpoint must be local loopback")
        self.base_url = config["base_url"].rstrip("/")
        self.channel_id = config["channel_id"]
        self._token = config["token"]
        self.cursor = 0

    @classmethod
    def from_env(cls):
        """Load the private channel config passed by the restricted script runner.

        Returns:
            Client bound to the script's original bot/session/account.
        """
        return cls(os.environ["ASTRBOT_INTERACTION_CONFIG"])

    def _request(self, method, route, body=None, *, query=None, timeout=35):
        suffix = (
            "?"
            + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
            if query
            else ""
        )
        request = urllib.request.Request(
            self.base_url + route + suffix,
            method=method,
            data=json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
            if body is not None
            else None,
            headers={
                "Authorization": "Bearer " + self._token,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                error = json.load(exc).get("error", "request_failed")
            except (ValueError, AttributeError):
                error = "request_failed"
            raise InteractionError(exc.code, error) from None
        except (urllib.error.URLError, TimeoutError):
            raise InteractionError(0, "connection_unavailable") from None

    def events(
        self, *, after_seq=None, since=None, until=None, wait_seconds=0, limit=500
    ):
        """Read true inbound events by cursor and optional UTC time window.

        Args:
            after_seq: Last processed sequence; defaults to this client's cursor.
            since: Optional inclusive UTC epoch start.
            until: Optional inclusive UTC epoch end.
            wait_seconds: Long-poll wait, at most 30 seconds per request.
            limit: Page size, at most 1000; has_more signals additional pages.

        Returns:
            Events, counts, last user message, state, cursor and channel status.
        """
        result = self._request(
            "GET",
            "/v1/events",
            query={
                "after_seq": self.cursor if after_seq is None else after_seq,
                "since": since,
                "until": until,
                "wait_seconds": wait_seconds,
                "limit": limit,
            },
            timeout=wait_seconds + 10,
        )
        self.cursor = result["next_cursor"]
        return result

    def iter_events(self, window_seconds, *, after_seq=None):
        """Yield observed messages during a caller-selected collection window.

        Args:
            window_seconds: Collection duration; the caller may break early.
            after_seq: Initial cursor, or the client's current cursor.

        Yields:
            Individual real message events; no content interpretation is applied.
        """
        if not isinstance(
            window_seconds, (int, float)
        ) or not 0 < window_seconds < float("inf"):
            raise ValueError("Collection window must be finite and positive")
        end = time.monotonic() + window_seconds
        cursor = self.cursor if after_seq is None else after_seq
        while time.monotonic() < end:
            page = self.events(
                after_seq=cursor, wait_seconds=min(30, max(0, end - time.monotonic()))
            )
            cursor = page["next_cursor"]
            yield from page["events"]

    def collect(self, window_seconds, *, after_seq=None):
        """Return messages collected for a fixed window; scripts choose decisions."""
        return list(self.iter_events(window_seconds, after_seq=after_seq))

    def status(self):
        return self._request(
            "GET", "/v1/status", query={"after_seq": self.cursor, "limit": 1}
        )

    def state(self):
        return self._request("GET", "/v1/state")

    def set_state(self, state, expected_revision):
        return self._request(
            "POST",
            "/v1/state",
            {"state": state, "expected_revision": expected_revision},
        )

    def reply(self, message, *, key=None, based_on_seq=None):
        """Request Gemini speech; returned pending is acceptance, never delivery.

        Args:
            message: What should be communicated, expressed using normal persona.
            key: Stable operation key; reuse it after an uncertain HTTP response.
            based_on_seq: Last observed message sequence; stale intents are refused.

        Returns:
            Receipt ID and current delivery state.
        """
        return self._request(
            "POST",
            "/v1/reply",
            {
                "message": message,
                "key": key or uuid4().hex,
                "based_on_seq": self.cursor if based_on_seq is None else based_on_seq,
            },
        )

    def reply_status(self, request_id):
        return self._request(
            "GET", "/v1/replies/" + urllib.parse.quote(request_id, safe="")
        )

    def stop_replies(self, *, pause=True):
        return self._request("POST", "/v1/stop", {"pause": pause})

    def resume_replies(self):
        return self._request("POST", "/v1/resume", {})

    def close(self):
        return self._request("POST", "/v1/close", {})

    def tasks(self, task_id=None):
        return self._request(
            "GET",
            "/v1/tasks"
            + ("/" + urllib.parse.quote(task_id, safe="") if task_id else ""),
        )

    def history(self, *, limit=24):
        return self._request("GET", "/v1/history", query={"limit": limit})

    def tools(self):
        return self._request("GET", "/v1/tools")

    def call_tool(self, name, arguments, *, key):
        return self._request(
            "POST", "/v1/tool-call", {"name": name, "arguments": arguments, "key": key}
        )

    def start_script(self, path, *, key, arguments=None, run_at=None, timeout=600):
        return self._request(
            "POST",
            "/v1/scripts",
            {
                "path": path,
                "key": key,
                "arguments": arguments or [],
                "run_at": run_at,
                "timeout": timeout,
            },
        )

    def script_status(self, script_id):
        return self._request(
            "GET", "/v1/scripts/" + urllib.parse.quote(script_id, safe="")
        )

    def stop_script(self, script_id):
        return self._request(
            "POST",
            "/v1/scripts/" + urllib.parse.quote(script_id, safe="") + "/stop",
            {},
        )
