import json

import pytest

from astrbot.core.message.components import At, Plain
from astrbot_plugin_private_companion.tests.test_task_runtime import event, register
from astrbot_plugin_private_companion.tests.test_task_runtime import runtime as runtime


@pytest.mark.asyncio
async def test_unprivileged_chat_can_optionally_mention_any_group_member(runtime):
    e = event("member", mid="ask-for-grant")
    register(runtime, e)
    assert not runtime.actor(e).task_authorized
    tool = runtime.make_group_message_tool(e)
    result = json.loads(await tool.call(None, parts=[{"type": "at", "user_id": "real-owner"},
                                                   {"type": "text", "text": " 这位群友找你授权。"},
                                                   {"type": "at", "user_id": "real-peer"}]))
    assert result["ok"] and result["already_sent"]
    call = runtime.plugin.context.send_message.await_args
    assert call.args[0] == e.unified_msg_origin
    chain = call.args[1].chain
    assert isinstance(chain[0], At) and str(chain[0].qq) == "real-owner"
    assert isinstance(chain[1], Plain) and "找你授权" in chain[1].text
    assert isinstance(chain[2], At) and str(chain[2].qq) == "real-peer"
    assert not runtime.permissions.check(runtime.actor(e), "shell", runtime.workspace)


@pytest.mark.asyncio
async def test_mention_rejects_scope_injection_private_and_broadcast(runtime):
    e = event("member", mid="mention-boundaries")
    register(runtime, e)
    tool = runtime.make_group_message_tool(e)
    for args in ({"parts": [{"type": "at", "user_id": "all"}]},
                 {"parts": [{"type": "at", "user_id": "昵称"}]},
                 {"parts": [{"type": "at", "user_id": "real"}], "group_id": "other"}):
        assert not json.loads(await tool.call(None, **args))["ok"]
    e.message_obj.group_id = ""
    assert not json.loads(await tool.call(None, parts=[{"type": "at", "user_id": "real"}]))["ok"]
    runtime.plugin.context.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_mention_failure_never_claims_sent_and_redacts_only_program_key(runtime):
    e = event("member", mid="mention-failed")
    register(runtime, e)
    tool = runtime.make_group_message_tool(e)
    runtime.plugin.context.send_message.return_value = False
    result = json.loads(await tool.call(None, parts=[{"type": "at", "user_id": "peer"},
                                                   {"type": "text", "text": "业务字段api_key=example，topsecret"}]))
    assert not result["ok"] and not result["already_sent"]
    text = runtime.plugin.context.send_message.await_args.args[1].chain[1].text
    assert "api_key=example" in text and "topsecret" not in text


def test_permission_prompt_owner_is_resolved_from_matching_account_not_nickname(runtime):
    runtime.plugin.data = {"users": {
        "owner-profile": {"relationship_role": "owner", "identity_subject_id": "raw-owner", "identity_adapter_instance_id": "bot", "nickname": "主要用户"},
        "foreign-profile": {"relationship_role": "owner", "identity_subject_id": "foreign-owner", "identity_adapter_instance_id": "other"},
    }}
    runtime.plugin._is_private_companion_owner_user_id = lambda uid: uid.endswith("profile")
    runtime.plugin._private_event_identity_context = lambda event, uid: {"subject": uid, "adapter": event.get_platform_id()}
    runtime.plugin._private_user_matches_event_identity = lambda user, context: user["identity_adapter_instance_id"] == context["adapter"]
    facts = runtime.permission_context(event("member"))
    assert facts["primary_users"] == [{"user_id": "raw-owner", "display_name": "主要用户"}]
    assert facts["task_authorized"] is False
