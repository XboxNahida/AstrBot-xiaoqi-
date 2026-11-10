import asyncio
import importlib
import json
import subprocess
import sys
from contextlib import asynccontextmanager

import aiohttp
import pytest
from aiohttp import web

KEY = "sk-fake-gateway-private-123456"
COMPLETED = b'data: {"type":"response.completed","response":{}}\n\n'
FAILED = b'data: {"type":"response.failed","response":{"error":{}}}\n\n'


def gateway(**kwargs):
    assert importlib.util.find_spec("codex_gateway"), "Responses gateway is missing"
    return importlib.import_module("codex_gateway").CodexGateway(**kwargs)


@asynccontextmanager
async def upstream(handler, path="/original/v9/responses"):
    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.router.add_post(path, handler)
    runner = web.AppRunner(app, handler_cancellation=True)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}" + path.removesuffix("/responses")
    finally:
        await runner.cleanup()


@asynccontextmanager
async def setup(handler, **kwargs):
    async with upstream(handler) as api_base:
        instance = gateway(**kwargs)
        url = await instance.start()
        token = instance.issue(model="gpt-test", api_base=api_base, api_key=KEY)
        async with aiohttp.ClientSession() as client:
            try:
                yield instance, client, url, token
            finally:
                await instance.close()


def post(client, url, token, payload=None, **kwargs):
    return client.post(
        url + "/responses",
        headers={"Authorization": "Bearer " + token, **kwargs.pop("headers", {})},
        json=payload if payload is not None else {"model": "gpt-test", "stream": True},
        **kwargs,
    )


def run(coro):
    asyncio.run(asyncio.wait_for(coro, 8))


@pytest.mark.asyncio
async def test_concurrent_cold_start_owns_exactly_one_listener(monkeypatch):
    module = importlib.import_module("codex_gateway")
    runners, sessions = [], []
    runner_class, session_class = module.web.AppRunner, module.aiohttp.ClientSession

    def runner(*args, **kwargs):
        value = runner_class(*args, **kwargs)
        runners.append(value)
        return value

    def session(*args, **kwargs):
        value = session_class(*args, **kwargs)
        sessions.append(value)
        return value

    monkeypatch.setattr(module.web, "AppRunner", runner)
    monkeypatch.setattr(module.aiohttp, "ClientSession", session)
    instance = gateway()
    try:
        urls = await asyncio.gather(instance.start(), instance.start())
        await instance.close()
        assert urls[0] == urls[1]
        assert len(runners) == len(sessions) == 1
        assert all(s.closed for s in sessions) and not any(r.sites for r in runners)
    finally:
        for value in sessions:
            await value.close()
        for value in runners:
            await value.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["cancel", "close", "failure"])
async def test_start_interruption_cleans_local_resources_and_safe_retry(
    monkeypatch, action
):
    module = importlib.import_module("codex_gateway")
    original = module.web.TCPSite.start
    entered, release = asyncio.Event(), asyncio.Event()
    sites = []

    async def gated(site):
        await original(site)
        sites.append(site)
        if len(sites) == 1:
            entered.set()
            await release.wait()
            if action == "failure":
                raise OSError("synthetic startup failure")

    monkeypatch.setattr(module.web.TCPSite, "start", gated)
    instance = gateway()
    first = asyncio.create_task(instance.start())
    await entered.wait()
    closing = None
    try:
        if action == "cancel":
            first.cancel()
        elif action == "close":
            closing = asyncio.create_task(instance.close())
            await asyncio.sleep(0)
        release.set()
        result = await asyncio.gather(first, return_exceptions=True)
        assert isinstance(result[0], BaseException)
        if closing:
            await closing
            with pytest.raises(RuntimeError):
                await instance.start()
        else:
            assert not sites[0]._server or not sites[0]._server.is_serving()
            assert await instance.start()
        await instance.close()
        assert instance._session is None or instance._session.closed
        assert all(not site._server or not site._server.is_serving() for site in sites)
    finally:
        release.set()
        await instance.close()
        if instance._session:
            await instance._session.close()
        for site in sites:
            await site._runner.cleanup()


def test_first_chunk_is_immediate_and_image_tool_body_and_sse_preserved():
    async def check():
        release = asyncio.Event()
        first = b'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"hello"}\n\n'
        image_tools = {
            "model": "gpt-test",
            "stream": True,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_image",
                            "image_url": "data:image/png;base64,aA==",
                        }
                    ],
                }
            ],
            "tools": [
                {"type": "function", "name": "read", "parameters": {"type": "object"}}
            ],
        }

        async def handler(request):
            assert await request.json() == image_tools
            assert request.headers["Authorization"] == "Bearer " + KEY
            assert "X-Untrusted" not in request.headers
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            await response.write(first)
            await release.wait()
            await response.write(COMPLETED)
            return response

        async with setup(handler) as (_, client, url, token):
            assert url.startswith("http://127.0.0.1:") and token not in url
            async with post(
                client, url, token, image_tools, headers={"X-Untrusted": "evil"}
            ) as response:
                assert response.status == 200
                assert (
                    await asyncio.wait_for(
                        response.content.readexactly(len(first)), 0.5
                    )
                    == first
                )
                release.set()
                assert await response.read() == COMPLETED

    run(check())


def test_rejects_invalid_credentials_model_stream_path_and_oversize():
    async def check():
        calls = 0

        async def handler(_):
            nonlocal calls
            calls += 1
            return web.Response(body=COMPLETED)

        async with setup(handler) as (instance, client, url, token):
            for invalid in ("wrong", ""):
                async with post(client, url, invalid) as response:
                    assert response.status == 401
            for payload in (
                {"model": "other", "stream": True},
                {"model": "gpt-test", "stream": False},
                [],
                {"model": "gpt-test", "stream": 1},
            ):
                async with post(client, url, token, payload) as response:
                    assert response.status == 400
            for path in (
                "/v1/responses",
                "/chat/completions",
                "/responses?url=http://evil",
            ):
                async with client.post(url + path, json={}) as response:
                    assert response.status in (400, 401, 404)
            async with client.get(url + "/responses") as response:
                assert response.status == 405
            async with client.post(
                url + "/responses",
                headers={"Authorization": "Bearer " + token},
                data=b"x" * (64 * 1024 * 1024 + 1),
            ) as response:
                assert response.status == 413
            instance.revoke(token)
            async with post(client, url, token) as response:
                assert response.status == 401
            assert calls == 0

    run(check())


@pytest.mark.parametrize(
    "ending",
    [
        b"",
        FAILED,
        COMPLETED + FAILED,
        b'event: error\ndata: {"error":{"message":"bad"}}\n\n' + COMPLETED,
    ],
)
def test_failure_budget_is_canonical_capped_and_new_payload_independent(ending):
    async def check():
        calls = 0

        async def handler(_):
            nonlocal calls
            calls += 1
            return web.Response(body=ending, content_type="text/event-stream")

        async with setup(handler) as (_, client, url, token):
            for index in range(4):
                payload = (
                    {"stream": True, "model": "gpt-test"}
                    if index % 2
                    else {"model": "gpt-test", "stream": True}
                )
                async with post(client, url, token, payload) as response:
                    await response.read()
                    assert response.status == (429 if index == 3 else 200)
            assert calls == 3
            async with post(
                client,
                url,
                token,
                {"model": "gpt-test", "stream": True, "input": "next tool round"},
            ) as response:
                await response.read()
                assert response.status == 200
            assert calls == 4

    run(check())


def test_completed_releases_budget_and_observer_is_nonblocking_and_isolated():
    async def check():
        events, cancelled = [], asyncio.Event()

        async def handler(_):
            return web.Response(body=COMPLETED, content_type="text/event-stream")

        async def observer(event):
            events.append(event)
            if event["type"] == "request_started":
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            else:
                raise ValueError(KEY)

        async with upstream(handler) as api_base:
            instance = gateway()
            url = await instance.start()
            token = instance.issue(
                model="gpt-test",
                api_base=api_base,
                api_key=KEY,
                on_event=observer,
                max_attempts=9,
            )
            async with aiohttp.ClientSession() as client:
                for _ in range(5):
                    async with post(client, url, token) as response:
                        assert response.status == 200
                        assert await response.read() == COMPLETED
            await instance.close()
            assert cancelled.is_set()
            assert any(item["type"] == "request_completed" for item in events)
            assert KEY not in repr(events) and "input" not in repr(events)

    run(check())


@pytest.mark.parametrize("action", ["disconnect", "revoke", "close"])
def test_disconnect_revoke_and_close_cancel_upstream_immediately(action):
    async def check():
        ended = asyncio.Event()

        async def handler(request):
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            await response.write(b"data: {}\n\n")
            try:
                await asyncio.Event().wait()
            finally:
                ended.set()

        async with setup(handler) as (instance, client, url, token):
            response = await post(client, url, token)
            assert await response.content.readexactly(10) == b"data: {}\n\n"
            if action == "disconnect":
                response.close()
            elif action == "revoke":
                instance.revoke(token)
            else:
                await instance.close()
            await asyncio.wait_for(ended.wait(), 0.8)
            response.close()

    run(check())


def test_non200_and_redirect_do_not_leak_key_or_body_and_do_not_retry():
    async def check():
        calls = 0

        async def handler(_):
            nonlocal calls
            calls += 1
            return web.Response(
                status=307 if calls == 1 else 401,
                body=KEY.encode(),
                headers={"Location": "http://127.0.0.1:1/evil", "X-Secret": KEY},
            )

        async with setup(handler) as (_, client, url, token):
            for expected in (307, 401):
                async with post(client, url, token, allow_redirects=False) as response:
                    assert response.status == expected
                    assert KEY not in await response.text()
                    assert KEY not in repr(response.headers)
            assert calls == 2

    run(check())


def test_cross_chunk_exact_key_redaction_does_not_filter_business_fields():
    async def check():
        release = asyncio.Event()
        content = (
            b'data: {"type":"response.output_text.delta","delta":"'
            + KEY.encode()
            + b'","token":"business","password":"sample"}\n\n'
        )

        async def handler(request):
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            await response.write(content[:55])
            await release.wait()
            for byte in content[55:]:
                await response.write(bytes([byte]))
                await asyncio.sleep(0)
            await response.write(COMPLETED)
            return response

        async with setup(handler) as (_, client, url, token):
            async with post(client, url, token) as response:
                prefix = await asyncio.wait_for(response.content.read(20), 0.5)
                release.set()
                result = prefix + await response.read()
                assert KEY.encode() not in result
                assert b'"token":"business","password":"sample"' in result
                assert result.endswith(COMPLETED)
                assert (
                    json.loads(result.split(b"data: ")[1].split(b"\n")[0])["delta"]
                    != KEY
                )

    run(check())


@pytest.mark.parametrize("mode", ["idle", "total"])
def test_network_idle_and_total_deadlines_are_independent(mode):
    async def check():
        ended = asyncio.Event()

        async def handler(request):
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            try:
                while True:
                    await response.write(b"data: {}\n\n")
                    await asyncio.sleep(0.03 if mode == "total" else 1)
            finally:
                ended.set()

        async with upstream(handler) as api_base:
            instance = gateway(request_total_timeout=0.18 if mode == "total" else 1)
            url = await instance.start()
            token = instance.issue(
                model="gpt-test", api_base=api_base, api_key=KEY, idle_timeout=0.08
            )
            async with aiohttp.ClientSession() as client:
                async with post(client, url, token) as response:
                    with pytest.raises(aiohttp.ClientPayloadError):
                        await asyncio.wait_for(response.read(), 0.6)
                await asyncio.wait_for(ended.wait(), 0.5)
            await instance.close()

    run(check())


def test_different_tokens_are_concurrent_and_keys_are_isolated():
    async def check():
        entered = asyncio.Event()
        seen = []

        async def handler(request):
            seen.append(request.headers["Authorization"])
            if len(seen) == 2:
                entered.set()
            await asyncio.wait_for(entered.wait(), 0.5)
            return web.Response(body=COMPLETED)

        async with setup(handler) as (instance, client, url, first):
            api_base = next(iter(instance._grants.values())).api_base
            second = instance.issue(
                model="other", api_base=api_base, api_key="different-secret"
            )

            async def request(token, model):
                async with post(
                    client, url, token, {"model": model, "stream": True}
                ) as response:
                    assert response.status == 200
                    assert await response.read() == COMPLETED

            await asyncio.gather(request(first, "gpt-test"), request(second, "other"))
            assert set(seen) == {"Bearer " + KEY, "Bearer different-secret"}
            assert KEY not in repr(instance) and KEY not in repr(instance._grants)

    run(check())


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/v1",
        "https://user:password@example.com/v1",
        "https://example.com/v1?secret=x",
        "https://example.com/v1#fragment",
        "file:///tmp/key",
        "https://example.com:bad",
        "https://@example.com/v1",
        "https://example.com/v1?",
        "https://example.com/v1#",
    ],
)
def test_host_grant_validates_upstream_url_without_leaking_credentials(url):
    instance = gateway()
    with pytest.raises(ValueError) as error:
        instance.issue(model="gpt-test", api_base=url, api_key=KEY)
    assert KEY not in str(error.value) and url not in str(error.value)


def test_bounded_failure_sequences_fail_closed_without_reopening_old_budget():
    async def check():
        calls = 0

        async def handler(_):
            nonlocal calls
            calls += 1
            return web.Response(status=500, text=KEY)

        async with setup(handler) as (instance, client, url, token):
            instance.MAX_FAILURE_SEQUENCES = 2
            for text, expected in [
                ("first", 500),
                ("second", 500),
                ("third", 429),
                ("first", 500),
                ("first", 500),
                ("first", 429),
            ]:
                async with post(
                    client,
                    url,
                    token,
                    {"model": "gpt-test", "stream": True, "input": text},
                ) as response:
                    assert response.status == expected
                    assert KEY not in await response.text()
            assert calls == 4

    run(check())


def test_explicit_host_headers_environment_proxy_ignored_and_max_attempts_one(
    monkeypatch,
):
    async def check():
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
        monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
        monkeypatch.setenv("NO_PROXY", "")
        monkeypatch.setenv("no_proxy", "")
        calls = 0

        async def handler(request):
            nonlocal calls
            calls += 1
            assert request.headers["X-Host-Header"] == "allowed"
            assert request.headers["Authorization"] == "Bearer " + KEY
            return web.Response(status=503, text=KEY)

        async with upstream(handler) as api_base:
            instance = gateway()
            url = await instance.start()
            token = instance.issue(
                model="gpt-test",
                api_base=api_base,
                api_key=KEY,
                max_attempts=1,
                headers={"X-Host-Header": "allowed", "authorization": "wrong"},
            )
            async with aiohttp.ClientSession(trust_env=False) as client:
                for expected in (503, 429):
                    async with post(client, url, token) as response:
                        assert response.status == expected
                        assert KEY not in await response.text()
            await instance.close()
            assert calls == 1

    run(check())


def test_deadline_before_headers_is_sanitized_and_emits_failure_metadata():
    async def check():
        events = []

        async def handler(_):
            await asyncio.Event().wait()

        async def observer(event):
            events.append(event)

        async with upstream(handler) as api_base:
            instance = gateway(request_total_timeout=0.08)
            url = await instance.start()
            token = instance.issue(
                model="gpt-test", api_base=api_base, api_key=KEY, on_event=observer
            )
            async with aiohttp.ClientSession() as client:
                async with post(client, url, token) as response:
                    assert response.status == 502
                    assert KEY not in await response.text()
            await asyncio.sleep(0)
            assert [event["type"] for event in events] == [
                "request_started",
                "request_failed",
            ]
            assert events[-1]["attempt"] == 1
            assert events[-1]["elapsed"] >= 0.07
            await instance.close()

    run(check())


def test_observation_never_disrupts_unknown_sse_types_or_crlf_tool_events():
    async def check():
        stream = (
            b'data: {"type":{"unknown":true},"payload":"untouched"}\r\n\r\n'
            b'event: response.output_item.added\r\ndata: {"type":"response.output_item.added","item":{"type":"function_call","name":"read","arguments":"{}"}}\r\n\r\n'
            + COMPLETED.replace(b"\n", b"\r\n")
        )

        async def handler(request):
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            for byte in stream:
                await response.write(bytes([byte]))
                await asyncio.sleep(0)
            return response

        async with setup(handler) as (_, client, url, token):
            for _ in range(4):
                async with post(client, url, token) as response:
                    assert response.status == 200
                    assert await response.read() == stream

    run(check())


@pytest.mark.parametrize("mode", ["direct", "cancel_finally", "reentrant"])
def test_observer_lifecycle_close_is_reentrant_and_does_not_wait_for_itself(mode):
    script = r"""
import asyncio
import json
import os
import sys
import time
from codex_gateway import CodexGateway

mode = sys.argv[1]
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)
errors = []
loop.set_exception_handler(lambda _, context: errors.append(type(context.get("exception")).__name__))

async def check():
    instance = CodexGateway()
    await instance.start()
    entered = asyncio.Event()
    returned = asyncio.Event()
    async def observer(event):
        entered.set()
        if mode == "direct":
            await instance.close()
            returned.set()
        else:
            try:
                await asyncio.Event().wait()
            finally:
                await instance.close()
                if mode == "reentrant":
                    await instance.close()
                returned.set()
    token = instance.issue(model="gpt-test", api_base="http://127.0.0.1:1", api_key="fake", on_event=observer)
    instance._emit(instance._grants[token], "request_failed", 1, time.monotonic())
    await entered.wait()
    if mode != "direct":
        asyncio.create_task(instance.close())
        if mode == "reentrant":
            asyncio.create_task(instance.close())
    await asyncio.sleep(0.15)
    state = {"returned": returned.is_set(), "callbacks": len(instance._callbacks), "session_closed": instance._session.closed, "errors": errors}
    print(json.dumps(state), flush=True)

try:
    loop.run_until_complete(check())
finally:
    # Broken self-referential cancellation cannot be safely unwound by asyncio.run.
    os._exit(0)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, mode],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    state = json.loads(result.stdout.strip())
    assert state == {
        "returned": True,
        "callbacks": 0,
        "session_closed": True,
        "errors": [],
    }


@pytest.mark.parametrize("ending", [b"\r", b"\n", b"\r\n"])
def test_sse_all_legal_line_endings_split_across_chunks_release_budget(ending):
    async def check():
        stream = COMPLETED.replace(b"\n", ending)

        async def handler(request):
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            for byte in stream:
                await response.write(bytes([byte]))
                await asyncio.sleep(0)
            return response

        async with setup(handler) as (_, client, url, token):
            for _ in range(4):
                async with post(client, url, token) as response:
                    assert response.status == 200
                    assert await response.read() == stream

    run(check())


def test_cross_grant_cookie_state_is_never_inherited_from_upstream():
    async def check():
        seen = []

        async def handler(request):
            seen.append(
                (request.headers["Authorization"], request.headers.get("Cookie"))
            )
            response = web.Response(body=COMPLETED)
            response.set_cookie("provider_session", "fake-account-A-session")
            return response

        async with upstream(handler) as base:
            # CookieJar ignores IP literals by default, so reproduce a real domain.
            base = base.replace("127.0.0.1", "localhost")
            instance = gateway()
            url = await instance.start()
            first = instance.issue(model="gpt-test", api_base=base, api_key="account-A")
            second = instance.issue(
                model="gpt-test", api_base=base, api_key="account-B"
            )
            async with aiohttp.ClientSession() as client:
                for token in (first, second, first):
                    async with post(client, url, token) as response:
                        assert response.status == 200
                        assert await response.read() == COMPLETED
            await instance.close()
            assert seen == [
                ("Bearer account-A", None),
                ("Bearer account-B", None),
                ("Bearer account-A", None),
            ]

    run(check())
