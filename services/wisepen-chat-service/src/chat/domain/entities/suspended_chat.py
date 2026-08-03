from datetime import datetime, timezone
from enum import StrEnum

from beanie import Document
from pydantic import Field
from pymongo import ASCENDING, IndexModel


class SuspendedChatStatus(StrEnum):
    AWAITING = "awaiting"
    RESUMING = "resuming"


class SuspendedChatReason(StrEnum):
    CLIENT_TOOL_RESULT = "client_tool_result"
    TOOL_APPROVAL = "tool_approval"


class SuspendedChat(Document):
    """未完成 Chat Turn 的临时恢复缓存。"""

    session_id: str
    user_id: str
    status: SuspendedChatStatus = SuspendedChatStatus.AWAITING
    suspend_reason: SuspendedChatReason
    context: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Settings:
        name = "wisepen_suspended_chat"
        indexes = [
            IndexModel([
                ("session_id", ASCENDING),
                ("user_id", ASCENDING),
                ("status", ASCENDING),
                ("suspend_reason", ASCENDING),
            ]),
        ]
