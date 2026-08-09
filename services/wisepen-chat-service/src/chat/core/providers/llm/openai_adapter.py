import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional

from openai import AsyncOpenAI

from chat.domain.entities import ChatMessage, Role
from chat.domain.entities.provider import ProviderType
from chat.domain.error_codes import ChatErrorCode
from chat.domain.interfaces import LLMProvider
from chat.domain.interfaces.llm import LLMEventType, LLMStreamEvent, LLMUsage
from chat.domain.entities.message import ToolCallMessage
from chat.domain.repositories.model_repo import ModelRequestInfo
from common.core.exceptions import ServiceException
from common.logger import error

from .utils import dump_provider_value, json_object, without_none


class OpenAIAdapter(LLMProvider):
    """
    OpenAI 官方 Responses API 适配器
    """

    @property
    def provider_type(self) -> ProviderType:
        return ProviderType.OPENAI

    def runtime_options_manifest(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "json_schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "temperature": {"type": "number", "minimum": 0, "maximum": 2},
                    "top_p": {"type": "number", "exclusiveMinimum": 0, "maximum": 1},
                    "reasoning": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "effort": {"type": "string", "enum": ["none", "minimal", "low", "medium", "high", "xhigh"]},
                            "summary": {"type": "string", "enum": ["auto", "concise", "detailed"]},
                        },
                    },
                },
            },
            "defaults": {
                "reasoning": {"effort": "medium", "summary": "auto"},
            },
        }

    async def stream_chat_completion(
        self,
        messages: List[ChatMessage],
        model_request: ModelRequestInfo,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> AsyncGenerator[LLMStreamEvent, None]:
        # 构造 SDK Client（指定 api_key 与 base_url）
        client_kwargs = {"api_key": model_request.api_key}
        if model_request.base_url:
            client_kwargs["base_url"] = model_request.base_url
        client = AsyncOpenAI(**client_kwargs)

        # 内部消息投影为 OpenAI Responses API input 格式
        request_input, instructions, previous_response_id = self._openai_messages_formatter(messages)

        # 设置请求参数
        request_kwargs:dict[str, Any] = {
            "model": model_request.model_name, # 模型名
            "input": request_input, # 消息
            "instructions": instructions or None, # system prompt
            "tools": self._openai_tools_formatter(tools), # 工具集
            "previous_response_id": previous_response_id, # Responses API 续写 id
            "stream": True,
            **model_request.runtime_options
        }

        output_items: list[dict[str, Any]] = []
        current_item: dict[str, Any] | None = None
        response_id: str | None = None

        try:
            stream = await client.responses.create(**without_none(request_kwargs))
            # 流式调用
            async for event in stream:
                event_type = getattr(event, "type", "")
                if event_type == "response.created": # OpenAI 创建了一个 response，可用于下一轮 previous_response_id，尤其 tool calling 时
                    response = getattr(event, "response", None)
                    response_id = getattr(response, "id", None)
                elif event_type == "response.output_item.added": # OpenAI 开始输出一个 item
                    current_item = dump_provider_value(getattr(event, "item", None)) or {}
                elif event_type == "response.output_text.delta": # 文本增量
                    delta = getattr(event, "delta", None)
                    if delta:
                        yield LLMStreamEvent(type=LLMEventType.TEXT_DELTA, delta=delta) # 传递 LLMStreamEvent TEXT_DELTA
                elif event_type in {"response.reasoning_summary_text.delta", "response.reasoning_text.delta"}: # 思考增量
                    delta = getattr(event, "delta", None)
                    if delta:
                        yield LLMStreamEvent(type=LLMEventType.REASONING_DELTA, delta=delta) # 传递 LLMStreamEvent REASONING_DELTA
                elif event_type == "response.function_call_arguments.delta" and current_item is not None: # 工具调用参数的增量
                    current_item["arguments"] = (current_item.get("arguments") or "") + (getattr(event, "delta", "") or "")
                elif event_type == "response.output_item.done": # 一个 output item 输出完成
                    item = dump_provider_value(getattr(event, "item", None)) or current_item
                    if item:
                        # output_items 是整轮 OpenAI response 的原生输出集合，用于后续解析工具调用和作为 provider_payload
                        output_items.append(item)
                    current_item = None
                elif event_type == "response.completed": # 整个 response 完成
                    response = getattr(event, "response", None)
                    response_id = getattr(response, "id", None) or response_id # 更新 response id，最终值覆盖或兜底
                    usage = getattr(response, "usage", None) # 计费
                    token_usage = int(getattr(usage, "total_tokens", 0) or 0) if usage else 0
                    if token_usage: # 传递 LLMStreamEvent USAGE
                        yield LLMStreamEvent(type=LLMEventType.USAGE, usage=LLMUsage(output_tokens=token_usage))
                elif event_type in {"response.incomplete", "response.failed"}:
                    # 非 completed 终止不能继续生成空 assistant，应记录上游详情并转换为业务错误
                    response = getattr(event, "response", None)
                    response_id = getattr(response, "id", None) or response_id
                    status = getattr(response, "status", None) or event_type.split(".", 1)[1]
                    provider_error = dump_provider_value(getattr(response, "error", None))
                    incomplete_details = dump_provider_value(
                        getattr(response, "incomplete_details", None)
                    )
                    error(
                        "OpenAI Responses stream terminated without completion.",
                        event_type=event_type,
                        response_id=response_id,
                        status=status,
                        provider_error=provider_error,
                        incomplete_details=incomplete_details,
                    )
                    raise RuntimeError(
                        f"{event_type} status={status} "
                        f"error={provider_error} "
                        f"incomplete_details={incomplete_details}"
                    )
        except Exception as e:
            raise ServiceException(ChatErrorCode.LLM_GENERATION_FAILED, custom_msg=f"OpenAI Responses Error: {e}")

        # 解析工具调用
        calls: list[ToolCallMessage] = []
        for item in output_items:
            # 仅处理 function_call item
            if item.get("type") != "function_call":
                continue
            calls.append(ToolCallMessage(
                call_id=item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex}",
                name=item.get("name") or "",
                arguments=json_object(item.get("arguments") or "{}")
            ))
        if calls: # 传递 LLMStreamEvent TOOL_CALLS
            yield LLMStreamEvent(type=LLMEventType.TOOL_CALLS, tool_calls=calls)

        # 保存 Responses 原生 output items 与 response_id，供下一轮协议回放
        yield LLMStreamEvent(type=LLMEventType.STATE, provider_payload={"output": output_items, "response_id": response_id})

    @staticmethod
    def _openai_messages_formatter(messages: List[ChatMessage]) -> tuple[list[Any], str, str | None]:
        # 提取 system prompt 为 instructions 参数
        instructions = "\n\n".join(msg.content or "" for msg in messages if msg.role == Role.SYSTEM)

        # 查找最近的 OpenAI response_id
        # Responses API 支持在上一次 response 的基础上继续
        # 典型 OpenAI Responses tool calling 流程是: 返回 response.id + function_call -> 执行工具 -> 再调用 Responses API，传 previous_response_id + function_call_output
        last_response_index = next((
            i
            for i in range(len(messages) - 1, -1, -1)
            if (
                messages[i].role == Role.ASSISTANT
                and messages[i].model_info
                and messages[i].model_info.provider_type == ProviderType.OPENAI
                and messages[i].provider_payload
                and messages[i].provider_payload.get("response_id")
            )
        ), -1) # 从后往前找最近一条 provider_type == OPENAI 并且 response_id 存在的消息

        # 如果找到 response_id，优先构造 continuation input
        if last_response_index >= 0:
            outputs = []
            for msg in messages[last_response_index + 1:]:
                # 如果最近一次 OpenAI assistant response 后面有工具结果，就不回放完整历史
                # 只把工具结果作为 input，同时带上 previous_response_id
                if msg.role == Role.TOOL:
                    output = [{"type": "input_text", "text": msg.content or ""}]
                    if msg.imgs:
                        output.extend({
                            "type": "input_image",
                            "image_url": f"data:{img.media_type};base64,{img.base64_data}",
                        } for img in msg.imgs)
                    outputs.append({"type": "function_call_output", "call_id": msg.tool_call_id, "output": output})
            if outputs:
                return outputs, instructions, messages[last_response_index].provider_payload['response_id']

        # 如果没有可续写工具结果，就走完整 input 回放
        items: list[Any] = []
        for msg in messages:
            if msg.role == Role.SYSTEM:
                continue
            # 如果当前消息是 OpenAI Responses 提供的，且存在 provider_payload，则直接取出
            if msg.role == Role.ASSISTANT and msg.model_info and msg.model_info.provider_type == ProviderType.OPENAI and msg.provider_payload:
                items.extend(msg.provider_payload["output"])
                continue
            if msg.role == Role.TOOL:
                # 执行工具后继续 conversation 应发送一个新的 user message，工具结果放置于 function_call_output 且 call_id 对应返回的 call_id / id
                output = [{"type": "input_text", "text": msg.content or ""}]
                if msg.imgs:
                    output.extend({
                            "type": "input_image",
                            "image_url": f"data:{img.media_type};base64,{img.base64_data}",
                    } for img in msg.imgs)
                items.append({"type": "function_call_output", "call_id": msg.tool_call_id, "output": output})
                continue
            # 对于用户消息，或其他非 OpenAI Responses 提供的消息
            role = "assistant" if msg.role == Role.ASSISTANT else "user"
            content: Any = [{"type": "input_text", "text": msg.content or ""}]
            if msg.imgs:
                content.extend({
                    "type": "input_image",
                    "image_url": f"data:{img.media_type};base64,{img.base64_data}",
                } for img in msg.imgs)
            items.append({
                "role": role,
                "content": content,
            })
        return items, instructions, None

    @staticmethod
    def _openai_tools_formatter(tools: Optional[List[Dict[str, Any]]]) -> Optional[List[Dict[str, Any]]]:
        # OpenAI Responses API 的工具 schema 是扁平 function item
        if not tools: return None
        result = []
        for tool in tools:
            function = tool.get("function", {})
            # 转换为 OpenAI Responses 要求的工具格式
            result.append({
                "type": "function",
                "name": function.get("name"),
                "description": function.get("description"),
                "parameters": function.get("parameters") or {"type": "object", "properties": {}},
            })
        return result
