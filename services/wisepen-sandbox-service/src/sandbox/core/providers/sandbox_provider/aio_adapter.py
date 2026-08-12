from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict

from sandbox.domain.interfaces import (
    SandboxProvider,
    SandboxProviderInfo,
    SandboxProviderType,
)


class AIOAdapterConfig(BaseModel):
    """AIO provider 的专属配置"""

    model_config = ConfigDict(extra="forbid")

    image: str
    request_timeout_seconds: float = 5.0


class AIOAdapter(SandboxProvider):
    """All-in-One Sandbox provider adapter"""

    _HEALTH_FIELDS = frozenset(
        {"success", "message", "data", "home_dir", "version", "detail"}
    )

    def __init__(
        self,
        image: str,
        request_timeout_seconds: float,
    ) -> None:
        self._image = image
        self._request_timeout_seconds = request_timeout_seconds

    @classmethod
    def sandbox_provider_id(cls) -> SandboxProviderType:
        return SandboxProviderType.AIO

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> AIOAdapter:
        adapter_config = AIOAdapterConfig.model_validate(config)
        return cls(
            image=adapter_config.image,
            request_timeout_seconds=adapter_config.request_timeout_seconds,
        )

    def get_sandbox_provider_info(self) -> SandboxProviderInfo:
        return SandboxProviderInfo(image=self._image)

    async def check_ready(self, base_url: str | None) -> bool:
        if base_url is None:
            return False

        try:
            async with httpx.AsyncClient(
                timeout=self._request_timeout_seconds,
                trust_env=False,
            ) as client:
                response = await client.get(f"{base_url.rstrip('/')}/v1/sandbox")
            if response.status_code != 200:
                return False
            payload = response.json()
        except (httpx.HTTPError, httpx.InvalidURL, ValueError):
            return False

        return isinstance(payload, dict) and self._HEALTH_FIELDS.issubset(payload)
