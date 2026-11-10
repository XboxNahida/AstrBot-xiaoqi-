"""Host-owned, loopback-only Responses transport for isolated Codex processes."""

import asyncio
import hashlib
import inspect
import ipaddress
import json
import math
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web


@dataclass(repr=False)
class _Grant:
    model: str
    api_base: str
    api_key: str
    idle_timeout: float
    max_attempts: int
    on_event: object
    headers: dict
    proxy: str | None
    active: bool = True
    attempts: dict = field(default_factory=dict)
    requests: set = field(default_factory=set)
    callbacks: set = field(default_factory=set)


class _StreamGuard:
    """Observe bounded SSE frames and redact only the exact host secret.

    Args:
        key: Real upstream key, which must never reach the isolated process.
    """

    def __init__(self, key):
        self.key = key.encode("utf-8")
        self.pending = b""
        self.frame = bytearray()
        self.discard_frame = False
        self._after_cr = False
        self._line_has_data = False
        self.completed = False
        self.failed = False

    def feed(self, chunk):
        """Return safe bytes immediately, retaining only a possible secret prefix.

        Args:
            chunk: Original upstream bytes.

        Returns:
            Bytes with exact key matches replaced; ordinary SSE is unchanged.
        """
        # SSE parsing observes the protocol; it never serializes forwarded data.
        for byte in chunk:
            if self._after_cr:
                self._after_cr = False
                if byte == 10:
                    continue
            if byte in (10, 13):
                # CR ends a line immediately; a following LF belongs to that CR.
                self._after_cr = byte == 13
                if not self.discard_frame:
                    self.frame.append(10)
                if self._line_has_data:
                    self._line_has_data = False
                    continue
                if not self.discard_frame:
                    normalized = bytes(self.frame)
                    labels = {
                        part[6:].strip()
                        for part in normalized.split(b"\n")
                        if part.startswith(b"event:")
                    }
                    self.failed |= bool(
                        labels & {b"error", b"response.failed", b"response.incomplete"}
                    )
                    data = b"\n".join(
                        part[5:].lstrip(b" ")
                        for part in normalized.split(b"\n")
                        if part.startswith(b"data:")
                    )
                    try:
                        event = json.loads(data)
                    except (ValueError, UnicodeError, RecursionError):
                        event = {}
                    if isinstance(event, dict):
                        kind = event.get("type", "")
                        self.completed |= kind == "response.completed"
                        self.failed |= isinstance(kind, str) and kind in {
                            "error",
                            "response.failed",
                            "response.incomplete",
                        }
                    self.frame.clear()
                else:
                    # Oversized frames cannot establish a successful completion.
                    self.failed = True
                    self.discard_frame = False
            else:
                self._line_has_data = True
                if not self.discard_frame:
                    self.frame.append(byte)
                    if len(self.frame) > 1024 * 1024:
                        self.frame.clear()
                        self.discard_frame = True
        data = (self.pending + chunk).replace(self.key, b"[REDACTED]")
        retained = 0
        for count in range(min(len(data), len(self.key) - 1), 0, -1):
            if data.endswith(self.key[:count]):
                retained = count
                break
        self.pending = data[-retained:] if retained else b""
        return data[:-retained] if retained else data


class CodexGateway:
    """Keep upstream credentials in the host while streaming local Responses.

    Args:
        request_total_timeout: Single network request deadline, capped at 600
            seconds. Shorter values support local boundary tests; this does not
            constrain a Codex turn or thread lifetime.
    """

    MAX_FAILURE_SEQUENCES = 256
    MAX_CALLBACKS = 64

    def __init__(self, *, request_total_timeout=600.0):
        if (
            not isinstance(request_total_timeout, (int, float))
            or not math.isfinite(request_total_timeout)
            or request_total_timeout <= 0
        ):
            raise ValueError("Invalid request timeout")
        self._total_timeout = min(float(request_total_timeout), 600.0)
        self._grants = {}
        self._runner = None
        self._session = None
        self._url = None
        self._closed = False
        self._requests = set()
        self._callbacks = set()
        self._close_task = None
        self._close_callers = set()
        self._start_lock = asyncio.Lock()

    async def start(self):
        """Start once on a random loopback port.

        Returns:
            Local base URL without any credentials.

        Raises:
            RuntimeError: If this gateway was already closed or cannot start.
        """
        async with self._start_lock:
            if self._closed:
                raise RuntimeError("Gateway is closed")
            if self._url:
                return self._url
            app = web.Application(client_max_size=64 * 1024 * 1024)
            app.router.add_post("/responses", self._handle)
            runner = web.AppRunner(
                app, access_log=None, handler_cancellation=True, shutdown_timeout=1
            )
            session = None
            try:
                await runner.setup()
                site = web.TCPSite(runner, "127.0.0.1", 0)
                await site.start()
                if self._closed:
                    raise RuntimeError("Gateway is closed")
                session = aiohttp.ClientSession(
                    trust_env=False, cookie_jar=aiohttp.DummyCookieJar()
                )
                port = site._server.sockets[0].getsockname()[1]
            except BaseException as exc:
                if session is not None:
                    await session.close()
                await runner.cleanup()
                if not isinstance(exc, Exception):
                    raise
                raise RuntimeError("Gateway could not start") from None
            self._runner, self._session = runner, session
            self._url = f"http://127.0.0.1:{port}"
            return self._url

    def issue(
        self,
        *,
        model,
        api_base,
        api_key,
        idle_timeout=60,
        max_attempts=3,
        on_event=None,
        headers=None,
        proxy=None,
    ):
        """Issue a process-local grant bound to one host-selected model.

        Args:
            model: Exact permitted Responses model name.
            api_base: Host-selected API root; its path is preserved verbatim.
            api_key: Upstream secret held only by this host process.
            idle_timeout: Connect/read idle boundary, capped at 60 seconds.
            max_attempts: Failed-sequence upstream budget, capped at three.
            on_event: Optional nonblocking metadata observer.
            headers: Optional host-selected headers, never supplied by Codex.
            proxy: Optional explicit host proxy; environment proxies are ignored.

        Returns:
            Cryptographically random temporary Bearer credential, valid until
            revoke/close; no short TTL interrupts a long-running Codex thread.

        Raises:
            ValueError: If the host grant configuration is invalid.
            RuntimeError: If this gateway is closed.
        """
        if self._closed:
            raise RuntimeError("Gateway is closed")
        valid_url = False
        try:
            parsed = urlsplit(api_base)
            local = parsed.hostname == "localhost"
            if parsed.hostname and not local:
                try:
                    local = ipaddress.ip_address(parsed.hostname).is_loopback
                except ValueError:
                    pass
            parsed.port
            valid_url = (
                bool(parsed.hostname)
                and parsed.username is None
                and parsed.password is None
                and "?" not in api_base
                and "#" not in api_base
                and not any(ord(char) < 32 for char in api_base)
                and parsed.scheme in ({"https", "http"} if local else {"https"})
            )
        except (ValueError, TypeError, AttributeError):
            pass
        if (
            not valid_url
            or not isinstance(model, str)
            or not model
            or not isinstance(api_key, str)
            or not api_key
            or "\r" in api_key
            or "\n" in api_key
        ):
            raise ValueError("Invalid upstream configuration")
        if (
            not isinstance(idle_timeout, (int, float))
            or not math.isfinite(idle_timeout)
            or idle_timeout <= 0
            or type(max_attempts) is not int
            or max_attempts < 1
        ):
            raise ValueError("Invalid request limits")
        if (
            (on_event is not None and not callable(on_event))
            or (headers is not None and not isinstance(headers, dict))
            or (proxy is not None and not isinstance(proxy, str))
        ):
            raise ValueError("Invalid host options")
        token = secrets.token_urlsafe(32)
        self._grants[token] = _Grant(
            model,
            api_base.rstrip("/"),
            api_key,
            min(float(idle_timeout), 60.0),
            min(max_attempts, 3),
            on_event,
            dict(headers or {}),
            proxy,
        )
        return token

    def revoke(self, token):
        """Immediately reject new requests and cancel a grant's active work.

        Args:
            token: Credential returned by issue; unknown credentials are ignored.
        """
        grant = self._grants.pop(token, None)
        if grant:
            grant.active = False
            for task in tuple(grant.requests | grant.callbacks):
                task.cancel()

    async def close(self):
        """Close resources once, allowing observers to reenter during cancellation."""
        caller = asyncio.current_task()
        from_callback = caller in self._callbacks
        self._close_callers.add(caller)
        try:
            if self._close_task is None:
                self._closed = True
                self._close_task = asyncio.create_task(self._shutdown())
            # A callback may call close from its cancellation finally block.
            # Resource cleanup must never depend on an observer awaiting it.
            await asyncio.shield(self._close_task)
            if not from_callback:
                pending = tuple(self._callbacks)
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
        finally:
            self._close_callers.discard(caller)

    async def _shutdown(self):
        """Release transport resources independently of observer callers."""
        for grant in self._grants.values():
            grant.active = False
        self._grants.clear()
        for task in tuple(self._requests | self._callbacks):
            if task not in self._close_callers:
                task.cancel()
        if self._requests:
            await asyncio.gather(*tuple(self._requests), return_exceptions=True)
        async with self._start_lock:
            if self._session:
                await self._session.close()
            if self._runner:
                await self._runner.cleanup()

    def _emit(self, grant, kind, attempt, started):
        """Schedule bounded metadata callbacks without blocking the SSE reader.

        Args:
            grant: Host grant whose observer receives this event.
            kind: Transport lifecycle event type.
            attempt: Upstream visit number for this canonical JSON payload.
            started: Monotonic request start time.
        """
        if (
            not grant.active
            or not grant.on_event
            or len(self._callbacks) >= self.MAX_CALLBACKS
        ):
            return
        event = {
            "type": kind,
            "model": grant.model.replace(grant.api_key, "[REDACTED]"),
            "attempt": attempt,
            "elapsed": max(0.0, time.monotonic() - started),
        }

        async def invoke():
            try:
                async with asyncio.timeout(5):
                    if inspect.iscoroutinefunction(grant.on_event):
                        result = grant.on_event(event)
                    else:
                        result = await asyncio.to_thread(grant.on_event, event)
                    if inspect.isawaitable(result):
                        await result
            except Exception:
                # Callback failures must not log credentials or affect transport.
                pass

        task = asyncio.create_task(invoke())
        self._callbacks.add(task)
        grant.callbacks.add(task)
        task.add_done_callback(self._callbacks.discard)
        task.add_done_callback(grant.callbacks.discard)

    async def _handle(self, request):
        """Validate local input, consume one budget unit, and stream upstream.

        Args:
            request: Untrusted isolated-process HTTP request.

        Returns:
            Unmodified ordinary SSE, or a generic credential-free HTTP error.
        """
        authorization = request.headers.get("Authorization", "")
        grant = (
            self._grants.get(authorization.removeprefix("Bearer "))
            if authorization.startswith("Bearer ")
            else None
        )
        if not grant or not grant.active:
            return web.Response(status=401, text="Invalid credential")
        if request.query_string:
            return web.Response(status=400, text="Invalid request")
        task = asyncio.current_task()
        grant.requests.add(task)
        self._requests.add(task)
        downstream = None
        attempt = 0
        succeeded = False
        started = time.monotonic()
        try:
            try:
                raw = await request.read()
                payload = json.loads(raw)
                if (
                    not isinstance(payload, dict)
                    or payload.get("model") != grant.model
                    or payload.get("stream") is not True
                ):
                    return web.Response(status=400, text="Invalid Responses request")
                canonical = json.dumps(
                    payload,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                    allow_nan=False,
                ).encode()
            except web.HTTPRequestEntityTooLarge:
                return web.Response(status=413, text="Request too large")
            except (ValueError, UnicodeError, TypeError, RecursionError):
                return web.Response(status=400, text="Invalid JSON request")
            digest = hashlib.sha256(canonical).digest()
            if not grant.active:
                return web.Response(status=401, text="Invalid credential")
            # Never evict failed sequences: eviction would reopen exhausted budgets.
            if (
                digest not in grant.attempts
                and len(grant.attempts) >= self.MAX_FAILURE_SEQUENCES
            ):
                return web.Response(status=429, text="Request budget exhausted")
            next_attempt = grant.attempts.get(digest, 0) + 1
            if next_attempt > grant.max_attempts:
                return web.Response(status=429, text="Request budget exhausted")
            attempt = next_attempt
            grant.attempts[digest] = attempt
            self._emit(grant, "request_started", attempt, started)
            timeout = aiohttp.ClientTimeout(
                total=self._total_timeout,
                connect=grant.idle_timeout,
                sock_connect=grant.idle_timeout,
                sock_read=grant.idle_timeout,
            )
            headers = {
                key: value
                for key, value in grant.headers.items()
                if key.lower() != "authorization"
            }
            headers.update(
                {
                    "Authorization": "Bearer " + grant.api_key,
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                }
            )
            async with self._session.post(
                grant.api_base + "/responses",
                data=raw,
                headers=headers,
                timeout=timeout,
                proxy=grant.proxy,
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    return web.Response(
                        status=response.status, text="Upstream request failed"
                    )
                downstream = web.StreamResponse(
                    headers={
                        "Content-Type": "text/event-stream",
                        "Cache-Control": "no-cache",
                    }
                )
                await downstream.prepare(request)
                guard = _StreamGuard(grant.api_key)
                async for chunk in response.content.iter_any():
                    safe = guard.feed(chunk)
                    if safe:
                        await downstream.write(safe)
                if guard.pending:
                    await downstream.write(guard.pending)
                await downstream.write_eof()
                succeeded = guard.completed and not guard.failed
                if succeeded:
                    grant.attempts.pop(digest, None)
                return downstream
        except asyncio.CancelledError:
            if request.transport:
                request.transport.close()
            raise
        except Exception:
            # Exception URLs, upstream bodies and headers can contain credentials.
            if downstream is not None:
                if request.transport:
                    request.transport.close()
                return downstream
            return web.Response(status=502, text="Upstream request failed")
        finally:
            if attempt:
                self._emit(
                    grant,
                    "request_completed" if succeeded else "request_failed",
                    attempt,
                    started,
                )
            grant.requests.discard(task)
            self._requests.discard(task)
