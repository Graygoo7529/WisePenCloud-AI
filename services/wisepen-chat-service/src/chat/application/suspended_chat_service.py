import base64
import pickle
from typing import Any

from chat.application.events import StepResumeRequirement
from chat.application.tools.core import ToolInvocationGroups
from chat.domain.entities import (
    ChatMessage,
    SuspendedChat,
    SuspendedChatReason,
    SuspendedChatStatus,
)
from chat.domain.error_codes import ChatErrorCode
from chat.domain.repositories import SuspendedChatRepository
from common.core.exceptions import ServiceException


class SuspendedChatService:
    def __init__(self, suspended_chat_repo: SuspendedChatRepository) -> None:
        self._repo = suspended_chat_repo

    @staticmethod
    def dump_context_data(context_data: dict[str, Any]) -> str:
        return base64.b64encode(pickle.dumps(context_data, protocol=pickle.HIGHEST_PROTOCOL)).decode("ascii")

    @staticmethod
    def load_context_data(suspended_chat: SuspendedChat) -> dict[str, Any]:
        try:
            value = pickle.loads(base64.b64decode(suspended_chat.context.encode("ascii"), validate=True))
        except Exception as exc:
            raise ServiceException(
                ChatErrorCode.SUSPENDED_CHAT_STATE_INVALID,
                custom_msg="SuspendedChat恢复上下文出错",
            ) from exc
        if not isinstance(value, dict):
            raise ServiceException(ChatErrorCode.SUSPENDED_CHAT_STATE_INVALID, custom_msg="SuspendedChat上下文格式错误")
        return value

    async def save_waiting_turn(
        self,
        user_id: str,
        session_id: str,
        requirement: StepResumeRequirement,
        context_data: dict[str, Any],
        suspended_chat: SuspendedChat | None = None,
    ) -> SuspendedChat | None:
        context = self.dump_context_data(context_data)
        if suspended_chat is None:
            return await self._repo.create(SuspendedChat(
                user_id=user_id,
                session_id=session_id,
                status=SuspendedChatStatus.AWAITING,
                suspend_reason=requirement.suspend_reason,
                context=context,
            ))

        suspended_chat.status = SuspendedChatStatus.AWAITING
        suspended_chat.suspend_reason = requirement.suspend_reason
        suspended_chat.context = context
        return await self._repo.save(suspended_chat, expected_status=SuspendedChatStatus.RESUMING)

    async def acquire_for_client_results(self, user_id: str, session_id: str) -> SuspendedChat:
        return await self._acquire(user_id, session_id, SuspendedChatReason.CLIENT_TOOL_RESULT)

    async def acquire_for_approval(self, user_id: str, session_id: str) -> SuspendedChat:
        return await self._acquire(user_id, session_id, SuspendedChatReason.TOOL_APPROVAL)

    async def _acquire(
        self,
        user_id: str,
        session_id: str,
        suspend_reason: SuspendedChatReason,
    ) -> SuspendedChat:
        candidates = await self._repo.find_awaiting_by_session(session_id, user_id, suspend_reason)
        if not candidates:
            raise ServiceException(ChatErrorCode.SUSPENDED_CHAT_NOT_FOUND)
        if len(candidates) != 1:
            raise ServiceException(ChatErrorCode.SUSPENDED_CHAT_STATE_INVALID)
        acquired = await self._repo.acquire(str(candidates[0].id), suspend_reason)
        if acquired is None:
            raise ServiceException(ChatErrorCode.SUSPENDED_CHAT_STATE_INVALID)
        return acquired

    async def save_resuming_context(
        self,
        suspended_chat: SuspendedChat,
        context_data: dict[str, Any],
        suspend_reason: SuspendedChatReason | None = None,
    ) -> SuspendedChat | None:
        suspended_chat.context = self.dump_context_data(context_data)
        if suspend_reason is not None:
            suspended_chat.suspend_reason = suspend_reason
        return await self._repo.save(suspended_chat, expected_status=SuspendedChatStatus.RESUMING)

    async def ensure_resuming(self, suspended_chat_id: str) -> bool:
        current = await self._repo.get(suspended_chat_id)
        return current is not None and current.status == SuspendedChatStatus.RESUMING

    # 一次turn正常关闭后，删除当前处于resuming状态的suspended_chat
    async def delete_resuming_suspended(self, suspended_chat: SuspendedChat) -> bool:
        return await self._repo.delete_resuming(str(suspended_chat.id))

    # 新start发起时，删去旧的unfinished的suspended_chat
    async def delete_unfinished_suspended(self, suspended_chat: SuspendedChat) -> bool:
        return await self._repo.delete_unfinished(str(suspended_chat.id)) is not None

    # 把未完成的SuspendedChat内容转换为前端可消费的数据
    async def get_pending_turn_for_display(
        self,
        user_id: str,
        session_id: str,
    ) -> tuple[list[ChatMessage], dict[str, str]] | None:
        candidates = await self._repo.find_unfinished_by_session(session_id, user_id)
        awaiting = [item for item in candidates if item.status == SuspendedChatStatus.AWAITING]
        if not awaiting:
            return None
        suspended_chat = max(awaiting, key=lambda item: item.updated_at)
        context_data = self.load_context_data(suspended_chat)
        messages = list(context_data.get("turn_messages") or [])
        requirement = context_data.get("resume_requirement")
        if not isinstance(requirement, StepResumeRequirement):
            return messages, {}
        groups = requirement.resume_context
        pending = groups.client if suspended_chat.suspend_reason == SuspendedChatReason.CLIENT_TOOL_RESULT else groups.approval_required
        state = "input-available" if suspended_chat.suspend_reason == SuspendedChatReason.CLIENT_TOOL_RESULT else "approval-requested"
        return messages, {item.tool_call_id: state for item in pending}
