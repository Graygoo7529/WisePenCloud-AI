import asyncio
from contextlib import asynccontextmanager, suppress

import redis.asyncio as redis
from redis.exceptions import LockError

from chat.core.config.app_settings import settings
from chat.domain.error_codes import ChatErrorCode
from common.core.exceptions import ServiceException
from common.logger import warn


class RedisSessionTurnLock:
    _LEASE_SECONDS = 60
    _RENEW_SECONDS = 20

    def __init__(self) -> None:
        self._redis = redis.from_url(settings.REDIS_URL, decode_responses=True)

    @asynccontextmanager
    async def hold(self, session_id: str):
        lock = self._redis.lock(
            f"wisepen:chat:turn_lock:{session_id}",
            timeout=self._LEASE_SECONDS,
            blocking_timeout=self._LEASE_SECONDS,
        )
        try:
            acquired = await lock.acquire()
        except Exception as exc:
            raise ServiceException(ChatErrorCode.CHAT_TURN_LOCK_FAILED) from exc
        if not acquired:
            raise ServiceException(ChatErrorCode.CHAT_TURN_LOCK_FAILED)

        async def renew() -> None:
            while True:
                await asyncio.sleep(self._RENEW_SECONDS)
                try:
                    await lock.extend(self._LEASE_SECONDS, replace_ttl=True)
                except Exception as exc:
                    warn("chat turn lock renewal failed.", session_id=session_id, exc=exc)
                    return

        renewal_task = asyncio.create_task(renew())
        try:
            yield
        finally:
            renewal_task.cancel()
            with suppress(asyncio.CancelledError):
                await renewal_task
            try:
                await lock.release()
            except LockError:
                warn("chat turn lock ownership lost before release.", session_id=session_id)
