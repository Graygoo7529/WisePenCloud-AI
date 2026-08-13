from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from chat.application.tools.core import (
    ToolDefinition,
    ToolExecutionError,
    ToolLLMSpec,
    ToolParametersSchema,
    ToolPolicy,
    ToolRiskLevel,
)
from chat.application.tools.core.output_cache.cache_store import ToolContentStore
from chat.core.config.app_settings import settings

from chat.application.tools.session_tools.tool_content_tools.window import ToolContentWindow, ToolContentWindowBuilder

_TIMEOUT_SECONDS = 300.0
_PARAMETERS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "content_id": {
            "type": "string",
            "minLength": 1,
            "description": "Required. One cnt_* id from a previous contents entry.",
        },
        "start": {
            "type": "integer",
            "description": (
                "Optional inclusive character offset. Negative values count from the end."
            ),
        },
        "end": {
            "type": "integer",
            "description": (
                "Optional exclusive character offset. Negative values count from the end."
            ),
        },
    },
    "required": ["content_id"],
    "additionalProperties": False,
}


@dataclass(slots=True)
class ToolContentRangeReadResult:
    content_id: str
    window: ToolContentWindow | None = None
    reason: str | None = None


class ToolContentReadRangeTool:
    __slots__ = ("_definition", "_store")

    def __init__(self, *, store: ToolContentStore) -> None:
        self._store = store
        self._definition = ToolDefinition(
            llm_spec=ToolLLMSpec(
                name="tool_content_read_range",
                description=(
                    "Read source text from one cached content_id by character range.\n\n"
                    "WHEN TO TRIGGER:\n"
                    "  - MUST trigger when you already know the exact offset range, the beginning, "
                    "or the end of cached content.\n"
                    "  - SHOULD trigger after structure/search results expose useful offsets.\n"
                    "DO NOT TRIGGER when:\n"
                    "  - You need pages or sections; use tool_content_read_pages or "
                    "tool_content_read_sections.\n"
                    "  - You need discovery; use tool_content_semantic_search or "
                    "tool_content_regex_search.\n\n"
                    "INPUT RULES:\n"
                    "  - Ranges use Python slice semantics: start is inclusive and end is exclusive.\n"
                    "  - Negative offsets count from the end; start=-1000 reads the final 1000 characters.\n"
                    "  - Omitting both offsets reads a token-budgeted window from the beginning.\n"
                    "  - If a requested range is truncated, continue from the returned end_offset."
                ),
                parameters_schema=ToolParametersSchema(_PARAMETERS_SCHEMA),
            ),
            policy=_policy(),
        )

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    async def execute(
        self,
        context: dict[str, Any],
        config: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> ToolContentRangeReadResult:
        del config
        try:
            content_id = str(kwargs["content_id"])
            # 只允许读取当前会话持有的 content，避免 LLM 猜测其他会话的 content_id。
            stored = await self._store.get(
                content_id=content_id,
                session_id=str(context["session_id"]),
            )
            if stored is None:
                # content 不存在时返回普通结果，让 LLM 可以改用 structure/search 重新定位。
                return ToolContentRangeReadResult(
                    content_id=content_id,
                    reason="content_not_found",
                )
            # range 读取只受单窗口预算限制，多段读取由调用方根据 end_offset 继续发起。
            builder = ToolContentWindowBuilder(
                char_budget=settings.TOOL_CONTENT_READ_WINDOW_CHAR_BUDGET
            )
            return ToolContentRangeReadResult(
                content_id=content_id,
                window=builder.build_range_window(
                    stored,
                    start=int(kwargs["start"]) if "start" in kwargs else None,
                    end=int(kwargs["end"]) if "end" in kwargs else None,
                ),
            )
        except Exception as exc:
            raise ToolExecutionError(
                reason="tool_content_read_range_failed",
                detail_reason=str(exc),
                retryable=False,
            ) from exc


def _policy() -> ToolPolicy:
    return ToolPolicy(
        expose_by_default=True,
        persist_output=True,
        risk_level=ToolRiskLevel.LOW,
        required_context_keys=("session_id",),
        timeout_seconds=_TIMEOUT_SECONDS,
    )
