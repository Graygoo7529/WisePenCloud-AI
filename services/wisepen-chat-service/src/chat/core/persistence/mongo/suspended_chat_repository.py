from chat.domain.entities import SuspendedChat
from chat.domain.error_codes import ChatErrorCode
from chat.domain.repositories import SuspendedChatRepository
from common.core.exceptions import ServiceException


class MongoSuspendedChatRepository(SuspendedChatRepository):
    @staticmethod
    def _from_raw(raw: dict | None) -> SuspendedChat | None:
        return SuspendedChat.model_validate(raw) if raw is not None else None

    async def create(self, suspended_chat: SuspendedChat) -> SuspendedChat:
        return await suspended_chat.insert()

    async def find_suspended_by_session(self, session_id: str, user_id: str) -> SuspendedChat | None:
        suspended_chat = await SuspendedChat.find(
            SuspendedChat.session_id == session_id,
            SuspendedChat.user_id == user_id,
        ).sort("-updated_at").first_or_none()

        return suspended_chat

    async def delete_suspended_by_session(self, session_id: str, user_id: str) -> None:
        suspended_chat = await SuspendedChat.find(
            SuspendedChat.session_id == session_id,
            SuspendedChat.user_id == user_id,
        ).sort("-updated_at").first_or_none()

        if suspended_chat is None:
            return

        await SuspendedChat.get_pymongo_collection().delete_one({"_id": suspended_chat.id})