from dataclasses import dataclass, field
from typing import List, Optional

from chat.application.events.base import StreamEvent
from chat.application.tools.core import ToolInvocationGroups
from chat.domain.entities import ChatMessage
from chat.domain.entities.suspended_chat import SuspendedChatReason


@dataclass(frozen=True)
class StepStartEvent(StreamEvent):
    """一个 agent step 开始"""

    pass


@dataclass(frozen=True)
class StepResumeRequirement:
    suspend_reason: SuspendedChatReason
    resume_context: ToolInvocationGroups


@dataclass(frozen=True)
class StepFinishEvent(StreamEvent):
    """一个 agent step 结束"""
    is_finished: bool
    intermediate_messages: List[ChatMessage] = field(default_factory=list)
    final_assistant_message: Optional[ChatMessage] = None
    token_usage: int = field(default_factory=int)
    resume_requirement: Optional[StepResumeRequirement] = None
