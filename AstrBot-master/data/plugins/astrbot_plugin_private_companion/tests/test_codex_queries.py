"""Verify existing Gemini readers expose actual Codex facts and event pages."""

import json

import pytest

from astrbot_plugin_private_companion.task_catalog import TASK_GUIDANCE
from astrbot_plugin_private_companion.tests import (
    test_codex_dispatch as dispatch_helpers,
    test_page_api_tasks as page_helpers,
)
from astrbot_plugin_private_companion.tests.test_task_runtime import event, register

runtime = dispatch_helpers.runtime
routed = dispatch_helpers.routed
page = page_helpers.page


@pytest.mark.asyncio
async def test_all_codex_event_readers_preserve_business_fields_and_real_secret_protection(
    routed, page
):
    rt, calls, _ = routed
    api, request, _, _, _ = page
    api.plugin._persistent_task_runtime = rt
    e = event()
    register(rt, e)
    scope = rt.scope(e)
    try:
        created = await rt.submit(e, {"goal": "public business event"})
        business = {
            "request": "ordinary request",
            "api_key": "example value",
            "headers": ["A"],
            "access_token": "example credential",
            "typed": {"type": "userMessage", "content": "ordinary dataset"},
            "echo": "fake-host-key",
        }
        public = rt.codex._redact(rt.codex.active[created["task_id"]], business)
        rt.codex.store.append_event(
            scope,
            created["task_id"],
            generation=rt.codex.active[created["task_id"]]["generation"],
            method="host_tool/completed",
            payload={"receipt": public},
        )
        request.args = {**vars(scope), "task_id": created["task_id"]}
        web = (await api.get_task_detail())["data"]["codex_events"]
        reader = json.loads(
            await rt.make_read_tool(e).call(None, task_id=created["task_id"])
        )["codex_events"]
        dialogue = json.loads(
            await rt.make_codex_tool(e).call(
                None, action="read", task_id=created["task_id"]
            )
        )
        for result in (web, reader, dialogue):
            encoded = json.dumps(result)
            for expected in (
                "ordinary request",
                "example value",
                "example credential",
                "ordinary dataset",
            ):
                assert expected in encoded
            assert "fake-host-key" not in encoded
        channel = rt.interaction_api.channels.open(scope, "public-secret-test", {})
        rt.codex.store.append_event(
            scope,
            created["task_id"],
            generation=rt.codex.active[created["task_id"]]["generation"],
            method="host_tool/completed",
            payload={
                "receipt": {"request": "ordinary request", "echo": channel["token"]}
            },
        )
        reader = await rt.make_read_tool(e).call(None, task_id=created["task_id"])
        assert channel["token"] not in reader
        assert "ordinary request" in reader
    finally:
        await rt.codex.close()


@pytest.mark.asyncio
async def test_catalog_and_background_reader_show_codex_binding_and_event_entry(routed):
    rt, calls, _ = routed
    e = event()
    register(rt, e)
    scope = rt.scope(e)
    try:
        created = await rt.submit(e, {"goal": "query actual Codex"})
        for index in range(3):
            await calls[0].emit(
                "item/completed",
                {
                    "threadId": created["thread_id"],
                    "turnId": created["turn_id"],
                    "item": {
                        "id": str(index),
                        "type": "commandExecution",
                        "exitCode": 0,
                        "aggregatedOutput": "checked file",
                    },
                },
            )
        catalog = await rt.catalog.read(scope)
        item = next(
            item for item in catalog["items"] if item["item_id"] == created["task_id"]
        )
        assert item["engine"] == "codex"
        assert item["metadata"]["codex"]["thread_id"] == created["thread_id"]
        assert item["codex_events"]["tool"] == "pc_codex_dialogue"
        tool = rt.make_read_tool(e)
        listing = json.loads(await tool.call(None))
        assert listing["tasks"][0]["metadata"]["codex"]["model"] == "gpt-6-sol"
        detail = json.loads(await tool.call(None, task_id=created["task_id"], limit=1))
        assert detail["task"]["engine"] == "codex"
        assert detail["task"]["metadata"]["codex"]["reasoning_effort"] == "medium"
        assert detail["task"]["current_provider"] == "catalog"
        assert detail["codex_events"]["next_after"]
        second = json.loads(
            await tool.call(
                None,
                task_id=created["task_id"],
                limit=1,
                after=detail["codex_events"]["next_after"],
            )
        )
        assert (
            second["codex_events"]["items"][0]["id"]
            > detail["codex_events"]["items"][0]["id"]
        )
        assert "fake-host-key" not in repr(detail)
        assert "pc_codex_dialogue" in TASK_GUIDANCE and "非GPT" in TASK_GUIDANCE
    finally:
        await rt.codex.close()


@pytest.mark.asyncio
async def test_disconnected_codex_query_is_unknown_without_restart(routed):
    rt, calls, _ = routed
    e = event()
    register(rt, e)
    try:
        created = await rt.submit(e, {"goal": "disconnected"})
        await rt.codex.close()
        result = json.loads(
            await rt.make_read_tool(e).call(None, task_id=created["task_id"])
        )
        assert result["task"]["status"] == "unknown_result"
        assert result["task"]["metadata"]["codex"]["connection_state"] == "disconnected"
        assert len(calls) == 1
    finally:
        await rt.codex.close()
