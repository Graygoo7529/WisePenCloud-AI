from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
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
from chat.application.utils.chunkers import LocatorKind, TextLocator

_TIMEOUT_SECONDS = 300.0
_PARAMETERS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "content_id": {
            "type": "string",
            "minLength": 1,
            "description": "Required. One cnt_* id from a previous contents entry.",
        },
    },
    "required": ["content_id"],
    "additionalProperties": False,
}


@dataclass(slots=True)
class ToolContentStructurePage:
    page_label: str
    start_offset: int
    end_offset: int


@dataclass(slots=True)
class ToolContentStructureSection:
    title: str
    section_path: str
    start_offset: int
    end_offset: int
    has_content: bool
    children: list["ToolContentStructureSection"] = field(default_factory=list)


@dataclass(slots=True)
class ToolContentStructureAnchor:
    anchor_label: str
    start_offset: int
    end_offset: int


@dataclass(slots=True)
class ToolContentStructureResult:
    content_id: str
    content_type: str | None = None
    total_length: int | None = None
    pages: list[ToolContentStructurePage] = field(default_factory=list)
    sections: list[ToolContentStructureSection] = field(default_factory=list)
    anchors: list[ToolContentStructureAnchor] = field(default_factory=list)
    metadata: dict[str, object] = field(default_factory=dict)
    reason: str | None = None


class ToolContentGetStructureTool:
    __slots__ = ("_definition", "_store")

    def __init__(self, *, store: ToolContentStore) -> None:
        self._store = store
        self._definition = ToolDefinition(
            llm_spec=ToolLLMSpec(
                name="tool_content_get_structure",
                description=(
                    "Get the structure for one cached content_id without reading body text.\n\n"
                    "WHEN TO TRIGGER:\n"
                    "  - MUST trigger when you need available pages, sections, anchors, or offsets "
                    "before choosing what to read.\n"
                    "  - SHOULD trigger before read tools when you know the document but not the "
                    "exact page label, section path, or offset.\n\n"
                    "OUTPUT RULES:\n"
                    "  - Returns total_length, pages, a section tree, and anchors.\n"
                    "  - Use page_label with tool_content_read_pages.\n"
                    "  - Use section_path with tool_content_read_sections.\n"
                    "  - Use offsets with tool_content_read_range.\n"
                    "  - This tool does not return body text."
                ),
                parameters_schema=ToolParametersSchema(_PARAMETERS_SCHEMA),
            ),
            policy=ToolPolicy(
                expose_by_default=True,
                persist_output=True,
                risk_level=ToolRiskLevel.LOW,
                required_context_keys=("session_id",),
                timeout_seconds=_TIMEOUT_SECONDS,
            ),
        )

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    async def execute(
        self,
        context: dict[str, Any],
        config: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> ToolContentStructureResult:
        del config
        try:
            content_id = str(kwargs["content_id"])
            # structure 只返回导航信息，不返回正文；正文读取交给 read/search 工具控制预算。
            stored = await self._store.get(
                content_id=content_id,
                session_id=str(context["session_id"]),
            )
            if stored is None:
                return ToolContentStructureResult(
                    content_id=content_id,
                    reason="content_not_found",
                )
            return ToolContentStructureResult(
                content_id=content_id,
                # store 只保存 is_markdown，对外仍需要返回稳定的 content_type 字符串。
                content_type="text/markdown" if stored.is_markdown else "text/plain",
                total_length=len(stored.text),
                pages=_build_pages(stored.locators),
                sections=_build_sections(stored.locators),
                anchors=_build_anchors(stored.locators),
                metadata=dict(stored.metadata),
            )
        except Exception as exc:
            raise ToolExecutionError(
                reason="tool_content_get_structure_failed",
                detail_reason=str(exc),
                retryable=False,
            ) from exc


def _build_pages(
    locators: Sequence[TextLocator],
) -> list[ToolContentStructurePage]:
    # page locator 保留物理页边界，供后续按页读取。
    return [
        ToolContentStructurePage(
            page_label=locator.name.removeprefix("page:"),
            start_offset=locator.start_offset,
            end_offset=locator.end_offset,
        )
        for locator in locators
        if locator.kind is LocatorKind.PAGE
    ]


def _build_anchors(
    locators: Sequence[TextLocator],
) -> list[ToolContentStructureAnchor]:
    # anchor locator 通常来自 Markdown 标题或显式锚点，用于快速跳转到局部内容。
    return [
        ToolContentStructureAnchor(
            anchor_label=locator.name.removeprefix("anchor:"),
            start_offset=locator.start_offset,
            end_offset=locator.end_offset,
        )
        for locator in locators
        if locator.kind is LocatorKind.ANCHOR
    ]


def _build_sections(
    locators: Sequence[TextLocator],
) -> list[ToolContentStructureSection]:
    # section locator 是扁平列表，structure 工具负责恢复成树状导航。
    section_locators = [
        locator for locator in locators if locator.kind is LocatorKind.SECTION
    ]
    # section path 需要作为 dict key 使用，因此这里保留 tuple，不按普通数组语义改成 list。
    locator_by_path = {
        tuple(locator.name.removeprefix("section:").split(" > ")): locator
        for locator in section_locators
    }
    children_by_parent: dict[
        tuple[str, ...],
        list[tuple[str, ...]],
    ] = {}
    for path in locator_by_path:
        # path[:-1] 是父节点路径；根节点的父路径为空 tuple。
        children_by_parent.setdefault(path[:-1], []).append(path)
    for children in children_by_parent.values():
        # 同级 section 按原文起始位置排序，保持文档阅读顺序。
        children.sort(key=lambda path: locator_by_path[path].start_offset)

    def build(path: tuple[str, ...]) -> ToolContentStructureSection:
        # 递归构建 section tree，children 只保存直接子节点。
        locator = locator_by_path[path]
        return ToolContentStructureSection(
            title=path[-1],
            section_path=" > ".join(path),
            start_offset=locator.start_offset,
            end_offset=locator.end_offset,
            has_content=locator.end_offset > locator.start_offset,
            children=[build(child) for child in children_by_parent.get(path, [])],
        )

    return [build(path) for path in children_by_parent.get((), [])]
