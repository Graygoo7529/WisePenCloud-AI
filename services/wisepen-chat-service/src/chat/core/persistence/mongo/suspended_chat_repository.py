from datetime import datetime, timezone

from beanie import PydanticObjectId
from pymongo import ReturnDocument

from chat.domain.entities import SuspendedChat, SuspendedChatReason, SuspendedChatStatus
from chat.domain.repositories import SuspendedChatRepository


class MongoSuspendedChatRepository(SuspendedChatRepository):
    @staticmethod
    def _from_raw(raw: dict | None) -> SuspendedChat | None:
        return SuspendedChat.model_validate(raw) if raw is not None else None

    async def create(self, suspended_chat: SuspendedChat) -> SuspendedChat:
        return await suspended_chat.insert()

    async def get(self, suspended_chat_id: str) -> SuspendedChat | None:
        try:
            return await SuspendedChat.get(PydanticObjectId(suspended_chat_id))
        except Exception:
            return None

    async def find_unfinished_by_session(self, session_id: str, user_id: str) -> list[SuspendedChat]:
        return await SuspendedChat.find(
            SuspendedChat.session_id == session_id,
            SuspendedChat.user_id == user_id,
            {"status": {"$in": [SuspendedChatStatus.AWAITING.value, SuspendedChatStatus.RESUMING.value]}},
        ).sort("-updated_at").to_list()

    async def find_awaiting_by_session(
        self,
        session_id: str,
        user_id: str,
        suspend_reason: SuspendedChatReason,
    ) -> list[SuspendedChat]:
        return await SuspendedChat.find(
            SuspendedChat.session_id == session_id,
            SuspendedChat.user_id == user_id,
            SuspendedChat.status == SuspendedChatStatus.AWAITING,
            SuspendedChat.suspend_reason == suspend_reason,
        ).sort("-updated_at").to_list()

    async def acquire(
        self,
        suspended_chat_id: str,
        suspend_reason: SuspendedChatReason,
    ) -> SuspendedChat | None:
        raw = await SuspendedChat.get_pymongo_collection().find_one_and_update(
            {
                "_id": PydanticObjectId(suspended_chat_id),
                "status": SuspendedChatStatus.AWAITING.value,
                "suspend_reason": suspend_reason.value,
            },
            {"$set": {"status": SuspendedChatStatus.RESUMING.value, "updated_at": datetime.now(timezone.utc)}},
            return_document=ReturnDocument.AFTER,
        )
        return self._from_raw(raw)

    async def save(
        self,
        suspended_chat: SuspendedChat,
        expected_status: SuspendedChatStatus | None = None,
    ) -> SuspendedChat | None:
        conditions: dict = {"_id": suspended_chat.id}
        if expected_status is not None:
            conditions["status"] = expected_status.value
        updated_at = datetime.now(timezone.utc)
        raw = await SuspendedChat.get_pymongo_collection().find_one_and_update(
            conditions,
            {"$set": {
                "status": suspended_chat.status.value,
                "suspend_reason": suspended_chat.suspend_reason.value,
                "context": suspended_chat.context,
                "updated_at": updated_at,
            }},
            return_document=ReturnDocument.AFTER,
        )
        return self._from_raw(raw)

    async def delete_unfinished(self, suspended_chat_id: str) -> SuspendedChat | None:
        raw = await SuspendedChat.get_pymongo_collection().find_one_and_delete({
            "_id": PydanticObjectId(suspended_chat_id),
            "status": {"$in": [SuspendedChatStatus.AWAITING.value, SuspendedChatStatus.RESUMING.value]},
        })
        return self._from_raw(raw)

    async def delete_resuming(self, suspended_chat_id: str) -> bool:
        raw = await SuspendedChat.get_pymongo_collection().find_one_and_delete({
            "_id": PydanticObjectId(suspended_chat_id),
            "status": SuspendedChatStatus.RESUMING.value,
        })
        return raw is not None
