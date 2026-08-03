import json
from dataclasses import dataclass
from typing import Any

from chat.application.events import (
    StreamEvent,
    ToolOutputAvailableEvent,
    ToolOutputDeniedEvent,
    ToolOutputErrorEvent,
)
from chat.application.tools.core import ToolInvocation
from chat.domain.entities import ChatMessage, Role


_MISSING_CLIENT_RESULT = "[Client Tool Error] Client tool result was not provided before resume."


@dataclass(frozen=True)
class ClientToolResult:
    tool_call_id: str
    output: Any | None = None
    error_text: str | None = None


@dataclass(frozen=True)
class ToolApprovalDecision:
    tool_call_id: str
    approved: bool


@dataclass(frozen=True)
class ClientToolResultMessages:
    events: list[StreamEvent]
    messages: list[ChatMessage]


@dataclass(frozen=True)
class ApprovalDecisionResult:
    approved_invocations: list[ToolInvocation]
    events: list[StreamEvent]
    messages: list[ChatMessage]


def build_client_tool_result_messages(
    pending_client_tool_calls: list[ToolInvocation],
    submissions: list[ClientToolResult],
    session_id: str,
) -> ClientToolResultMessages:
    submission_by_id = {item.tool_call_id: item for item in submissions}
    events: list[StreamEvent] = []
    messages: list[ChatMessage] = []

    for invocation in pending_client_tool_calls:
        submission = submission_by_id.get(invocation.tool_call_id)
        if submission is None:
            content = _MISSING_CLIENT_RESULT
            events.append(ToolOutputErrorEvent(call_id=invocation.tool_call_id, error_text=content))
        elif submission.error_text is not None:
            content = f"[Client Tool Error] {submission.error_text}"
            events.append(ToolOutputErrorEvent(call_id=invocation.tool_call_id, error_text=submission.error_text))
        else:
            content = submission.output if isinstance(submission.output, str) else json.dumps(
                submission.output,
                ensure_ascii=False,
                default=str,
            )
            events.append(ToolOutputAvailableEvent(call_id=invocation.tool_call_id, output=submission.output))

        messages.append(ChatMessage(
            session_id=session_id,
            role=Role.TOOL,
            tool_call_id=invocation.tool_call_id,
            tool_name=invocation.tool_name,
            content=content,
        ))

    return ClientToolResultMessages(events=events, messages=messages)


def apply_tool_approval_decisions(
    pending_invocations: list[ToolInvocation],
    decisions: list[ToolApprovalDecision],
    session_id: str,
) -> ApprovalDecisionResult:
    decision_by_id = {item.tool_call_id: item.approved for item in decisions}
    approved: list[ToolInvocation] = []
    events: list[StreamEvent] = []
    messages: list[ChatMessage] = []

    for invocation in pending_invocations:
        if decision_by_id.get(invocation.tool_call_id, False):
            approved.append(invocation)
            continue

        content = f"[Tool Approval Denied] Tool '{invocation.tool_name}' was not approved by the user."
        events.append(ToolOutputDeniedEvent(call_id=invocation.tool_call_id))
        messages.append(ChatMessage(
            session_id=session_id,
            role=Role.TOOL,
            tool_call_id=invocation.tool_call_id,
            tool_name=invocation.tool_name,
            content=content,
        ))

    return ApprovalDecisionResult(
        approved_invocations=approved,
        events=events,
        messages=messages,
    )
