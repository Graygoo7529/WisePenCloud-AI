from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from chat.application.tools.core.definition import ToolLLMSpec, ToolDefinition
from chat.application.tools.core.execution.result import ToolExecutionResult
from chat.domain.entities import VisionImage

from pydantic import BaseModel
import orjson

def schema_renderer(llm_spec: ToolLLMSpec) -> dict[str, Any]:
    """将工具定义渲染为模型可消费的 function calling schema。"""
    return {
        "type": "function",
        "function": {
            "name": llm_spec.name,
            "description": llm_spec.description,
            "parameters": llm_spec.parameters_schema.to_dict(),
        },
    }

@dataclass(frozen=True, slots=True)
class RenderToolResult:
    """可写入模型上下文和会话记录的最终工具输出"""
    tool_call_id: str
    tool_name: str
    persisted_output_placeholder: str | None
    tool_output: Any | None
    images: list[VisionImage]

def tool_result_renderer(tool_result: ToolExecutionResult, tool_definition: ToolDefinition | None) -> RenderToolResult:
    if tool_result.tool_execution_error is not None:
        error = tool_result.tool_execution_error
        error_output = {
                "reason": error.reason,
                "detail_reason": error.detail_reason,
                "retryable": error.retryable,
                "metadata": error.metadata,
            }
        output = f"[Tool Error] {error_output}"
    else:
        output = clean_tool_output(tool_result.tool_output)

    if tool_definition is None or tool_definition.policy.persist_output:
        persisted_output_placeholder = None
    else:
        try:
            persisted_output_placeholder = tool_definition.policy.persisted_output_placeholder_factory(
                tool_result.tool_invocation.tool_call_arguments,
                output,
            )
        except Exception:
            persisted_output_placeholder = None
        persisted_output_placeholder = persisted_output_placeholder or "[Tool output persisted.]"

    return RenderToolResult(
        tool_call_id=tool_result.tool_invocation.tool_call_id,
        tool_name=tool_result.tool_invocation.tool_name,
        persisted_output_placeholder=persisted_output_placeholder,
        tool_output=output,
        images=tool_result.images,
    )


def clean_tool_output(
        output: Any,
) -> str:
    """将常见工具返回值渲染成适合进入消息上下文的文本"""
    try:
        encoded_output = orjson.dumps(
            output,
            default=_json_default,
            option=orjson.OPT_NON_STR_KEYS | orjson.OPT_SERIALIZE_NUMPY,
        )
        normalized_output = _remove_empty_json_values(
            orjson.loads(encoded_output)
        )
        return orjson.dumps(normalized_output).decode()
    except TypeError:
        return str(output)


def _remove_empty_json_values(value: Any) -> Any:
    """递归删除 JSON 对象和数组中的空值"""
    if isinstance(value, dict):
        values = {
            key: _remove_empty_json_values(item)
            for key, item in value.items()
        }
        return {
            key: item
            for key, item in values.items()
            if not _is_empty_json_value(item)
        }
    if isinstance(value, list):
        values = [_remove_empty_json_values(item) for item in value]
        return [item for item in values if not _is_empty_json_value(item)]
    return value


def _is_empty_json_value(value: Any) -> bool:
    return value is None or value == "" or value == {} or value == []


def _json_default(value: Any) -> Any:
    """补充 orjson 默认不支持的常见工具返回类型"""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")

    if isinstance(value, Mapping):
        return dict(value)

    if isinstance(value, (set, frozenset)):
        return list(value)

    raise TypeError(
        f"unsupported tool output type: {type(value).__qualname__}"
    )
