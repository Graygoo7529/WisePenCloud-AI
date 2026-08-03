from __future__ import annotations

from typing import TYPE_CHECKING, Any

from chat.application.tools.core.definition import Tool
from chat.application.tools.core.llm.renderer import schema_renderer
from chat.application.tools.client_tools import PageClientToolCapability, client_tool_from_capability
from chat.domain.error_codes import ChatErrorCode
from chat.domain.repositories import ToolConfigRepository
from common.core.exceptions import ServiceException

if TYPE_CHECKING:
    from chat.application.tools.core.mcp import McpToolCatalog, SystemMcpToolCatalog


class ToolScope:
    """一次请求内的工具可见性和可信上下文快照"""

    def __init__(
        self,
        *,
        tools: dict[str, Tool],
        context: dict[str, Any] | None,
        configs: dict[str, dict[str, Any]] | None = None,
        page_client_tool_capabilities: list[PageClientToolCapability] | None = None,
    ) -> None:
        self._tools = dict(tools)
        self._context = dict(context or {})
        self._configs = { name: dict(config) for name, config in (configs or {}).items() if name in self._tools}
        self._schemas: list[dict[str, Any]] = [schema_renderer(tool.definition.llm_spec) for tool in self._tools.values()]
        self._page_client_tool_capabilities = [
            capability
            for capability in (page_client_tool_capabilities or [])
            if capability.name in self._tools
        ]

    def schemas(self) -> list[dict[str, Any]]:
        return list(self._schemas)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def config_for(self, name: str) -> dict[str, Any] | None:
        config = self._configs.get(name)
        return dict(config) if config is not None else None

    @property
    def context(self) -> dict[str, Any]:
        return dict(self._context)

    def __len__(self) -> int:
        return len(self._tools)

    def to_resume_data(self) -> dict[str, Any]:
        return {
            "tool_names": list(self._tools),
            "context": dict(self._context),
            "configs": {name: dict(config) for name, config in self._configs.items()},
            "page_client_tool_capabilities": list(self._page_client_tool_capabilities),
        }

class ToolRegistry:
    """全局工具注册表，负责派生请求级工具视图"""

    def __init__(
        self,
        tool_config_repo: ToolConfigRepository,
        mcp_tool_catalog: McpToolCatalog | None = None,
        system_mcp_tool_catalog: SystemMcpToolCatalog | None = None,
    ) -> None:
        self._tool_config_repo = tool_config_repo
        self._mcp_tool_catalog = mcp_tool_catalog
        self._system_mcp_tool_catalog = system_mcp_tool_catalog
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.definition.llm_spec.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def schemas(self) -> list[dict[str, Any]]:
        """返回全局已注册工具的 schema。

        该方法仅用于诊断和测试。运行期 LLM 调用必须使用 ToolScope.schemas()，
        确保已应用当前请求的 expose/allow/deny 过滤。
        """
        return [schema_renderer(tool.definition.llm_spec) for tool in self._tools.values()]

    async def system_tools(self) -> dict[str, Tool]:
        system_tools = dict(self._tools)
        if self._system_mcp_tool_catalog is None:
            return system_tools

        # 收集系统内部 MCP 工具
        system_mcp_tools = await self._system_mcp_tool_catalog.load_system_tools()
        for name, tool in system_mcp_tools.items():
            if name not in system_tools:
                system_tools[name] = tool
        return system_tools

    async def derive(
        self,
        *,
        tool_context: dict[str, Any] | None = None,
        expose_tool_name_set: set[str] | None = None,
        allow_tool_name_set: set[str] | None = None,
        deny_tool_name_set: set[str] | None = None,
        page_client_tool_capabilities: list[PageClientToolCapability] | None = None,
        user_id: str,
    ) -> ToolScope:
        context = dict(tool_context or {})
        expose_tool_name_set = expose_tool_name_set or set()
        deny_tool_name_set = deny_tool_name_set or set()

        tools = await self.system_tools()

        # 收集用户配置的 MCP 工具
        if self._mcp_tool_catalog is not None:
            user_tools = await self._mcp_tool_catalog.load_user_mcp_tools(user_id)
            for name, tool in user_tools.items():
                if name not in tools:
                    tools[name] = tool

        self._merge_client_tools(tools, page_client_tool_capabilities or [])

        filtered_tools: dict[str, Tool] = {}
        # 处理工具配置
        configured_tool_name_set, tool_configs = await self._resolve_tool_config(user_id, tools)
        for name, tool in tools.items():
            policy = tool.definition.policy
            # 如果一个工具没有被配置，那么它就不会被启用
            if (
                configured_tool_name_set is not None
                and tool.definition.config_spec is not None
                and name not in configured_tool_name_set
            ):
                continue

            explicitly_exposed = name in expose_tool_name_set
            skill_exposed = (policy.required_allowed_builtin_skill_ids and
                             set(policy.required_allowed_builtin_skill_ids).issubset(set(context.get("allowed_skill_ids") or [])))

            if not policy.expose_by_default:
                if explicitly_exposed or skill_exposed:
                    filtered_tools[name] = tool
                continue

            if allow_tool_name_set is not None and name not in allow_tool_name_set:
                continue
            if policy.expose_by_default and name in deny_tool_name_set:
                continue

            filtered_tools[name] = tool

        return ToolScope(
            tools=filtered_tools,
            context=context,
            configs=tool_configs,
            page_client_tool_capabilities=page_client_tool_capabilities,
        )

    async def restore_scope(self, resume_data: dict[str, Any], user_id: str) -> ToolScope:
        tool_names = list(resume_data.get("tool_names") or [])
        page_capabilities = list(resume_data.get("page_client_tool_capabilities") or [])
        tools = await self.system_tools()

        if self._mcp_tool_catalog is not None:
            for name, tool in (await self._mcp_tool_catalog.load_user_mcp_tools(user_id)).items():
                tools.setdefault(name, tool)

        self._merge_client_tools(tools, page_capabilities)
        missing_names = [name for name in tool_names if name not in tools]
        if missing_names:
            raise ServiceException(
                ChatErrorCode.TOOL_NOT_FOUND,
                custom_msg=f"恢复对话所需工具不可用: {', '.join(missing_names)}",
            )

        selected_tools = {name: tools[name] for name in tool_names}
        return ToolScope(
            tools=selected_tools,
            context=dict(resume_data.get("context") or {}),
            configs=dict(resume_data.get("configs") or {}),
            page_client_tool_capabilities=page_capabilities,
        )

    def __len__(self) -> int:
        return len(self._tools)

    async def _resolve_tool_config(self, user_id: str, tools: dict[str, Tool]) -> tuple[set[str], dict[str, dict[str, Any]]]:
        # 获取用户所有 Tool 配置
        configs = await self._tool_config_repo.list_tool_configs(user_id)
        configs = { config.tool_name: config for config in configs}

        configured_tool_names: set[str] = set() # 收集已经配置完整的 Tool name
        tool_configs: dict[str, dict[str, Any]] = {} # 收集配置

        for name, tool in tools.items():
            config_spec = tool.definition.config_spec
            if config_spec is None:
                continue  # 无需配置的跳过

            entity = configs.get(name)
            # 检查用户是否有配置、是否启用、必填项是否完整
            if entity is None or not entity.enabled: continue # 不满足的跳过
            missing: list[str] = []
            for key in config_spec.required_keys:
                source = entity.secret_config if key in config_spec.secret_keys else entity.config
                if source.get(key) is None or (isinstance(source.get(key), str) and not source.get(key).strip()):
                    missing.append(key)
            if missing: continue # 不满足的跳过

            # 合并普通配置和 secret 配置
            configured_tool_names.add(name)
            tool_configs[name] = {
                **{
                    key: value
                    for key, value in entity.config.items()
                },
                **{
                    key: value
                    for key, value in entity.secret_config.items()
                },
            }

        return configured_tool_names, tool_configs

    @staticmethod
    def _merge_client_tools(
        tools: dict[str, Tool],
        capabilities: list[PageClientToolCapability],
    ) -> None:
        seen: set[str] = set()
        for capability in capabilities:
            if capability.name in seen or capability.name in tools:
                raise ServiceException(
                    ChatErrorCode.TOOL_CONFIG_INVALID,
                    custom_msg=f"客户端工具名称冲突: {capability.name}",
                )
            seen.add(capability.name)
            tools[capability.name] = client_tool_from_capability(capability)
