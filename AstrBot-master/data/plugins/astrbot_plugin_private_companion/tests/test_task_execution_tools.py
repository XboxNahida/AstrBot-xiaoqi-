import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from astrbot_plugin_private_companion.task_execution_tools import create_execution_tools


@pytest.mark.asyncio
async def test_execution_tools_bind_host_actor_and_return_real_evidence():
    actor = object()
    result = SimpleNamespace(exit_code=0, stdout="实际结果", stderr="")
    broker = SimpleNamespace(
        read_text=AsyncMock(return_value=result), write_text=AsyncMock(return_value=result),
        run_powershell=AsyncMock(return_value=result), install_dependencies=AsyncMock(return_value=result),
        list_tools=AsyncMock(return_value=result), read_tool=AsyncMock(return_value=result),
    )
    tools = create_execution_tools(broker, actor)
    assert len(tools) == 6
    await tools["pc_task_list_tools"].call(None)
    broker.list_tools.assert_awaited_once_with(actor)
    await tools["pc_task_read_tool"].call(None, name="辅助脚本.py")
    broker.read_tool.assert_awaited_once_with(actor, "辅助脚本.py")
    text = await tools["pc_task_read_file"].call(None, path="结果.txt")
    broker.read_text.assert_awaited_once_with(actor, "结果.txt")
    assert json.loads(text)["stdout"] == "实际结果"
    await tools["pc_task_write_file"].call(None, path="结果.txt", content="中文")
    broker.write_text.assert_awaited_once_with(actor, "结果.txt", "中文")
    await tools["pc_task_run_command"].call(None, code="Get-Date", timeout=30)
    broker.run_powershell.assert_awaited_once_with(actor, "Get-Date", timeout=30)
    await tools["pc_task_install_dependencies"].call(None, requirements=["packaging==25.0"])
    broker.install_dependencies.assert_awaited_once_with(actor, ["packaging==25.0"])


@pytest.mark.asyncio
async def test_nonzero_exit_is_failure_and_unknown_outcome_is_not_faked():
    broker = SimpleNamespace(run_powershell=AsyncMock(return_value=SimpleNamespace(exit_code=1, stdout="", stderr="failed")))
    tool = create_execution_tools(broker, object())["pc_task_run_command"]
    result = json.loads(await tool.call(None, code="exit 1"))
    assert result["success"] is False and result["exit_code"] == 1
    broker.run_powershell.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        await tool.call(None, code="long running")


@pytest.mark.asyncio
async def test_workspace_tool_binds_identity_and_returns_recoverable_input_error():
    actor = object()
    directory = SimpleNamespace(call=AsyncMock(return_value={"success": True, "workspaces": []}))
    tools = create_execution_tools(SimpleNamespace(), actor, workspace_directory=directory)
    tool = tools["pc_task_workspace"]
    result = json.loads(await tool.call(None, action="list", path=".", workspace_id="peer"))
    assert result["success"]
    directory.call.assert_awaited_once_with(actor, action="list", path=".", workspace_id="peer")
    directory.call.side_effect = FileNotFoundError("not found")
    result = json.loads(await tool.call(None, action="read", path="wrong.txt"))
    assert result["success"] is False and result["recoverable"] is True
    assert result["code"] == "workspace_path_error"
    directory.call.side_effect = PermissionError("revoked")
    result = json.loads(await tool.call(None, action="info"))
    assert result["success"] is False and result.get("recoverable") is not True
    assert result["code"] == "workspace_access_denied"
