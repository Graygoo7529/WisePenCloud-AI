import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from chat.application.tools.core.definition import ToolExecutionTarget, ToolRiskLevel

if TYPE_CHECKING:
    from chat.application.tools.core.registry import ToolScope

from common.logger import warn


@dataclass
class ToolCallMessageAccumulator:
    """累积流式 tool call 片段"""
    tool_call_id: str = ""
    tool_name: str = ""
    tool_call_argument_str: str = ""


@dataclass(frozen=False)
class ToolInvocation:
    tool_call_id: str
    tool_name: str
    tool_call_arguments: dict[str, Any]
    query_loop_iteration: int | None = None
    is_approved: bool = False
    # metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ClassifiedToolInvocationPlan:
    approval_required_tools: list[ToolInvocation] # 需要请求-确认的工具调用
    server_tools: list[ToolInvocation] # 服务端工具调用
    client_tools: list[ToolInvocation] # 客户端工具调用


def classify_tools(
    invocations: list[ToolInvocation],
    tool_scope: "ToolScope",
) -> ClassifiedToolInvocationPlan:
    approval_required: list[ToolInvocation] = []
    server: list[ToolInvocation] = []
    client: list[ToolInvocation] = []

    for invocation in invocations:
        tool = tool_scope.get(invocation.tool_name)
        if tool is not None and tool.definition.policy.execution_target == ToolExecutionTarget.CLIENT:
            client.append(invocation)
        elif tool is not None and tool.definition.policy.risk_level == ToolRiskLevel.HIGH: # 高危工具执行前需要请求用户确认
            approval_required.append(invocation)
        else:
            server.append(invocation)

    return ClassifiedToolInvocationPlan(
        approval_required_tools=approval_required,
        server_tools=server,
        client_tools=client,
    )


def tool_call_parse(accumulators: dict[int, ToolCallMessageAccumulator], *, query_loop_iteration: int | None = None) -> list[ToolInvocation]:
    invocations: list[ToolInvocation] = []
    for idx in sorted(accumulators.keys()):
        acc = accumulators[idx]
        try:
            tool_call_arguments = json.loads(acc.tool_call_argument_str) if acc.tool_call_argument_str else {}
        except json.JSONDecodeError as e:
            warn("tool call arguments parse failed.", tool_name=acc.tool_name, exc=e)
            tool_call_arguments = {}
        if not isinstance(tool_call_arguments, dict):
            warn(
                "tool call arguments parse failed beacuse arguments is not a JSON object",
                tool_name=acc.tool_name,
                arguments_parsed_type=type(tool_call_arguments).__name__,
            )
            tool_call_arguments = {}
        invocations.append(
            ToolInvocation(
                tool_call_id=acc.tool_call_id, tool_name=acc.tool_name,
                tool_call_arguments=tool_call_arguments, query_loop_iteration=query_loop_iteration
            )
        )
    return invocations

