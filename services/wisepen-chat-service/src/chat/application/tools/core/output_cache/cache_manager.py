from __future__ import annotations

from typing import Dict

from chat.application.tools.core.llm.invocation import ToolInvocation
from dataclasses import dataclass, field, asdict
from typing import Any

from chat.application.tools.core.output_cache.cache_store import ToolContentStore, ToolContentReceipt
from chat.core.config.app_settings import settings
from common.logger import warn

_TRUNCATION_MARKER = "\n...\n"


@dataclass(frozen=True, slots=True)
class CacheableText:
    content: str  # 可缓存正文
    is_markdown: bool = False  # # 是否为 MD 格式 (影响缓存)
    metadata: Dict[str, Any] = field(default_factory=dict)

@dataclass(frozen=True, slots=True, kw_only=True)
class ToolStandardOutput:
    uncacheable_result: Dict[str, Any] = field(default_factory=dict)  # 不可缓存结果
    cacheable_results: tuple[CacheableText, ...] = ()  # 可缓存结果


class ToolOutputCache:
    def __init__(self, *, tool_content_store: ToolContentStore) -> None:
        self._per_char_budget = settings.TOOL_CONTENT_PREVIEW_PER_CHAR_BUDGET # 单段限制
        self._total_char_budget = settings.TOOL_CONTENT_PREVIEW_TOTAL_CHAR_BUDGET # 总限制
        if self._per_char_budget < 1: raise ValueError("per_char_budget must be greater than 0")
        if self._total_char_budget < 1: raise ValueError("total_char_budget must be greater than 0")

        self._content_store = tool_content_store

    async def process(self, *, tool_output: Any, invocation: ToolInvocation, session_id: str) -> Dict[str, Any] | Any:
        if not isinstance(tool_output, ToolStandardOutput):
            return tool_output # 如果工具未输出 ToolStandardOutput 类型的输出，不进行缓存处理

        # 复制工具本来可见的结果
        payload = dict(tool_output.uncacheable_result)

        # 挑出非空正文
        cacheable_texts = tuple(
            cacheable_text for cacheable_text in tool_output.cacheable_results
            if cacheable_text.content and not cacheable_text.content.isspace()
        )

        if not cacheable_texts: return payload # 无可缓存文本，直接返回原始 payload

        receipts: dict[int, ToolContentReceipt] = {}

        for index, cacheable_text in enumerate(cacheable_texts):
            try:
                result = await self._content_store.put(
                    session_id=session_id,
                    text=cacheable_text.content, is_markdown=cacheable_text.is_markdown,
                    metadata=dict(cacheable_text.metadata),
                )
            except Exception as exc:
                # 缓存属于附加能力，单段入库失败不应中断整个工具调用。
                warn("tool output cache content store failed.", e=exc,
                     tool_name=invocation.tool_name, tool_call_id=invocation.tool_call_id, cacheable_text_index=index,
                     )
                continue

            if result is not None:
                receipts[index] = result

        preview_budget = self._preview_budget_calc(cacheable_texts)

        payload["contents"] = []
        for index, cacheable_text in enumerate(cacheable_texts):
            preview, truncated = self._build_preview_text(cacheable_text.content, preview_budget[index])

            item: dict[str, Any] = {
                "content_index": index,
                "text": preview,
                "truncated": truncated,
                "total_length": len(cacheable_text.content),
                "metadata": dict(cacheable_text.metadata),
            }
            receipt = receipts.get(index)
            if receipt is not None: item.update(asdict(receipt))

            payload["contents"].append(item)

        return payload

    def _preview_budget_calc(
        self,
        cacheable_texts: tuple[CacheableText, ...],
    ) -> tuple[int, ...]:
        """为每段 preview 分配字符预算，优先保留更短、更容易完整展示的内容"""

        desired = tuple(
            min(len(item.content), self._per_char_budget)
            for item in cacheable_texts
        )
        if sum(desired) <= self._total_char_budget:
            return desired

        # 总预算不够时，先按“每段想要多少预算”从小到大排序，短文本会优先拿到完整预览
        budgets = [0] * len(desired)
        remaining = self._total_char_budget
        ordered = sorted(range(len(desired)), key=desired.__getitem__)
        for position, index in enumerate(ordered):
            # 当前位置之后还剩多少段在等预算
            # 按剩余量做平均，避免前面分配太多导致后面的段直接变成 0
            pending = len(ordered) - position
            fair_share = remaining // pending
            if desired[index] <= fair_share:
                # 当前段的“理想预算”仍然在公平份额以内，先完整满足它
                budgets[index] = desired[index]
                remaining -= desired[index]
                continue
            # 从这一段开始，后面的每一段都只按同一份公平预算分配
            for pending_index in ordered[position:]:
                budgets[pending_index] = fair_share
            # 余数按原始排序顺序往前补，确保总和刚好等于 total_char_budget
            for pending_index in ordered[position : position + remaining % pending]:
                budgets[pending_index] += 1
            break
        return tuple(budgets)

    @staticmethod
    def _build_preview_text(text: str, char_budget: int) -> tuple[str, bool]:
        """按字符预算生成模型可见 preview"""

        if len(text) <= char_budget:
            return text, False
        if char_budget <= 0:
            return "", True
        if char_budget <= len(_TRUNCATION_MARKER):
            return text[:char_budget], True

        available = char_budget - len(_TRUNCATION_MARKER)
        head_budget = available - available // 2
        tail_budget = available // 2
        tail = text[-tail_budget:] if tail_budget else ""
        return text[:head_budget] + _TRUNCATION_MARKER + tail, True

def parse_tool_standard_output(output: Any) -> Any:
    if not isinstance(output, Dict):
        return output
    uncacheable_result = output.get("uncacheable_result")
    cacheable_results = output.get("cacheable_results")
    if not isinstance(uncacheable_result, Dict) or not isinstance(cacheable_results, (list, tuple)):
        return output
    return ToolStandardOutput(
        uncacheable_result=uncacheable_result,
        cacheable_results=tuple(
            CacheableText(
                content=str(item["content"]),
                is_markdown=bool(item.get("is_markdown", False)),
                metadata=item.get("metadata", {}) if isinstance(item.get("metadata", {}), Dict) else {}
            )
            for item in cacheable_results if isinstance(item, Dict) and "content" in item
        ),
    )