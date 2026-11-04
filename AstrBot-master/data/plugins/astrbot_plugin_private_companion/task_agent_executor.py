"""Run bounded provider/tool steps under TaskService leases, without host agents."""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import re
from collections.abc import Callable, Mapping
from typing import Any

import jsonschema

from astrbot.core.agent.tool import ToolSet

try:
    from .task_model_policy import request_policy
    from .task_multimodal import materialize_images
    from .task_service import TaskFailure, TaskJob, TaskService
except ImportError:
    from task_model_policy import request_policy
    from task_multimodal import materialize_images
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
            valid = isinstance(contexts, list) and all(
                isinstance(item, dict) for item in contexts
            )
        except (ValueError, TypeError, UnicodeError):
            valid = False
        if not valid:
            raise TaskFailure("context_limit") from None
        messages = json.loads(encoded)
        while (
            len(messages) > self.MAX_CONTEXT_MESSAGES
            or len(json.dumps(messages, ensure_ascii=False).encode("utf-8"))
            > self.MAX_CONTEXT_BYTES
        ):
            rounds = [
                i
                for i, item in enumerate(messages)
                if item.get("role") == "assistant" and item.get("tool_calls")
            ]
            if len(rounds) < 2:
                raise TaskFailure("context_limit")
            # Retain the original user context and whole recent protocol rounds.
            # Full observations remain in durable step records, not a lossy claim.
            del messages[rounds[0] : rounds[1]]
        return messages

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
        try:
            policy = request_policy(request)
        except ValueError:
            raise TaskFailure("missing_input") from None
        names = request.get("tool_names", [])
        required = request.get("required_tool_names", [])
        groups = request.get("required_tool_groups", [])
        if (
            not isinstance(names, list)
            or not isinstance(required, list)
            or not all(isinstance(name, str) and name for name in names + required)
            or not isinstance(groups, list)
            or any(
                not isinstance(group, list)
                or not group
                or any(not isinstance(name, str) or not name for name in group)
                for group in groups
            )
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
            current = request.get("current_input", {})
            content = request.get("prompt", job.goal)
            if isinstance(current, dict) and current.get("content"):
                content = [
                    {"type": "text", "text": "本次后台任务目标：" + job.goal
                     + "\n以下是同一来源消息的原始文字及附件，旧聊天仅用于理解追问，不替代本轮材料。"
                     + "如果图片被提供器移除、显示为占位或无法读取，可调用pc_inspect_images核对；不要凭旧图或旧题猜测本轮画面。"},
                    *copy.deepcopy(current["content"]),
                ]
            contexts.append(
                {"role": "user", "content": content}
            )
            contexts = self._bounded(contexts)
        system_prompt = request.get("system_prompt", "")
        completion_contract = request.get("completion_contract") == "status_v1"
        if completion_contract:
            system_prompt += (
                "\nWhen no more tools are needed, return only a JSON object "
                '{"task_status":"completed|blocked|needs_input","text":"user-facing factual summary"}. '
                "You decide whether the ENTIRE requested goal is fulfilled using actual observations. "
                "Distinguish a textual answer from external operations and never invent tool success. "
                "Tool suggestions are not mandatory workflows; choose and repair your own plan. "
                "A tool error is feedback, not the end of the task. Correct parameters or choose another approach. "
                "An unknown result means an operation MAY already have happened: inspect actual state before repeating it. "
                "Unexecuted calls from a failed batch have no effects and may be replanned. "
                "Earlier full tool outputs remain in durable task steps if context is compacted. "
                "Listing existing settings does not complete a request to change them. "
                "If tools or capabilities are missing, report blocked with the unfinished work "
                "and actual reason; if user input is required report needs_input. "
                "Never return completed for partial work or an explanation of how to do it. "
                "For custom interactions, write and schedule your script through pc_interaction_channel "
                "and verify its receipt before reporting completed. Do not ask the user to "
                "provide internal script tools that are already available to you."
            )
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
            resolved = await self._resolve(
                self.resolve_tools,
                job,
                None if request.get("dynamic_tool_catalog") is True else tuple(names),
            )
        except Exception:
            raise TaskFailure("permission_denied") from None
        if not isinstance(resolved, Mapping):
            raise TaskFailure("permission_denied")
        allowed = {
            name: resolved[name]
            for name in (
                resolved if request.get("dynamic_tool_catalog") is True else names
            )
            if name in resolved
            and getattr(resolved[name], "name", None) == name
            and getattr(resolved[name], "active", True) is True
        }
        toolset = ToolSet()
        for name in sorted(allowed):
            toolset.add_tool(allowed[name])
        reserved = True
        while True:
            if not self.service.heartbeat(job):
                raise TaskFailure("lease_lost")
            if not pending:
                if request.get("dynamic_tool_catalog") is True:
                    try:
                        current_tools = await self._resolve(
                            self.resolve_tools, job, None
                        )
                    except Exception:
                        raise TaskFailure("permission_denied") from None
                    if not isinstance(current_tools, Mapping):
                        raise TaskFailure("permission_denied")
                    allowed = {
                        name: tool
                        for name, tool in current_tools.items()
                        if getattr(tool, "name", None) == name
                        and getattr(tool, "active", True) is True
                    }
                    toolset = ToolSet()
                    for name in sorted(allowed):
                        toolset.add_tool(allowed[name])
                current = self.service.get(job.task_id, job.scope)
                if current is None:
                    raise TaskFailure("lease_lost")
                provider_id = policy.provider_id
                if not reserved and not self.service.begin_attempt(job, provider_id):
                    raise TaskFailure("attempt_limit")
                reserved = False
                try:
                    provider = await self._resolve(self.resolve_provider, provider_id)
                    modalities = getattr(provider, "provider_config", {}).get("modalities")
                    outgoing = materialize_images(contexts, request, supports_image=not modalities or "image" in modalities)
                    async with asyncio.timeout(policy.timeout_seconds):
                        response = await provider.text_chat(
                            prompt=None,
                            system_prompt=system_prompt,
                            contexts=outgoing,
                            func_tool=toolset,
                            tool_choice="auto",
                            request_max_retries=0,
                            timeout=policy.timeout_seconds,
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
                    if completion_contract:
                        try:
                            report = json.loads(
                                re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
                            )
                            if (
                                not isinstance(report, dict)
                                or report.get("task_status")
                                not in {"completed", "blocked", "needs_input"}
                                or not isinstance(report.get("text"), str)
                                or not report["text"].strip()
                            ):
                                raise ValueError
                        except (ValueError, TypeError):
                            raise TaskFailure(
                                "protocol_error", retryable=True
                            ) from None
                        text = report["text"]
                        self._bounded([{"role": "assistant", "content": text}])
                        if report["task_status"] != "completed":
                            current = self.service.get(job.task_id, job.scope)
                            if current is None or not self.service.record_step(
                                job,
                                current.step_key,
                                {
                                    "kind": "completion_report",
                                    "text": text,
                                    "task_status": report["task_status"],
                                    "receipts": receipts,
                                },
                                next_step=f"incomplete:{completed + 1}",
                            ):
                                raise TaskFailure("lease_lost")
                            raise TaskFailure(
                                "missing_input"
                                if report["task_status"] == "needs_input"
                                else "task_incomplete"
                            )
                    mode = request.get("mode", "tools")
                    if not receipts and mode != "reasoning" and not completion_contract:
                        raise TaskFailure("missing_evidence")
                    if not text.strip():
                        raise TaskFailure("missing_evidence")
                    self._bounded([{"role": "assistant", "content": text}])
                    return {
                        "mode": mode,
                        "text": text,
                        "successful_tool_names": sorted(succeeded),
                        "receipts": receipts,
                        "verification": "tool_receipts"
                        if receipts
                        else "model_report_only",
                    }
                contexts.append(
                    {"role": "assistant", "content": text, "tool_calls": pending}
                )
                contexts = self._bounded(contexts)
            call = pending[0]
            name = call["function"]["name"]
            if name not in allowed:
                raise TaskFailure("permission_denied")
            tool = allowed[name]
            invalid_arguments = False
            try:
                arguments = call["function"]["arguments"]
                args = (
                    json.loads(arguments) if isinstance(arguments, str) else arguments
                )
                if not isinstance(args, dict):
                    raise ValueError
            except (KeyError, ValueError, TypeError):
                invalid_arguments = True
            schema = getattr(tool, "parameters", None)
            if schema and not invalid_arguments:
                try:
                    jsonschema.validate(args, schema)
                except jsonschema.ValidationError:
                    invalid_arguments = True
                except jsonschema.SchemaError:
                    raise TaskFailure("missing_input") from None
            outcome = None
            if invalid_arguments:
                outcome = {
                    "status": "error",
                    "content": "tool_argument_error: not executed. Correct JSON arguments using the exposed tool schema.",
                    "receipt": {"tool": name, "not_executed": True},
                }
            else:
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
                if outcome is None:
                    try:
                        outcome = await self.execute_tool(job, tool, args)
                    except TaskFailure as exc:
                        if exc.error_kind != "unknown_result":
                            raise
                        outcome = {
                            "status": "unknown",
                            "content": "unknown_result: the operation may have happened. Query actual state before deciding whether to retry.",
                            "receipt": {"tool": name, "observed_output": False},
                        }
                    except Exception:
                        outcome = {
                            "status": "unknown",
                            "content": "unknown_result: the adapter did not confirm the operation. Verify actual state before retrying.",
                            "receipt": {"tool": name, "observed_output": False},
                        }
                if (
                    not isinstance(outcome, Mapping)
                    or outcome.get("status") not in {"success", "error", "unknown"}
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
            if outcome["status"] == "success":
                receipts.append({"name": name, "receipt": outcome["receipt"]})
                succeeded.add(name)
            pending = pending[1:]
            if outcome["status"] != "success":
                for skipped in pending:
                    contexts.append(
                        {
                            "role": "tool",
                            "tool_call_id": skipped["id"],
                            "content": json.dumps(
                                {
                                    "status": "not_executed",
                                    "code": "replan_after_tool_error",
                                }
                            ),
                        }
                    )
                pending = []
                contexts = self._bounded(contexts)
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
                    "tool": name,
                    "outcome": outcome,
                },
                next_step=f"tool:{completed + 1}",
            ):
                raise TaskFailure("lease_lost")
            reserved = False
