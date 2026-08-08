from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

# 仓储接口仅用 SuspendedChat 做类型标注；运行时导入会在实体初始化期间
# 经 repositories.__init__ 回指尚未定义完成的 SuspendedChat，形成循环导入。
if TYPE_CHECKING:
    from chat.domain.entities.suspended_chat import SuspendedChat


class SuspendedChatRepository(ABC):
    @abstractmethod
    async def create(self, suspended_chat: SuspendedChat) -> SuspendedChat: ...

    @abstractmethod
    async def find_suspended_by_session(self, session_id: str, user_id: str) -> SuspendedChat | None: ...

    @abstractmethod
    async def delete_by_id(self, suspended_chat_id: str) -> None: ...

    @abstractmethod
    async def delete_suspended_by_session(self, session_id: str, user_id: str) -> None: ...
