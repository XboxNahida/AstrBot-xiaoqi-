"""Run bounded provider/tool steps under TaskService leases, without host agents."""

from __future__ import annotations

import copy
import inspect
import json
from collections.abc import Callable, Mapping
from typing import Any

import jsonschema

from astrbot.core.agent.tool import ToolSet

try:
    from .task_service import TaskFailure, TaskJob, TaskService
except ImportError:
    from task_service import TaskFailure, TaskJob, TaskService


class TaskAgentExecutor:
    """Execute injected tools; this adapter does not provide OS isolation.

    Args:
        service: Owner of persistent attempts, leases and checkpoints.
        resolve_provider: Sync/async callable taking the configured provider ID.
        resolve_tools: Sync/async callable taking (job, requested_names), returning
            a name-to-FunctionTool mapping. Extra tools are never exposed.
        execute_tool: Async callable taking (job, tool, arguments). It must return
            JSON data with status='success', content and a nonempty verified
            receipt. It must not return success for a still-running operation.
        authorize: Sync/async callable taking (job, tool, arguments). Only the
            literal True permits invocation. Identity comes exclusively from job.

    Metadata request fields are prompt, system_prompt, contexts, tool_names,
    primary, fallback, required_tool_names and optional mode='reasoning'. The
    host supplies this request; no model-supplied field changes authorization.
    The host executor must sanitize its receipts and content before returning;
    exception traces are neither persisted nor attached to TaskFailure.
    """

    MAX_STEPS = 30
    MAX_CONTEXT_MESSAGES = 128
    MAX_CONTEXT_BYTES = 131072

    def __init__(
        self,
        service: TaskService,
        *,
        resolve_provider: Callable,
        resolve_tools: Callable,
        execute_tool: Callable,
        authorize: Callable,
    ):
        self.service = service
        self.resolve_provider = resolve_provider
        self.resolve_tools = resolve_tools
        self.execute_tool = execute_tool
        self.authorize = authorize

    async def _resolve(self, callback, *args):
        """Await optional asynchronous host adapters.

        Args:
            callback: Trusted injected adapter.
            *args: Adapter arguments.

        Returns:
            Resolved adapter value.
        """
        value = callback(*args)
        return await value if inspect.isawaitable(value) else value

    def _bounded(self, contexts):
        """Copy complete protocol messages or reject oversized context.

        Args:
            contexts: JSON protocol messages, never truncated mid-tool-call.

        Returns:
            Independent JSON-compatible messages.

        Raises:
            TaskFailure: Context is invalid or exceeds the fixed storage bound.
        """
        try:
            encoded = json.dumps(contexts, ensure_ascii=False, allow_nan=False)
            valid = (
                isinstance(contexts, list)
                and len(contexts) <= self.MAX_CONTEXT_MESSAGES
                and len(encoded.encode("utf-8")) <= self.MAX_CONTEXT_BYTES
                and all(isinstance(item, dict) for item in contexts)
            )
        except (ValueError, TypeError, UnicodeError):
            valid = False
        if not valid:
            raise TaskFailure("context_limit") from None
        return json.loads(encoded)

    async def execute(self, job: TaskJob) -> Mapping[str, Any]:
        """Continue the most recent successful checkpoint with bounded calls.

        Args:
            job: Claimed lease whose first model attempt is already reserved.

        Returns:
            Evidence suitable for TaskService.finish; no state is finalized here.

        Raises:
            TaskFailure: Stable classified error, for TaskScheduler to persist.
        """
        request = job.metadata.get("request", {})
        if not isinstance(request, Mapping) or not request.get("primary"):
            raise TaskFailure("missing_input")
        names = request.get("tool_names", [])
        required = request.get("required_tool_names", [])
        if (
            not isinstance(names, list)
            or not isinstance(required, list)
            or not all(isinstance(name, str) and name for name in names + required)
        ):
            raise TaskFailure("missing_input")
        checkpoint = {}
        checkpoints = [
            item
            for item in job.steps.values()
            if isinstance(item, dict)
            and item.get("kind") == "tool"
            and isinstance(item.get("number"), int)
        ]
        if checkpoints:
            checkpoint = max(checkpoints, key=lambda item: item["number"])
        contexts = self._bounded(
            checkpoint.get("contexts", request.get("contexts", []))
        )
        if not checkpoint and request.get("prompt", job.goal):
            contexts.append(
                {"role": "user", "content": request.get("prompt", job.goal)}
            )
            contexts = self._bounded(contexts)
        system_prompt = request.get("system_prompt", "")
        if job.error_kind in {
            "missing_required_tool",
            "tool_argument_error",
            "protocol_error",
        }:
            system_prompt += (
                f"\nPrevious request failed: {job.error_kind}. "
                "Correct the tool protocol using the allowed schemas and verified "
                "checkpoint. Do not repeat successful tool operations."
            )
        receipts = copy.deepcopy(checkpoint.get("receipts", []))
        pending = copy.deepcopy(checkpoint.get("pending_calls", []))
        completed = checkpoint.get("number", 0)
        succeeded = {item["name"] for item in receipts}
        if pending and job.error_kind in {"tool_argument_error", "protocol_error"}:
            for call in pending:
                contexts.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": json.dumps(
                            {"status": "not_executed", "error_kind": job.error_kind}
                        ),
                    }
                )
            pending = []
            contexts = self._bounded(contexts)
        try:
            resolved = await self._resolve(self.resolve_tools, job, tuple(names))
        except Exception:
            raise TaskFailure("permission_denied") from None
        if not isinstance(resolved, Mapping):
            raise TaskFailure("permission_denied")
        allowed = {
            name: resolved[name]
            for name in names
            if name in resolved
            and getattr(resolved[name], "name", None) == name
            and getattr(resolved[name], "active", True) is True
        }
        if set(required) - succeeded - allowed.keys():
            raise TaskFailure("permission_denied")
        toolset = ToolSet()
        for tool in allowed.values():
            toolset.add_tool(tool)
        reserved = True
        while True:
            if not self.service.heartbeat(job):
                raise TaskFailure("lease_lost")
            if not pending:
                current = self.service.get(job.task_id, job.scope)
                if current is None:
                    raise TaskFailure("lease_lost")
                provider_id = request["primary"]
                next_attempt = current.attempts if reserved else current.attempts + 1
                if next_attempt > 1 and request.get("fallback"):
                    provider_id = request["fallback"]
                if not reserved and not self.service.begin_attempt(job, provider_id):
                    raise TaskFailure("attempt_limit")
                reserved = False
                try:
                    provider = await self._resolve(self.resolve_provider, provider_id)
                    response = await provider.text_chat(
                        prompt="",
                        system_prompt=system_prompt,
                        contexts=copy.deepcopy(contexts),
                        func_tool=toolset,
                        tool_choice="required" if set(required) - succeeded else "auto",
                        request_max_retries=0,
                    )
                except Exception:
                    raise TaskFailure("provider_unavailable", retryable=True) from None
                if not self.service.heartbeat(job):
                    raise TaskFailure("lease_lost")
                if getattr(response, "role", "assistant") == "err":
                    raise TaskFailure("provider_unavailable", retryable=True)
                try:
                    pending = response.to_openai_tool_calls()
                    if not isinstance(pending, list):
                        raise ValueError
                    call_ids = set()
                    for call in pending:
                        if (
                            not isinstance(call["id"], str)
                            or not call["id"]
                            or call["id"] in call_ids
                            or not isinstance(call["function"]["name"], str)
                            or not isinstance(
                                call["function"]["arguments"], (str, dict)
                            )
                        ):
                            raise ValueError
                        call_ids.add(call["id"])
                    text = response.completion_text or ""
                    if not isinstance(text, str):
                        raise ValueError
                except Exception:
                    raise TaskFailure("protocol_error", retryable=True) from None
                if not pending:
                    if set(required) - succeeded:
                        raise TaskFailure("missing_required_tool", retryable=True)
                    mode = request.get("mode", "tools")
                    if not receipts and mode != "reasoning":
                        raise TaskFailure("missing_evidence")
                    if not text.strip():
                        raise TaskFailure("missing_evidence")
                    self._bounded([{"role": "assistant", "content": text}])
                    return {
                        "mode": mode,
                        "text": text,
                        "successful_tool_names": sorted(succeeded),
                        "receipts": receipts,
                    }
                contexts.append(
                    {"role": "assistant", "content": text, "tool_calls": pending}
                )
                contexts = self._bounded(contexts)
            if completed >= self.MAX_STEPS:
                raise TaskFailure("step_limit")
            call = pending[0]
            try:
                name = call["function"]["name"]
                arguments = call["function"]["arguments"]
                args = (
                    json.loads(arguments) if isinstance(arguments, str) else arguments
                )
                if (
                    not isinstance(args, dict)
                    or not isinstance(name, str)
                    or not isinstance(call["id"], str)
                ):
                    raise ValueError
            except (KeyError, ValueError, TypeError):
                raise TaskFailure("tool_argument_error", retryable=True) from None
            if name not in allowed:
                raise TaskFailure("permission_denied")
            tool = allowed[name]
            schema = getattr(tool, "parameters", None)
            if schema:
                try:
                    jsonschema.validate(args, schema)
                except jsonschema.ValidationError:
                    raise TaskFailure("tool_argument_error", retryable=True) from None
                except jsonschema.SchemaError:
                    raise TaskFailure("missing_input") from None
            try:
                authorized = await self._resolve(
                    self.authorize, job, tool, copy.deepcopy(args)
                )
            except Exception:
                authorized = False
            if not self.service.heartbeat(job):
                raise TaskFailure("lease_lost")
            if authorized is not True:
                raise TaskFailure("permission_denied")
            try:
                outcome = await self.execute_tool(job, tool, args)
                if (
                    not isinstance(outcome, Mapping)
                    or outcome.get("status") != "success"
                    or not outcome.get("receipt")
                    or "content" not in outcome
                ):
                    raise ValueError
                outcome = json.loads(json.dumps(dict(outcome), allow_nan=False))
                contexts.append(
                    {
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "content": json.dumps(outcome, ensure_ascii=False),
                    }
                )
                contexts = self._bounded(contexts)
            except TaskFailure:
                raise
            except Exception:
                raise TaskFailure("unknown_result") from None
            if not self.service.heartbeat(job):
                raise TaskFailure("lease_lost")
            completed += 1
            receipts.append({"name": name, "receipt": outcome["receipt"]})
            succeeded.add(name)
            pending = pending[1:]
            current = self.service.get(job.task_id, job.scope)
            if current is None or not self.service.record_step(
                job,
                current.step_key,
                {
                    "kind": "tool",
                    "number": completed,
                    "contexts": contexts,
                    "pending_calls": pending,
                    "receipts": receipts,
                },
                next_step=f"tool:{completed + 1}",
            ):
                raise TaskFailure("lease_lost")
            reserved = False
