from __future__ import annotations

from typing import Any

from chat.application.tools.core import ToolDefinition, ToolExecutionError
from chat.application.tools.core.output_cache.cache_manager import parse_tool_standard_output


class McpRemoteTool:
    def __init__(
        self,
        *,
        mcp_client: Any,
        server: Any,
        remote_name: str,
        definition: ToolDefinition,
        failure_reason: str,
    ) -> None:
        self._mcp_client = mcp_client
        self._server = server
        self._remote_name = remote_name
        self._definition = definition
        self._failure_reason = failure_reason

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    async def execute(
        self,
        context: dict[str, Any],
        config: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        try:
            tool_context = {
                key: context[key]
                for key in self._definition.policy.required_context_keys
                if key in context
            }

            if self._server is None: # 内部 MCP
                output = await self._mcp_client.call_tool(
                    self._server,
                    self._remote_name,
                    kwargs,
                    tool_config=config,
                    tool_context=tool_context,
                    timeout_seconds=self._definition.policy.timeout_seconds,
                )
            else:
                output = await self._mcp_client.call_tool(
                    self._server,
                    self._remote_name,
                    kwargs,
                    timeout_seconds=self._definition.policy.timeout_seconds
                )
            return parse_tool_standard_output(output)
        except Exception as e:
            raise ToolExecutionError(
                reason=self._failure_reason,
                detail_reason=str(e),
                retryable=False,
            ) from e
