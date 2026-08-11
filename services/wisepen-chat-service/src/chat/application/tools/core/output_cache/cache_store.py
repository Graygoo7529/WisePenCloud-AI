from __future__ import annotations

import uuid

from chat.application.utils.chunkers import (
    Chunk,
    ChunkDocument,
    MarkdownChunker,
    PlainTextChunker,
    TextLocator,
    LocatorKind,
    SourceSpan
)
from chat.domain.repositories import ToolContentRepository
from dataclasses import dataclass, field


_DEFAULT_MAX_CHARS = 20_000_000

@dataclass(frozen=True, slots=True)
class ToolContentChunk:
    chunk_index: int
    source_spans: tuple[SourceSpan, ...]
    section_paths: tuple[tuple[str, ...], ...] = ()
    page_labels: tuple[str, ...] = ()
    anchor_labels: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StoredToolContent:
    """持久化内容实体"""
    content_id: str
    session_id: str # 会话 ID
    is_markdown: bool
    text: str
    chunks: tuple[ToolContentChunk, ...] = ()
    locators: tuple[TextLocator, ...] = ()
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolContentReceipt:
    content_id: str
    chunk_count: int
    locator_count: int
    locator_kinds: tuple[LocatorKind, ...]
    total_length: int
    metadata: dict[str, object] = field(default_factory=dict)


class ToolContentStore:

    def __init__(self, *, tool_content_repository: ToolContentRepository, max_chars: int = _DEFAULT_MAX_CHARS) -> None:
        if max_chars < 1: raise ValueError("max_chars must be greater than 0")
        self._tool_content_repository = tool_content_repository
        self._max_chars = max_chars

    async def put(self, *, session_id: str, text: str, is_markdown: bool, metadata: dict[str, object] | None = None) -> ToolContentReceipt | None:
        """校验并持久化一段工具正文"""

        if not text or text.isspace(): return None
        if len(text) > self._max_chars: return None

        content_metadata = dict(metadata or {})
        chunks, locators = self._chunk(text=text, is_markdown=is_markdown, metadata=content_metadata)

        content_id = f"cnt_{uuid.uuid4().hex[:16]}" # 生成稳定的内容 ID
        stored = StoredToolContent(
            content_id=content_id, session_id=session_id,
            is_markdown=is_markdown, text=text,
            chunks=chunks, locators=locators,
            metadata=content_metadata,
        )
        await self._tool_content_repository.put(stored)

        return ToolContentReceipt(
            content_id=stored.content_id,
            chunk_count=len(chunks),
            locator_count=len(locators),
            locator_kinds=tuple(dict.fromkeys(locator.kind for locator in locators)),
            total_length=len(text),
            metadata=content_metadata,
        )

    def _chunk(self, *, text: str, is_markdown: bool, metadata: dict[str, object]) -> tuple[tuple[ToolContentChunk, ...], tuple[TextLocator, ...]]:
        """根据内容类型选择 chunker，并把通用 chunk 结果转成 ToolContent 模型。"""

        chunker = MarkdownChunker() if is_markdown else PlainTextChunker()
        result = chunker.chunk(
            document=ChunkDocument(
                text=text,
                content_type="text/markdown" if is_markdown else "text/plain",
                metadata=metadata,
            )
        )
        return (
            tuple(ToolContentChunk(
                chunk_index=chunk.chunk_index,
                source_spans=chunk.source_spans,
                section_paths=_tuple_metadata(chunk, "section_paths"),
                page_labels=_string_metadata(chunk, "page_labels"),
                anchor_labels=_string_metadata(chunk, "anchor_labels"),
            ) for chunk in result.chunks),
            result.locators,
        )

    async def get(
        self,
        *,
        content_id: str,
        session_id: str,
    ) -> StoredToolContent | None:
        stored = await self._tool_content_repository.get(content_id)
        if stored is None or stored.session_id != session_id: return None
        return stored

def _tuple_metadata(chunk: Chunk, key: str) -> tuple[tuple[str, ...], ...]:
    values = chunk.metadata.get(key)
    if not isinstance(values, (list, tuple)): return ()
    return tuple(
        tuple(str(item) for item in value if str(item))
        for value in values if isinstance(value, (list, tuple))
    )


def _string_metadata(chunk: Chunk, key: str) -> tuple[str, ...]:
    values = chunk.metadata.get(key)
    if not isinstance(values, (list, tuple)): return ()
    return tuple(str(value) for value in values if str(value))
