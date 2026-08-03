from contextlib import AbstractAsyncContextManager
from typing import Protocol


class SessionTurnLock(Protocol):
    def hold(self, session_id: str) -> AbstractAsyncContextManager[None]: ...
