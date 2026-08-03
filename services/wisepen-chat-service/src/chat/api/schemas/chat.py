from typing import Optional, List, Dict, Any, Set, Self
from pydantic import BaseModel, Field, model_validator


class ClientToolCapability(BaseModel):
    name: str = Field(..., min_length=1, description="客户端工具名称；不能为空。")
    description: str = Field(..., min_length=1, description="客户端工具说明；不能为空。")
    input_schema: Dict[str, Any] = Field(..., description="客户端工具输入 JSON Schema，根类型必须为 object。")

    @model_validator(mode="after")
    def validate_input_schema(self) -> Self:
        from chat.application.tools.core import ToolParametersSchema

        ToolParametersSchema(self.input_schema)
        return self


class ChatRequest(BaseModel):
    """
    聊天请求传输对象
    """
    session_id: str = Field(..., description="目标会话 ID；必须属于当前登录用户。")
    query: str = Field(..., description="本轮用户输入；不能为空，会作为最新 user message 进入 prompt。")
    model: Optional[str] = Field(
        default=None,
        description="可选模型 ID；未传时使用服务默认模型；若会话 Agent 禁止请求覆盖，则以 Agent 默认模型为准。",
    )
    provider_id: Optional[str] = Field(
        default=None,
        description="可选 Provider ID；用于指定当前模型的一条 active 映射，未传时选择首选映射。",
    )
    runtime_options: Dict[str, Any] = Field(
        default_factory=dict,
        description="模型运行时选项；会与目标 Provider manifest 默认值合并，并按 manifest JSON Schema 校验。",
    )
    frontend_states: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description="前端状态列表；仅 disabled=false 且 value 非空的条目会注入应用上下文。",
    )
    user_defined_attachment_ids: Optional[List[str]] = Field(
        default=None,
        description="本轮用户点选的附件 ID 列表；用于在 prompt 中标记重点附件，附件主体仍来自会话已关联附件。",
    )
    user_defined_allow_tool_names: Optional[Set[str]] = Field(
        default=None,
        description="本轮工具白名单；传入时覆盖 Agent allow_tool_names，且仍受工具是否暴露和模型是否支持工具约束。",
    )
    user_defined_deny_tool_names: Optional[Set[str]] = Field(
        default=None,
        description="本轮工具黑名单；传入时覆盖 Agent deny_tool_names，用于隐藏默认暴露工具。",
    )
    user_defined_on_demand_skill_ids: Optional[Set[str]] = Field(
        default=None,
        description="本轮候选 Skill ID 集合；传入时覆盖 Agent on_demand_skill_ids，并由 Skill matcher 按 query 选择可展示 Skill metadata。",
    )
    user_defined_force_enabled_skill_ids: Optional[Set[str]] = Field(
        default=None,
        description="预留字段；当前入口接收并透传，但现有 coordinator 尚未消费该字段，不应依赖其强制启用 Skill。",
    )
    page_client_tool_capabilities: List[ClientToolCapability] = Field(
        default_factory=list,
        description="当前页面支持执行的客户端工具能力；只在本轮及其恢复链路中有效。",
    )


class ClientToolResultSubmission(BaseModel):
    tool_call_id: str = Field(..., min_length=1, description="客户端工具调用 ID。")
    output: Any | None = Field(default=None, description="客户端工具成功执行结果。")
    error_text: Optional[str] = Field(default=None, min_length=1, description="客户端工具执行失败原因。")

    @model_validator(mode="after")
    def validate_output_or_error(self) -> Self:
        has_output = "output" in self.model_fields_set
        has_error = "error_text" in self.model_fields_set and self.error_text is not None
        if has_output == has_error:
            raise ValueError("output 和 error_text 必须且只能提供一个")
        return self


class ChatResumeRequest(BaseModel):
    session_id: str = Field(..., min_length=1, description="目标会话 ID；必须属于当前登录用户。")
    tool_results: List[ClientToolResultSubmission] = Field(..., min_length=1, description="客户端工具执行结果。")

    @model_validator(mode="after")
    def validate_unique_tool_call_ids(self) -> Self:
        ids = [item.tool_call_id for item in self.tool_results]
        if len(ids) != len(set(ids)):
            raise ValueError("tool_call_id 不允许重复")
        return self


class ToolApprovalDecisionSubmission(BaseModel):
    tool_call_id: str = Field(..., min_length=1, description="待审批工具调用 ID。")
    approved: bool = Field(..., description="是否批准执行该高危工具。")


class ChatApprovalRequest(BaseModel):
    session_id: str = Field(..., min_length=1, description="目标会话 ID；必须属于当前登录用户。")
    decisions: List[ToolApprovalDecisionSubmission] = Field(..., min_length=1, description="高危工具审批结果。")

    @model_validator(mode="after")
    def validate_unique_tool_call_ids(self) -> Self:
        ids = [item.tool_call_id for item in self.decisions]
        if len(ids) != len(set(ids)):
            raise ValueError("tool_call_id 不允许重复")
        return self
