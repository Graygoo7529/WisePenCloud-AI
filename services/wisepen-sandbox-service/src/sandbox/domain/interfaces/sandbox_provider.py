from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class SandboxProviderType(StrEnum):
    AIO = "AIO"


class SandboxProviderInfo(BaseModel):
    """Provider 创建沙箱所需的镜像"""

    image: str = Field(..., description="容器镜像名称")


class SandboxProvider(ABC):
    """沙箱类型适配器接口。"""

    @classmethod
    @abstractmethod
    def sandbox_provider_id(cls) -> SandboxProviderType:
        pass

    @classmethod
    @abstractmethod
    def from_config(
        cls,
        config: Mapping[str, Any],
    ) -> SandboxProvider:
        pass

    @abstractmethod
    def get_sandbox_provider_info(self) -> SandboxProviderInfo:
        pass

    @abstractmethod
    async def check_ready(self, base_url: str | None) -> bool:
        pass
