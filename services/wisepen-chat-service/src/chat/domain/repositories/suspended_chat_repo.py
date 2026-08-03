from abc import ABC, abstractmethod

from chat.domain.entities import SuspendedChat, SuspendedChatReason, SuspendedChatStatus


class SuspendedChatRepository(ABC):
    @abstractmethod
    async def create(self, suspended_chat: SuspendedChat) -> SuspendedChat: ...

    @abstractmethod
    async def get(self, suspended_chat_id: str) -> SuspendedChat | None: ...

    @abstractmethod
    async def find_unfinished_by_session(self, session_id: str, user_id: str) -> list[SuspendedChat]: ...

    @abstractmethod
    async def find_awaiting_by_session(
        self,
        session_id: str,
        user_id: str,
        suspend_reason: SuspendedChatReason,
    ) -> list[SuspendedChat]: ...

    @abstractmethod
    async def acquire(
        self,
        suspended_chat_id: str,
        suspend_reason: SuspendedChatReason,
    ) -> SuspendedChat | None: ...

    @abstractmethod
    async def save(
        self,
        suspended_chat: SuspendedChat,
        expected_status: SuspendedChatStatus | None = None,
    ) -> SuspendedChat | None: ...

    @abstractmethod
    async def delete_unfinished(self, suspended_chat_id: str) -> SuspendedChat | None: ...

    @abstractmethod
    async def delete_resuming(self, suspended_chat_id: str) -> bool: ...
