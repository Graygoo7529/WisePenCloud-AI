__all__ = []
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from wisepen_mcp.capabilities.skill_creator import register_skill_creator_tools
from wisepen_mcp.capabilities.web_search import register_web_search_tools
from wisepen_mcp.capabilities.web_search.search_tools import BaseSearchTool
from wisepen_mcp.service_client import AIAssetClient


def build_mcp_server(
    *,
    ai_asset_client: AIAssetClient,
) -> FastMCP:
    mcp = FastMCP(
        "wisepen-mcp-service",
        stateless_http=True,
        json_response=True,
        streamable_http_path="/",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        ),
    )
    register_skill_creator_tools(mcp, ai_asset_client)
    register_web_search_tools(mcp)
    return mcp


__all__ = ["build_mcp_server"]

