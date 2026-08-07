from dataclasses import dataclass
from typing import Any

from chat.application.tools.core.definition import (
    ToolDefinition,
    ToolExecutionTarget,
    ToolLLMSpec,
    ToolParametersSchema,
    ToolPolicy,
)


@dataclass(frozen=True)
class ClientToolCapability:
    name: str
    description: str
    input_schema: dict[str, Any]


class _ClientTool:
    def __init__(self, capability: ClientToolCapability) -> None:
        self.capability = capability
        self._definition = ToolDefinition(
            llm_spec=ToolLLMSpec(
                name=capability.name,
                description=capability.description,
                parameters_schema=ToolParametersSchema(capability.input_schema),
            ),
            policy=ToolPolicy(
                expose_by_default=True,
                execution_target=ToolExecutionTarget.CLIENT,
            ),
        )

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

def client_tool_from_capability(capability: ClientToolCapability) -> _ClientTool:
    return _ClientTool(capability)
