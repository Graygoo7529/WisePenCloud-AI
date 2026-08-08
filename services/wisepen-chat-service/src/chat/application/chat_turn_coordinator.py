from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, Set, Awaitable, Callable
from beanie import PydanticObjectId
from fastapi import BackgroundTasks

from chat.application.tools import ToolScope
from chat.domain.entities.suspended_chat import SuspendedTurnContext, SuspendedChat
from chat.domain.error_codes import ChatErrorCode
from chat.domain.repositories.model_repo import ModelRequestInfo
from common.logger import error

from chat.core.config.app_settings import settings
from chat.domain.entities import ChatMessage, Role
from chat.application.llm_provider_resolver import LLMProviderResolver
from chat.application.token_counter import TokenCounter
from chat.domain.interfaces.llm import TextCompletionProvider
from chat.domain.interfaces.memory import MemoryProvider
from chat.domain.repositories import SessionRepository, MessageRepository, HotContextRepository, ModelRepository, \
    ProviderRepository, SuspendedChatRepository
from common.core.exceptions import ServiceException
from chat.application.chat_context_assembler import ChatContextAssembler, WindowedMessages
from chat.application.query_loop_runtime import QueryLoopRuntime
from chat.application.agents import (
    AgentResolver,
    DefaultAgentResolver, AgentSpec,
)
from chat.application.events import StepFinishEvent, ErrorEvent
from chat.api.vercel_sse_mapper import to_vercel_sse
from chat.application.chat_turn_finalizer import ChatTurnFinalizer
from chat.application.tools.skill_tools.utils.skill_matcher import SkillMatcher
from chat.application.tools.core import ToolRegistry
from chat.application.tools.client_tools import ClientToolCapability
from chat.application.tools.core.definition import ClientToolResult, ToolApprovalStatus
from common.kafka.producer import KafkaProducerClient


# Skill 工具默认不暴露；仅在本轮存在可展示 Skill 时整体解禁
_SKILL_TOOL_NAMES = frozenset({"load_skill", "load_skill_asset"})
# Session 工具默认不暴露；仅在本轮存在存在不可见的上下文历史时解禁（有summary）
_SESSION_TOOL_NAMES = frozenset({"get_historical_chat_messages"})

@dataclass(frozen=False)
class ChatTurnContext:
    user_id: str = None
    session_id: str = None
    model_info: ModelRequestInfo = None
    agent_spec: AgentSpec = None
    session_summary: Optional[str] = None
    windowed_history_messages: WindowedMessages = None
    tool_scope: ToolScope = None
    messages_for_llm: list[ChatMessage] = field(default_factory=list)
    chat_record_messages: list[ChatMessage] = field(default_factory=list)
    token_usage: int = 0

class ChatTurnCoordinator:
    """
    Chat协调器：负责编排聊天流程中的各个环节，包含上下文管理、LLM ReAct、记忆更新等。
    公共入口 handle_start 方法实现了从接收用户输入到生成响应的完整流程，支持异步流式输出和后置处理任务
    """

    def __init__(
            self,
            llm_provider_resolver: LLMProviderResolver,
            text_llm: TextCompletionProvider,
            token_counter: TokenCounter,
            memory: MemoryProvider,
            model_repo: ModelRepository,
            provider_repo: ProviderRepository,
            session_repo: SessionRepository,
            message_repo: MessageRepository,
            hot_context_repo: HotContextRepository,
            suspended_chat_repo: SuspendedChatRepository,
            tool_registry: ToolRegistry,
            kafka_producer: KafkaProducerClient,
            skill_matcher: SkillMatcher,
            agent_resolver: AgentResolver | None = None,
    ):
        self._memory = memory
        self._model_repo = model_repo
        self._session_repo = session_repo
        self._context_assembler = ChatContextAssembler(
            message_repo=message_repo, session_repo=session_repo, hot_context_repo=hot_context_repo
        )
        self._tool_registry = tool_registry
        self._query_loop_runtime = QueryLoopRuntime(
            llm_provider_resolver=llm_provider_resolver,
            token_counter=token_counter,
        )
        self._turn_finalizer = ChatTurnFinalizer(
            text_llm=text_llm,
            token_counter=token_counter,
            memory=memory,
            message_repo=message_repo, session_repo=session_repo, hot_context_repo=hot_context_repo,
            provider_repo=provider_repo,
            kafka_producer=kafka_producer
        )
        self._skill_matcher = skill_matcher
        self._agent_resolver = agent_resolver or DefaultAgentResolver()

        self._suspended_chat_repo = suspended_chat_repo

    async def handle_suspended_chat_recover(
            self,
            user_id: str,
            session_id: str,
            client_tool_results: list[ClientToolResult],
            tool_approval_status: List[ToolApprovalStatus],
            background_tasks: BackgroundTasks,
            cancel_requested: Callable[[], Awaitable[bool]] | None = None,
    ):
        suspended_chat: SuspendedChat | None = await self._suspended_chat_repo.find_suspended_by_session(session_id, user_id)
        if suspended_chat is None:
            raise ServiceException(ChatErrorCode.SUSPENDED_CHAT_NOT_FOUND)
        suspended_chat_id = str(suspended_chat.id)
        tool_scope = await self._tool_registry.recover_derived(suspended_chat.context.tool_scope_data, user_id)

        chat_turn_context = ChatTurnContext(
            user_id=user_id,
            session_id=session_id,
            model_info=suspended_chat.context.model_info,
            agent_spec=suspended_chat.context.agent_spec,
            session_summary=suspended_chat.context.session_summary,
            windowed_history_messages=suspended_chat.context.windowed_history_messages,
            tool_scope=tool_scope,
            messages_for_llm=suspended_chat.context.messages_for_llm,
            chat_record_messages=suspended_chat.context.chat_record_messages,
            token_usage=suspended_chat.context.token_usage
        )

        async for event in self.query_llm(
            chat_turn_context=chat_turn_context,
            client_tool_results=client_tool_results,
            tool_approval_status=tool_approval_status,
            cancel_requested=cancel_requested,
        ):
            yield event
        self.set_background_task(background_tasks, chat_turn_context)

        await self._suspended_chat_repo.delete_by_id(suspended_chat_id)

    # -------------------------------------------------------------------------
    # 公共入口
    # -------------------------------------------------------------------------
    async def handle_new_chat_start(
            self,
            user_id: str,
            session_id: str,
            user_query: str,
            background_tasks: BackgroundTasks,
            model_id: PydanticObjectId,
            provider_id: Optional[PydanticObjectId] = None,
            runtime_options: dict = None,
            frontend_states: Optional[List[Dict[str, Any]]] = None,
            user_defined_attachment_ids: Optional[List[str]] = None,
            user_defined_allow_tool_names: Optional[Set[str]] = None,
            user_defined_deny_tool_names: Optional[Set[str]] = None,
            user_defined_on_demand_skill_ids: Optional[Set[str]] = None,
            user_defined_force_enabled_skill_ids: Optional[Set[str]] = None,
            client_tool_capabilities: list[ClientToolCapability] | None = None,
            cancel_requested: Callable[[], Awaitable[bool]] | None = None,
    ):
        chat_turn_context = ChatTurnContext()
        chat_turn_context.session_id = session_id
        chat_turn_context.user_id = user_id
        # 获取当前对话的 Agent
        session = await self._session_repo.get_session_for_user(session_id, user_id)
        agent = await self._agent_resolver.resolve(session.agent_id)

        chat_turn_context.agent_spec = agent.spec
        memory_policy = chat_turn_context.agent_spec.memory_policy
        tool_and_skill_policy = chat_turn_context.agent_spec.tool_and_skill_policy
        model_policy = chat_turn_context.agent_spec.model_policy

        # 关闭此前未完成的 SuspendedChat
        await self.close_unfinished_before_start(
            user_id=user_id,
            session_id=session_id
        )

        # 如果禁止覆盖，且指定了模型和供应商
        if not model_policy.allow_request_override:
            if model_policy.default_model_id: model_id = PydanticObjectId(model_policy.default_model_id)
            if model_policy.default_provider_id: provider_id = PydanticObjectId(model_policy.default_provider_id)

        # 解析模型、映射、供应商和 API 凭证
        chat_turn_context.model_info = await self._model_repo.resolve_model_for_chat(
            model_id=model_id,
            user_id=user_id,
            provider_id=provider_id,
            runtime_options=runtime_options or {}
        )

        # Token窗口尺寸
        context_limit = chat_turn_context.model_info.context_window_tokens or settings.CTX_TOKEN_LIMIT
        output_reserve = chat_turn_context.model_info.max_output_tokens or settings.CTX_DEFAULT_OUTPUT_RESERVE_TOKENS
        prompt_budget_tokens = max(
            context_limit - output_reserve,
            settings.CTX_MIN_PROMPT_BUDGET_TOKENS,
        )

        # 加载会话历史 (若启用)
        # 从 Redis 读取最近对话, 如果 Redis 缓存失效，会自动从 MongoDB 回填最近的 N 条历史，确保对话连贯性
        if memory_policy.enable_chat_memory:
            chat_history_record_messages = await self._context_assembler.get_chat_history_record_messages(session_id)
        else:
            chat_history_record_messages = []

        # 加载长期记忆 (若启用)
        # 从 Memory 按相似度阈值召回跨会话事实 (此处实现是Mem0)
        relevant_facts = []
        if memory_policy.enable_long_term_memory:
            relevant_facts = await self._memory.search(
                user_id=user_id,
                query=user_query,
                limit=memory_policy.long_term_memory_limit,
                score_threshold=memory_policy.long_term_memory_score_threshold,
            )

        # 加载会话的历史摘要 (若启用，前提是必须启用会话历史)
        if memory_policy.enable_chat_memory and memory_policy.enable_chat_memory_summary:
            chat_turn_context.session_summary = await self._context_assembler.get_session_summary(session_id)

            # 窗口化消息以用于压缩
            # 从后往前累加 Token，低水位内保留为 messages_keep，更早的未压缩明细进入 messages_compress_candidates
            # candidates 当前轮仍会进入 prompt，本轮结束后会被合并进新摘要，并在下一轮不再作为明细注入
            chat_turn_context.windowed_history_messages = await self._context_assembler.build_windowed_messages(
                chat_history_record_messages,
                prompt_budget_tokens=prompt_budget_tokens,
                high_watermark_ratio=memory_policy.high_watermark_ratio,
                low_watermark_ratio=memory_policy.low_watermark_ratio,
            )

        # 构建工具上下文
        tool_context: dict[str, Any] = {
            "session_id": session_id,
            "user_id": user_id,
        }

        # 构建Skill视图
        # 返回本轮可展示给 LLM 的 Skill metadata，由 LLM 判断是否加载
        available_skills = []
        if tool_and_skill_policy.enable_use_tool and tool_and_skill_policy.enable_use_skill:
            # 若用户指定了 user_defined_on_demand_skill_ids，则覆盖 agent 预设的 on_demand_skill_ids
            on_demand_skill_ids = (user_defined_on_demand_skill_ids if user_defined_on_demand_skill_ids is not None else tool_and_skill_policy.on_demand_skill_ids) or set()
            # 构建 available_skills
            available_skills = await self._skill_matcher.match(
                on_demand_skill_ids=on_demand_skill_ids,
                user_query=user_query,
                skill_match_top_k=tool_and_skill_policy.skill_match_top_k,
            )

        expose_tool_name_set = set()
        if available_skills:
            expose_tool_name_set.update(_SKILL_TOOL_NAMES)
            # allowed_skill_ids 表示本轮展示给 LLM 的 Skill 白名单，工具执行前仍会校验
            tool_context["allowed_skill_ids"] = [s.skill_id for s in available_skills]

        # 如有压缩，则暴露会话相关工具（例如召回被压缩的上下文）
        if chat_turn_context.session_summary is not None:
            expose_tool_name_set.update(_SESSION_TOOL_NAMES)

        # 构建工具视图
        # expose_tool_name_set 仅在有可展示 Skill 时解禁 Skill 工具

        if not tool_and_skill_policy.enable_use_tool:
            # 若不启用Tool，则allow_tool_name_set为空
            allow_tool_name_set:Set[str] = set()
        else:
            # 若用户指定了 user_defined_allow_tool_names，则覆盖 agent 预设的 allow_tool_names
            allow_tool_name_set = user_defined_allow_tool_names or tool_and_skill_policy.allow_tool_names or None

        # 若用户指定了 user_defined_deny_tool_names，则覆盖 agent 预设的 deny_tool_names
        deny_tool_name_set = user_defined_deny_tool_names or tool_and_skill_policy.deny_tool_names or None

        chat_turn_context.tool_scope = await self._tool_registry.derive(
            tool_context=tool_context,
            expose_tool_name_set=expose_tool_name_set,
            allow_tool_name_set=allow_tool_name_set,
            deny_tool_name_set=deny_tool_name_set,
            user_id=user_id,
            client_tool_capabilities=client_tool_capabilities,
        )

        # 对话中的全部附件
        temp_attachments, resource_attachments = await self._session_repo.get_session_attachments(session_id, user_id)

        # 提示词组装
        # 将系统提示词、Mem0 检索到的事实、会话的历史摘要、前端上下文以及窗口内的未压缩明细消息组装成 LLM 所需的格式
        chat_turn_context.messages_for_llm = self._context_assembler.assemble_prompt(
            session_id=session_id,
            user_query=user_query,
            system_prompt=chat_turn_context.agent_spec.system_prompt,  # 系统提示词
            session_summary=chat_turn_context.session_summary,  # 会话的历史摘要
            history_messages=chat_history_record_messages, # 会话历史
            relevant_facts=relevant_facts, # 长期记忆检索的事实
            frontend_states=frontend_states, # 用户前端状态
            available_skills=available_skills or None, # 可用技能
            temp_attachments=temp_attachments, # 对话中的全部临时附件
            resource_attachments=resource_attachments, # 对话中的全部资源附件
            user_defined_attachment_ids=user_defined_attachment_ids, # 用户指定的附件
        )

        # 构造 chat_record_messages
        # chat_record_messages 将用于记录本轮对话的历史，以供后续对话使用
        user_message_metadata = {
            "relevant_facts": relevant_facts,
            "frontend_states": frontend_states or {},
            "available_skills_id": [skill.skill_id for skill in available_skills] or [],
            "temp_attachments": temp_attachments or [],
            "resource_attachments": resource_attachments or [],
            "user_defined_attachment_ids": user_defined_attachment_ids or []
        }

        chat_turn_context.chat_record_messages = [ChatMessage(
            session_id=session_id, role=Role.USER, content=user_query,
            metadata=user_message_metadata,
        )]

        chat_turn_context.token_usage = 0
        async for event in self.query_llm(
                chat_turn_context=chat_turn_context,
                client_tool_results=None,
                tool_approval_status=None,
                cancel_requested=cancel_requested,
        ):
            yield event
        self.set_background_task(background_tasks, chat_turn_context)

    async def query_llm(
            self,
            chat_turn_context: ChatTurnContext,
            client_tool_results: list[ClientToolResult] | None,
            tool_approval_status: List[ToolApprovalStatus] | None,
            cancel_requested: Callable[[], Awaitable[bool]] | None = None,
    ):
        # 流式推理
        try:
            async for event in self._query_loop_runtime.stream_chat_with_tool_calling(
                messages=chat_turn_context.messages_for_llm,
                tool_scope=chat_turn_context.tool_scope,
                session_id=chat_turn_context.session_id,
                agent_max_iterations=chat_turn_context.agent_spec.agent_max_iterations,
                model_info=chat_turn_context.model_info,
                client_tool_results=client_tool_results,
                tool_approval_status=tool_approval_status,
                cancel_requested=cancel_requested,
            ):
                # QueryLoopRuntime 产出的事件如果是 StepFinishEvent 额外处理消息累积
                if isinstance(event, StepFinishEvent):
                    chat_turn_context.token_usage += event.token_usage # 计费
                    if not event.is_finished:
                        # 向 chat_record_messages 追加中间消息（Tool Calls）
                        chat_turn_context.chat_record_messages.extend(event.intermediate_messages)
                        if event.aborted:
                            yield to_vercel_sse(event)
                            yield to_vercel_sse(ErrorEvent(error_text="本轮对话已被用户取消"))
                            return
                        # SSE流需要中断（因调用客户端工具、需要工具调用批准等）
                        if event.suspension is not None:
                            chat_turn_context.messages_for_llm.extend(event.intermediate_messages)
                            suspended_turn_context = SuspendedTurnContext(
                                model_info=chat_turn_context.model_info,
                                agent_spec=chat_turn_context.agent_spec,
                                session_summary=chat_turn_context.session_summary,
                                windowed_history_messages=chat_turn_context.windowed_history_messages,
                                tool_scope_data=chat_turn_context.tool_scope.to_suspension_data(),
                                messages_for_llm=chat_turn_context.messages_for_llm,
                                chat_record_messages=chat_turn_context.chat_record_messages,
                                token_usage=chat_turn_context.token_usage,
                                turn_suspension=event.suspension)
                            await self._suspended_chat_repo.create(SuspendedChat(
                                session_id=chat_turn_context.session_id,
                                user_id=chat_turn_context.user_id,
                                context=suspended_turn_context
                            ))
                    else:
                        # 向 chat_record_messages 追加最终回复消息
                        chat_turn_context.chat_record_messages.append(event.final_assistant_message)
                yield to_vercel_sse(event)

                # 切断SSE流
                if isinstance(event, StepFinishEvent) and event.suspension is not None:
                    return
        except ServiceException as e:
            error("chat stream generation failed.", session_id=chat_turn_context.session_id, exc=e)
            yield to_vercel_sse(ErrorEvent(error_text=str(e)))
            return

    def set_background_task(self, background_tasks, chat_turn_context: ChatTurnContext):
        # 使用 FastAPI 的 BackgroundTasks 在响应返回给用户后，异步执行
        if background_tasks is not None:
            background_tasks.add_task(
                self._turn_finalizer.persist_message_and_token_bill,
                user_id=chat_turn_context.user_id,
                session_id=chat_turn_context.session_id,
                chat_record_messages=chat_turn_context.chat_record_messages,
                memory_policy=chat_turn_context.agent_spec.memory_policy,
                model_info=chat_turn_context.model_info,
                token_usage=chat_turn_context.token_usage,
                billing_group_id=chat_turn_context.agent_spec.billing_group_id,
            )
            # 调用轻量级模型生成并更新会话的全局摘要
            if (chat_turn_context.agent_spec.memory_policy.enable_chat_memory
                    and chat_turn_context.agent_spec.memory_policy.enable_chat_memory_summary
                    and chat_turn_context.windowed_history_messages is not None
                    and chat_turn_context.windowed_history_messages.needs_compression):
                background_tasks.add_task(
                    self._turn_finalizer.summarize_and_compress,
                    session_id=chat_turn_context.session_id,
                    windowed_history_messages=chat_turn_context.windowed_history_messages,
                    chat_record_messages=chat_turn_context.chat_record_messages,
                    existing_summary=chat_turn_context.session_summary,
                    memory_policy=chat_turn_context.agent_spec.memory_policy,
                )
            # 自动生成标题
            if chat_turn_context.agent_spec.auto_generate_title:
                background_tasks.add_task(
                    self._turn_finalizer.auto_generate_title,
                    session_id=chat_turn_context.session_id,
                    user_id=chat_turn_context.user_id,
                    # chat_record_messages的首条消息即为用户查询
                    user_query=str(chat_turn_context.chat_record_messages[0].content)
                )

    async def close_unfinished_before_start(self, user_id: str, session_id: str) -> None:
        unfinished_chat: SuspendedChat | None = await self._suspended_chat_repo.find_suspended_by_session(session_id, user_id)
        if unfinished_chat is None:
            return # 没有未完成的对话，无需关闭
        unfinished_chat_id = str(unfinished_chat.id)

        pending_messages = []
        pending_messages.extend(
            (invocation, "[Tool Approval Interrupted] User did not complete high-risk tool approval before the turn was interrupted.",)
            for invocation in unfinished_chat.context.turn_suspension.classified_tool_invocation_plan.approval_required_tools
        )
        pending_messages.extend(
            (invocation, "[Client Tool Error] Client tool execution was interrupted before it started.",)
            for invocation in unfinished_chat.context.turn_suspension.classified_tool_invocation_plan.client_tools
        )
        pending_messages.extend(
            (invocation, "[Tool Execution Error] Tool execution was interrupted before it started.",)
            for invocation in unfinished_chat.context.turn_suspension.classified_tool_invocation_plan.server_tools
        )

        for invocation, message in pending_messages:
            unfinished_chat.context.chat_record_messages.append(
                ChatMessage(
                    session_id=session_id, role=Role.TOOL,
                    tool_call_id=invocation.tool_call_id, tool_name=invocation.tool_name,
                    content=message,
                )
            )

        unfinished_chat.context.chat_record_messages.append(
            ChatMessage(
                session_id=session_id, role=Role.ASSISTANT,
                content="本轮对话已中断，未能生成完整回复",
            )
        )

        # 完成后处理
        await self._turn_finalizer.persist_message_and_token_bill(
            user_id=unfinished_chat.user_id,
            session_id=unfinished_chat.session_id,
            chat_record_messages=unfinished_chat.context.chat_record_messages,
            memory_policy=unfinished_chat.context.agent_spec.memory_policy,
            model_info=unfinished_chat.context.model_info,
            token_usage=unfinished_chat.context.token_usage,
            billing_group_id=unfinished_chat.context.agent_spec.billing_group_id,
        )
        # 调用轻量级模型生成并更新会话的全局摘要
        if (unfinished_chat.context.agent_spec.memory_policy.enable_chat_memory
                and unfinished_chat.context.agent_spec.memory_policy.enable_chat_memory_summary
                and unfinished_chat.context.windowed_history_messages is not None
                and unfinished_chat.context.windowed_history_messages.needs_compression):
            await self._turn_finalizer.summarize_and_compress(
                session_id=unfinished_chat.session_id,
                windowed_history_messages=unfinished_chat.context.windowed_history_messages,
                chat_record_messages=unfinished_chat.context.chat_record_messages,
                existing_summary=unfinished_chat.context.session_summary,
                memory_policy=unfinished_chat.context.agent_spec.memory_policy,
            )

        await self._suspended_chat_repo.delete_by_id(unfinished_chat_id)
