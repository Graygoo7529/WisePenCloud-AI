from __future__ import annotations

from typing import TYPE_CHECKING, Any

from beanie import PydanticObjectId
from fastapi import BackgroundTasks

from chat.application.events import ErrorEvent, StepFinishEvent, StepResumeRequirement, StreamEvent
from chat.application.session_turn_lock import SessionTurnLock
from chat.application.suspended_chat_service import SuspendedChatService
from chat.application.tool_resume_outcomes import (
    ClientToolResult,
    ToolApprovalDecision,
    apply_tool_approval_decisions,
    build_client_tool_result_messages,
)
from chat.application.tools.core import ToolInvocationGroups, ToolRegistry, ToolScope
from chat.domain.entities import ChatMessage, Role, SuspendedChat, SuspendedChatReason
from chat.domain.error_codes import ChatErrorCode
from common.core.exceptions import ServiceException
from common.logger import error

if TYPE_CHECKING:
    from chat.application.chat_turn_finalizer import ChatTurnFinalizer
    from chat.application.query_loop_runtime import QueryLoopRuntime


class SuspendedTurnRuntime:
    """统一执行客户端结果和高危工具审批的恢复流程。"""

    def __init__(
        self,
        *,
        suspended_chat_service: SuspendedChatService,
        session_turn_lock: SessionTurnLock,
        tool_registry: ToolRegistry,
        query_loop_runtime: QueryLoopRuntime,
        turn_finalizer: ChatTurnFinalizer,
    ) -> None:
        self._service = suspended_chat_service
        self._lock = session_turn_lock
        self._tool_registry = tool_registry
        self._query_loop = query_loop_runtime
        self._finalizer = turn_finalizer

    @staticmethod
    def build_context(
        *, turn_messages: list[ChatMessage], llm_messages: list[ChatMessage],
        tool_scope: ToolScope, model_info: Any, agent_spec: Any, memory_policy: Any,
        token_usage: int, user_query: str, session_summary: str | None,
        windowed_history_messages: Any,
        resume_requirement: StepResumeRequirement,
    ) -> dict[str, Any]:
        SuspendedTurnRuntime._assign_missing_message_ids(turn_messages)
        next_iteration = SuspendedTurnRuntime._next_iteration(resume_requirement, fallback=0)
        return {
            "turn_messages": list(turn_messages), "llm_messages": list(llm_messages),
            "tool_scope_data": tool_scope.to_resume_data(), "model_info": model_info,
            "agent_spec": agent_spec, "memory_policy": memory_policy,
            "token_usage": token_usage, "user_query": user_query,
            "session_summary": session_summary, "next_iteration": next_iteration,
            "windowed_history_messages": windowed_history_messages,
            "resume_requirement": resume_requirement,
        }

    async def resume(
        self, *, user_id: str, session_id: str, tool_results: list[ClientToolResult],
        background_tasks: BackgroundTasks | None,
    ):
        async for event in self._run(
            user_id=user_id, session_id=session_id, submissions=tool_results,
            approval=False, background_tasks=background_tasks,
        ):
            yield event

    async def approval(
        self, *, user_id: str, session_id: str, decisions: list[ToolApprovalDecision],
        background_tasks: BackgroundTasks | None,
    ):
        async for event in self._run(
            user_id=user_id, session_id=session_id, submissions=decisions,
            approval=True, background_tasks=background_tasks,
        ):
            yield event

    async def _run(
        self, *, user_id: str, session_id: str,
        submissions: list[ClientToolResult] | list[ToolApprovalDecision],
        approval: bool, background_tasks: BackgroundTasks | None,
    ):
        async with self._lock.hold(session_id):
            suspended_chat = (
                await self._service.acquire_for_approval(user_id, session_id)
                if approval else await self._service.acquire_for_client_results(user_id, session_id)
            )
            data = self._service.load_context_data(suspended_chat)
            requirement = data.get("resume_requirement")
            if not isinstance(requirement, StepResumeRequirement):
                raise ServiceException(ChatErrorCode.SUSPENDED_CHAT_STATE_INVALID)
            groups = requirement.resume_context
            try:
                tool_scope = await self._tool_registry.restore_scope(data["tool_scope_data"], user_id)
            except ServiceException:
                await self._service.save_waiting_turn(
                    user_id,
                    session_id,
                    requirement,
                    data,
                    suspended_chat=suspended_chat,
                )
                raise
            
            # 检查提交的工具调用ID是否与挂起的工具调用匹配
            pending_toolcalls = groups.approval_required if approval else groups.client
            if not {item.tool_call_id for item in submissions}.issubset({item.tool_call_id for item in pending_toolcalls}):
                await self._service.save_waiting_turn(
                    user_id,
                    session_id,
                    requirement,
                    data,
                    suspended_chat=suspended_chat,
                )
                raise ServiceException(ChatErrorCode.SUSPENDED_CHAT_STATE_INVALID)

            turn_messages = list(data.get("turn_messages") or [])
            llm_messages = list(data.get("llm_messages") or [])
            token_usage = int(data.get("token_usage") or 0)
            if approval:
                decision = apply_tool_approval_decisions(pending_toolcalls, submissions, session_id)
                initial_events = list(decision.events)
                new_messages = list(decision.messages)
                executable = list(groups.server) + list(decision.approved_invocations)
            else:
                client_result = build_client_tool_result_messages(pending_toolcalls, submissions, session_id)
                initial_events = list(client_result.events)
                new_messages = list(client_result.messages)
                executable = []
            turn_messages.extend(new_messages)
            llm_messages.extend(new_messages)
            self._assign_missing_message_ids(turn_messages)
            data.update(turn_messages=turn_messages, llm_messages=llm_messages, resume_requirement=None)
            
            if await self._service.save_resuming_context(suspended_chat, data) is None:
                return

        for event in initial_events:
            yield event

        if executable:
            if not await self._can_continue(session_id, suspended_chat):
                return
            tool_events, tool_messages = await self._query_loop.execute_server_tool_invocations(
                executable, tool_scope, session_id,
            )
            if not await self._can_continue(session_id, suspended_chat):
                return
            for event in tool_events:
                yield event
            turn_messages.extend(tool_messages)
            llm_messages.extend(tool_messages)

        if approval and groups.client:
            self._assign_missing_message_ids(turn_messages)
            next_requirement = StepResumeRequirement(
                suspend_reason=SuspendedChatReason.CLIENT_TOOL_RESULT,
                resume_context=ToolInvocationGroups(approval_required=[], server=[], client=groups.client),
            )
            data.update(
                turn_messages=turn_messages, llm_messages=llm_messages,
                resume_requirement=next_requirement,
            )
            async with self._lock.hold(session_id):
                if await self._service.save_waiting_turn(
                    user_id, session_id, next_requirement, data, suspended_chat=suspended_chat,
                ) is None:
                    return
            return

        if not await self._can_continue(session_id, suspended_chat):
            return
        try:
            async for event in self._query_loop.stream_chat_with_tool_calling(
                messages=llm_messages, tool_scope=tool_scope, session_id=session_id,
                agent_max_iterations=data["agent_spec"].agent_max_iterations,
                model_info=data["model_info"], start_iteration=int(data.get("next_iteration") or 0),
            ):
                if isinstance(event, StepFinishEvent):
                    token_usage += event.token_usage
                    if event.is_finished:
                        turn_messages.append(event.final_assistant_message)
                    else:
                        turn_messages.extend(event.intermediate_messages)
                        if event.resume_requirement is not None:
                            llm_messages.extend(event.intermediate_messages)
                yield event
                if isinstance(event, StepFinishEvent) and event.resume_requirement is not None:
                    self._assign_missing_message_ids(turn_messages)
                    data.update(
                        turn_messages=turn_messages, llm_messages=llm_messages,
                        token_usage=token_usage, resume_requirement=event.resume_requirement,
                        next_iteration=self._next_iteration(
                            event.resume_requirement,
                            fallback=int(data.get("next_iteration") or 0),
                        ),
                    )
                    async with self._lock.hold(session_id):
                        if await self._service.save_waiting_turn(
                            user_id, session_id, event.resume_requirement, data,
                            suspended_chat=suspended_chat,
                        ) is None:
                            return
                    return
        except ServiceException as exc:
            error("resumed chat stream generation failed.", session_id=session_id, exc=exc)
            yield ErrorEvent(error_text=str(exc))
            turn_messages.append(ChatMessage(
                session_id=session_id,
                role=Role.ASSISTANT,
                content="本轮对话执行失败，未能生成完整回复。",
            ))
            data.update(
                turn_messages=turn_messages,
                llm_messages=llm_messages,
                token_usage=token_usage,
            )
            await self._finalize(
                suspended_chat=suspended_chat,
                user_id=user_id,
                session_id=session_id,
                context_data=data,
            )
            return

        data.update(turn_messages=turn_messages, llm_messages=llm_messages, token_usage=token_usage)
        if background_tasks is not None:
            background_tasks.add_task(
                self._finalize, suspended_chat=suspended_chat, user_id=user_id,
                session_id=session_id, context_data=data,
            )
        else:
            await self._finalize(
                suspended_chat=suspended_chat, user_id=user_id,
                session_id=session_id, context_data=data,
            )

    async def _can_continue(self, session_id: str, suspended_chat: SuspendedChat) -> bool:
        async with self._lock.hold(session_id):
            return await self._service.ensure_resuming(str(suspended_chat.id))

    @staticmethod
    def _assign_missing_message_ids(messages: list[ChatMessage]) -> None:
        """为进入暂停缓存、之后会正式持久化的消息预分配 Mongo ObjectId。"""
        for message in messages:
            if message.id is None:
                message.id = PydanticObjectId()

    @staticmethod
    def _next_iteration(requirement: StepResumeRequirement, fallback: int) -> int:
        invocations = (
            list(requirement.resume_context.approval_required)
            + list(requirement.resume_context.server)
            + list(requirement.resume_context.client)
        )
        if not invocations or invocations[0].query_loop_iteration is None:
            return fallback
        return invocations[0].query_loop_iteration + 1

    # 正常完成对话后进行持久化处理
    async def _finalize(
        self, *, suspended_chat: SuspendedChat, user_id: str,
        session_id: str, context_data: dict[str, Any],
    ) -> None:
        deleted = False
        async with self._lock.hold(session_id):
            if not await self._service.ensure_resuming(str(suspended_chat.id)):
                return
            await self._finalizer.persist_message_and_token_bill(
                user_id=user_id, session_id=session_id,
                chat_record_messages=context_data["turn_messages"],
                memory_policy=context_data["memory_policy"],
                model_info=context_data["model_info"],
                token_usage=int(context_data.get("token_usage") or 0),
                group_id=context_data["agent_spec"].billing_group_id,
            )
            deleted = await self._service.delete_resuming_suspended(suspended_chat)
        if deleted:
            await self._run_post_finalize(
                user_id=user_id,
                session_id=session_id,
                context_data=context_data,
            )
        
    # 新start发起时关闭未完成的对话
    async def close_unfinished_before_start(
        self, *, user_id: str, session_id: str,
        background_tasks: BackgroundTasks | None,
    ) -> None:
        unfinished = await self._service._repo.find_unfinished_by_session(session_id, user_id)
        for suspended_chat in unfinished:
            data = self._service.load_context_data(suspended_chat)
            requirement = data.get("resume_requirement")
            if isinstance(requirement, StepResumeRequirement):
                groups = requirement.resume_context
                pending_messages = []
                if suspended_chat.suspend_reason == SuspendedChatReason.CLIENT_TOOL_RESULT:
                    pending_messages.extend((
                        invocation,
                        "[Client Tool Error] Client disconnected before returning the client tool result.",
                    ) for invocation in groups.client)
                else:
                    pending_messages.extend((
                        invocation,
                        "[Tool Approval Interrupted] User did not complete high-risk tool approval before the turn was interrupted.",
                    ) for invocation in groups.approval_required)
                    pending_messages.extend((
                        invocation,
                        "[Tool Execution Error] Tool execution was interrupted before it started.",
                    ) for invocation in groups.server)
                    pending_messages.extend((
                        invocation,
                        "[Client Tool Error] Client tool execution was interrupted before it started.",
                    ) for invocation in groups.client)
                for invocation, message in pending_messages:
                    data["turn_messages"].append(ChatMessage(
                        session_id=session_id, role=Role.TOOL,
                        tool_call_id=invocation.tool_call_id, tool_name=invocation.tool_name,
                        content=message,
                    ))
            data["turn_messages"].append(ChatMessage(
                session_id=session_id, role=Role.ASSISTANT,
                content="本轮对话已中断，未能生成完整回复。",
            ))
            await self._finalizer.persist_message_and_token_bill(
                user_id=user_id, session_id=session_id,
                chat_record_messages=data["turn_messages"], memory_policy=data["memory_policy"],
                model_info=data["model_info"], token_usage=int(data.get("token_usage") or 0),
                group_id=data["agent_spec"].billing_group_id,
            )
            if await self._service.delete_unfinished_suspended(suspended_chat):
                if background_tasks is not None:
                    background_tasks.add_task(
                        self._run_post_finalize,
                        user_id=user_id,
                        session_id=session_id,
                        context_data=data,
                    )
                else:
                    await self._run_post_finalize(
                        user_id=user_id,
                        session_id=session_id,
                        context_data=data,
                    )

    async def _run_post_finalize(
        self,
        *,
        user_id: str,
        session_id: str,
        context_data: dict[str, Any],
    ) -> None:
        memory_policy = context_data["memory_policy"]
        windowed_history_messages = context_data.get("windowed_history_messages")
        if (
            memory_policy.enable_chat_memory
            and memory_policy.enable_chat_memory_summary
            and windowed_history_messages is not None
            and windowed_history_messages.needs_compression
        ):
            await self._finalizer.summarize_and_compress(
                session_id=session_id,
                windowed_history_messages=windowed_history_messages,
                chat_record_messages=context_data["turn_messages"],
                existing_summary=context_data.get("session_summary"),
                memory_policy=memory_policy,
            )
        if context_data["agent_spec"].auto_generate_title:
            await self._finalizer.auto_generate_title(
                session_id=session_id,
                user_id=user_id,
                user_query=context_data.get("user_query") or "",
            )
