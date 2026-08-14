from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Set

from chat.application.agents.models import AgentToolAndSkillPolicy
from chat.application.tools.skill_tools.utils.skill_matcher import SkillMatcher
from chat.domain.entities.skill import SkillMeta

_CURRENT_NOTE_EDITOR_SKILL_ID = "builtin:current-note-editor"

# Skill / Session / 附件工具默认不暴露，只在本轮上下文确实需要时解禁
_SKILL_TOOL_NAMES = frozenset({"load_skill", "load_skill_asset"})
_SESSION_TOOL_NAMES = frozenset({"get_historical_chat_messages"})
_IMAGE_ATTACHMENT_TOOL_NAMES = frozenset({"load_image_attachment"})
_CURRENT_NOTE_EDIT_TOOL_NAMES = frozenset({"read_current_note_for_edit", "apply_current_note_edits"})


@dataclass(frozen=False)
class ChatTurnToolPolicyResult:
    available_skills: list[SkillMeta] = field(default_factory=list)
    tool_context: dict[str, Any] = field(default_factory=dict)
    expose_tool_name_set: set[str] = field(default_factory=set)
    allow_tool_name_set: set[str] | None = None
    deny_tool_name_set: set[str] | None = None


class ChatTurnToolPolicyBuilder:
    def __init__(self, skill_matcher: SkillMatcher) -> None:
        self._skill_matcher = skill_matcher

    async def build(
        self,
        *,
        tool_and_skill_policy: AgentToolAndSkillPolicy,
        user_query: str,
        frontend_states: list[dict[str, Any]] | None,
        has_session_summary: bool,
        has_history_image_record: bool,
        session_id: str,
        user_id: str,
        temporary_attachment_refs: Any,
        user_defined_allow_tool_names: Optional[Set[str]],
        user_defined_deny_tool_names: Optional[Set[str]],
        user_defined_on_demand_skill_ids: Optional[Set[str]],
        user_defined_force_enabled_skill_ids: Optional[Set[str]],
    ) -> ChatTurnToolPolicyResult:
        # 构建 Skill 视图：返回本轮可展示给 LLM 的 Skill metadata，由 LLM 判断是否加载
        available_skills: list[SkillMeta] = []
        if tool_and_skill_policy.enable_use_tool and tool_and_skill_policy.enable_use_skill:
            # 若用户指定了 user_defined_on_demand_skill_ids，则覆盖 agent 预设的 on_demand_skill_ids
            on_demand_skill_ids = (
                user_defined_on_demand_skill_ids
                if user_defined_on_demand_skill_ids is not None
                else tool_and_skill_policy.on_demand_skill_ids
            ) or set()
            available_skills = await self._skill_matcher.match(
                on_demand_skill_ids=on_demand_skill_ids,
                user_query=user_query,
                skill_match_top_k=tool_and_skill_policy.skill_match_top_k,
            )

        current_note_edit_enabled = find_opened_note_resource(frontend_states) is not None

        tool_context: dict[str, Any] = {
            "session_id": session_id,
            "user_id": user_id,
            "temporary_attachment_refs": temporary_attachment_refs,
        }

        # allowed_skill_ids 表示本轮展示给 LLM 的 Skill 白名单
        allowed_skill_ids = {skill.skill_id for skill in available_skills}
        if current_note_edit_enabled:
            allowed_skill_ids.add(_CURRENT_NOTE_EDITOR_SKILL_ID)
        if allowed_skill_ids:
            tool_context["allowed_skill_ids"] = sorted(allowed_skill_ids)

        # expose_tool_name_set 只解禁本轮需要出现的工具
        # 默认不暴露按需工具
        expose_tool_name_set: set[str] = set()
        if available_skills:
            expose_tool_name_set.update(_SKILL_TOOL_NAMES)
        if current_note_edit_enabled:
            expose_tool_name_set.update(_CURRENT_NOTE_EDIT_TOOL_NAMES)
        if has_session_summary:
            # 如有压缩摘要，则暴露会话工具，用于召回被压缩的上下文
            expose_tool_name_set.update(_SESSION_TOOL_NAMES)
        if has_history_image_record:
            # 如历史上下文中有图片，则暴露图片附件读取工具
            expose_tool_name_set.update(_IMAGE_ATTACHMENT_TOOL_NAMES)

        if not tool_and_skill_policy.enable_use_tool:
            # 若不启用 Tool，则 allow_tool_name_set 为空，禁止所有工具
            allow_tool_name_set: set[str] | None = set()
        else:
            # 若用户指定了 user_defined_allow_tool_names，则覆盖 agent 预设的 allow_tool_names
            allow_tool_name_set = user_defined_allow_tool_names or tool_and_skill_policy.allow_tool_names or None

        # 若用户指定了 user_defined_deny_tool_names，则覆盖 agent 预设的 deny_tool_names
        deny_tool_name_set = user_defined_deny_tool_names or tool_and_skill_policy.deny_tool_names or None

        return ChatTurnToolPolicyResult(
            available_skills=available_skills,
            tool_context=tool_context,
            expose_tool_name_set=expose_tool_name_set,
            allow_tool_name_set=allow_tool_name_set,
            deny_tool_name_set=deny_tool_name_set,
        )


def find_opened_note_resource(frontend_states: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    for state in frontend_states or []:
        if state.get("disabled", False) or state.get("key") != "workspace_open_resource":
            continue
        value = state.get("value")
        if not isinstance(value, dict):
            continue
        resource_type = str(value.get("resource_type") or "").lower()
        viewer = str(value.get("viewer") or "").lower()
        if resource_type == "note" or viewer == "note":
            return value
    return None
