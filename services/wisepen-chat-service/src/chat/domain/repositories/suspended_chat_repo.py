from abc import ABC, abstractmethod

from chat.domain.entities import SuspendedChat


class SuspendedChatRepository(ABC):
    @abstractmethod
    async def create(self, suspended_chat: SuspendedChat) -> SuspendedChat: ...

    @abstractmethod
    async def find_suspended_by_session(self, session_id: str, user_id: str) -> SuspendedChat | None: ...

    @abstractmethod
    async def delete_by_id(self, suspended_chat_id: str) -> None: ...

    @abstractmethod
    async def delete_suspended_by_session(self, session_id: str, user_id: str) -> None: ...
