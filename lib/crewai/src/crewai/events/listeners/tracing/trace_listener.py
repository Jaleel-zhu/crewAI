"""Trace collection listener for orchestrating trace collection."""

import os
from typing import Any, ClassVar
import uuid

from typing_extensions import Self

from crewai.cli.authentication.token import AuthError, get_auth_token
from crewai.cli.version import get_crewai_version
from crewai.events.base_event_listener import BaseEventListener
from crewai.events.event_bus import CrewAIEventsBus
from crewai.events.utils.console_formatter import ConsoleFormatter
from crewai.events.listeners.tracing.first_time_trace_handler import (
    FirstTimeTraceHandler,
)
from crewai.events.listeners.tracing.trace_batch_manager import TraceBatchManager
from crewai.events.listeners.tracing.types import TraceEvent
from crewai.events.listeners.tracing.utils import safe_serialize_to_dict
from crewai.events.types.agent_events import (
    AgentExecutionCompletedEvent,
    AgentExecutionErrorEvent,
    AgentExecutionStartedEvent,
    LiteAgentExecutionCompletedEvent,
    LiteAgentExecutionErrorEvent,
    LiteAgentExecutionStartedEvent,
)
from crewai.events.types.crew_events import (
    CrewKickoffCompletedEvent,
    CrewKickoffFailedEvent,
    CrewKickoffStartedEvent,
)
from crewai.events.types.flow_events import (
    FlowCreatedEvent,
    FlowFinishedEvent,
    FlowPlotEvent,
    FlowStartedEvent,
    MethodExecutionFailedEvent,
    MethodExecutionFinishedEvent,
    MethodExecutionStartedEvent,
)
from crewai.events.types.knowledge_events import (
    KnowledgeQueryCompletedEvent,
    KnowledgeQueryFailedEvent,
    KnowledgeQueryStartedEvent,
    KnowledgeRetrievalCompletedEvent,
    KnowledgeRetrievalStartedEvent,
)
from crewai.events.types.llm_events import (
    LLMCallCompletedEvent,
    LLMCallFailedEvent,
    LLMCallStartedEvent,
)
from crewai.events.types.llm_guardrail_events import (
    LLMGuardrailCompletedEvent,
    LLMGuardrailStartedEvent,
)
from crewai.events.types.memory_events import (
    MemoryQueryCompletedEvent,
    MemoryQueryFailedEvent,
    MemoryQueryStartedEvent,
    MemoryRetrievalCompletedEvent,
    MemoryRetrievalStartedEvent,
    MemorySaveCompletedEvent,
    MemorySaveFailedEvent,
    MemorySaveStartedEvent,
)
from crewai.events.types.reasoning_events import (
    AgentReasoningCompletedEvent,
    AgentReasoningFailedEvent,
    AgentReasoningStartedEvent,
)
from crewai.events.types.task_events import (
    TaskCompletedEvent,
    TaskFailedEvent,
    TaskStartedEvent,
)
from crewai.events.types.tool_usage_events import (
    ToolUsageErrorEvent,
    ToolUsageFinishedEvent,
    ToolUsageStartedEvent,
)


class TraceCollectionListener(BaseEventListener):
    """Trace collection listener that orchestrates trace collection."""

    complex_events: ClassVar[list[str]] = [
        "task_started",
        "task_completed",
        "llm_call_started",
        "llm_call_completed",
        "agent_execution_started",
        "agent_execution_completed",
    ]

    _instance: Self | None = None
    _initialized: bool = False
    _listeners_setup: bool = False

    def __new__(cls, batch_manager: TraceBatchManager | None = None) -> Self:
        """Create or return singleton instance."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(
        self,
        batch_manager: TraceBatchManager | None = None,
        formatter: ConsoleFormatter | None = None,
    ) -> None:
        """Initialize trace collection listener.

        Args:
            batch_manager: Optional trace batch manager instance.
            formatter: Optional console formatter for output.
        """
        if self._initialized:
            return

        super().__init__()
        self.batch_manager = batch_manager or TraceBatchManager()
        self._initialized = True
        self.first_time_handler = FirstTimeTraceHandler()
        self.formatter = formatter
        self.memory_retrieval_in_progress = False
        self.memory_save_in_progress = False

        if self.first_time_handler.initialize_for_first_time_user():
            self.first_time_handler.set_batch_manager(self.batch_manager)

    def _check_authenticated(self) -> bool:
        """Check if tracing should be enabled."""
        try:
            return bool(get_auth_token())
        except AuthError:
            return False

    def _get_user_context(self) -> dict[str, str]:
        """Extract user context for tracing."""
        return {
            "user_id": os.getenv("CREWAI_USER_ID", "anonymous"),
            "organization_id": os.getenv("CREWAI_ORG_ID", ""),
            "session_id": str(uuid.uuid4()),
            "trace_id": str(uuid.uuid4()),
        }

    def setup_listeners(self, crewai_event_bus: CrewAIEventsBus) -> None:
        """Setup event listeners - delegates to specific handlers.

        Args:
            crewai_event_bus: The event bus to register listeners on.
        """
        if self._listeners_setup:
            return

        self._register_flow_event_handlers(crewai_event_bus)
        self._register_context_event_handlers(crewai_event_bus)
        self._register_action_event_handlers(crewai_event_bus)

        self._listeners_setup = True

    def _register_flow_event_handlers(self, event_bus: CrewAIEventsBus) -> None:
        """Register handlers for flow events."""

        @event_bus.on(FlowCreatedEvent)
        def on_flow_created(source: Any, event: FlowCreatedEvent) -> None:
            pass

        @event_bus.on(FlowStartedEvent)
        def on_flow_started(source: Any, event: FlowStartedEvent) -> None:
            if not self.batch_manager.is_batch_initialized():
                self._initialize_flow_batch(source, event)
            self._handle_trace_event("flow_started", source, event)

        @event_bus.on(MethodExecutionStartedEvent)
        def on_method_started(source: Any, event: MethodExecutionStartedEvent) -> None:
            self._handle_trace_event("method_execution_started", source, event)

        @event_bus.on(MethodExecutionFinishedEvent)
        def on_method_finished(
            source: Any, event: MethodExecutionFinishedEvent
        ) -> None:
            self._handle_trace_event("method_execution_finished", source, event)

        @event_bus.on(MethodExecutionFailedEvent)
        def on_method_failed(source: Any, event: MethodExecutionFailedEvent) -> None:
            self._handle_trace_event("method_execution_failed", source, event)

        @event_bus.on(FlowFinishedEvent)
        def on_flow_finished(source: Any, event: FlowFinishedEvent) -> None:
            self._handle_trace_event("flow_finished", source, event)

        @event_bus.on(FlowPlotEvent)
        def on_flow_plot(source: Any, event: FlowPlotEvent) -> None:
            self._handle_action_event("flow_plot", source, event)

    def _register_context_event_handlers(self, event_bus: CrewAIEventsBus) -> None:
        """Register handlers for context events (start/end)."""

        @event_bus.on(CrewKickoffStartedEvent)
        def on_crew_started(source: Any, event: CrewKickoffStartedEvent) -> None:
            if not self.batch_manager.is_batch_initialized():
                self._initialize_crew_batch(source, event)
            self._handle_trace_event("crew_kickoff_started", source, event)

        @event_bus.on(CrewKickoffCompletedEvent)
        def on_crew_completed(source: Any, event: CrewKickoffCompletedEvent) -> None:
            self._handle_trace_event("crew_kickoff_completed", source, event)
            if self.batch_manager.batch_owner_type == "crew":
                if self.first_time_handler.is_first_time:
                    self.first_time_handler.mark_events_collected()
                    self.first_time_handler.handle_execution_completion()
                else:
                    self.batch_manager.finalize_batch()

        @event_bus.on(CrewKickoffFailedEvent)
        def on_crew_failed(source: Any, event: CrewKickoffFailedEvent) -> None:
            self._handle_trace_event("crew_kickoff_failed", source, event)
            if self.first_time_handler.is_first_time:
                self.first_time_handler.mark_events_collected()
                self.first_time_handler.handle_execution_completion()
            else:
                self.batch_manager.finalize_batch()

        @event_bus.on(TaskStartedEvent)
        def on_task_started(source: Any, event: TaskStartedEvent) -> None:
            self._handle_trace_event("task_started", source, event)

        @event_bus.on(TaskCompletedEvent)
        def on_task_completed(source: Any, event: TaskCompletedEvent) -> None:
            self._handle_trace_event("task_completed", source, event)

        @event_bus.on(TaskFailedEvent)
        def on_task_failed(source: Any, event: TaskFailedEvent) -> None:
            self._handle_trace_event("task_failed", source, event)

        @event_bus.on(AgentExecutionStartedEvent)
        def on_agent_started(source: Any, event: AgentExecutionStartedEvent) -> None:
            self._handle_trace_event("agent_execution_started", source, event)

        @event_bus.on(AgentExecutionCompletedEvent)
        def on_agent_completed(
            source: Any, event: AgentExecutionCompletedEvent
        ) -> None:
            self._handle_trace_event("agent_execution_completed", source, event)

        @event_bus.on(LiteAgentExecutionStartedEvent)
        def on_lite_agent_started(
            source: Any, event: LiteAgentExecutionStartedEvent
        ) -> None:
            self._handle_trace_event("lite_agent_execution_started", source, event)

        @event_bus.on(LiteAgentExecutionCompletedEvent)
        def on_lite_agent_completed(
            source: Any, event: LiteAgentExecutionCompletedEvent
        ) -> None:
            self._handle_trace_event("lite_agent_execution_completed", source, event)

        @event_bus.on(LiteAgentExecutionErrorEvent)
        def on_lite_agent_error(
            source: Any, event: LiteAgentExecutionErrorEvent
        ) -> None:
            self._handle_trace_event("lite_agent_execution_error", source, event)

        @event_bus.on(AgentExecutionErrorEvent)
        def on_agent_error(source: Any, event: AgentExecutionErrorEvent) -> None:
            self._handle_trace_event("agent_execution_error", source, event)

        @event_bus.on(LLMGuardrailStartedEvent)
        def on_guardrail_started(source: Any, event: LLMGuardrailStartedEvent) -> None:
            self._handle_trace_event("llm_guardrail_started", source, event)

        @event_bus.on(LLMGuardrailCompletedEvent)
        def on_guardrail_completed(
            source: Any, event: LLMGuardrailCompletedEvent
        ) -> None:
            self._handle_trace_event("llm_guardrail_completed", source, event)

    def _register_action_event_handlers(self, event_bus: CrewAIEventsBus) -> None:
        """Register handlers for action events (LLM calls, tool usage)."""

        @event_bus.on(LLMCallStartedEvent)
        def on_llm_call_started(source: Any, event: LLMCallStartedEvent) -> None:
            self._handle_action_event("llm_call_started", source, event)

        @event_bus.on(LLMCallCompletedEvent)
        def on_llm_call_completed(source: Any, event: LLMCallCompletedEvent) -> None:
            self._handle_action_event("llm_call_completed", source, event)

        @event_bus.on(LLMCallFailedEvent)
        def on_llm_call_failed(source: Any, event: LLMCallFailedEvent) -> None:
            self._handle_action_event("llm_call_failed", source, event)

        @event_bus.on(ToolUsageStartedEvent)
        def on_tool_started(source: Any, event: ToolUsageStartedEvent) -> None:
            self._handle_action_event("tool_usage_started", source, event)

        @event_bus.on(ToolUsageFinishedEvent)
        def on_tool_finished(source: Any, event: ToolUsageFinishedEvent) -> None:
            self._handle_action_event("tool_usage_finished", source, event)

        @event_bus.on(ToolUsageErrorEvent)
        def on_tool_error(source: Any, event: ToolUsageErrorEvent) -> None:
            self._handle_action_event("tool_usage_error", source, event)

        @event_bus.on(MemoryQueryStartedEvent)
        def on_memory_query_started(
            source: Any, event: MemoryQueryStartedEvent
        ) -> None:
            self._handle_action_event("memory_query_started", source, event)

        @event_bus.on(MemoryQueryCompletedEvent)
        def on_memory_query_completed(
            source: Any, event: MemoryQueryCompletedEvent
        ) -> None:
            self._handle_action_event("memory_query_completed", source, event)
            if self.formatter and self.memory_retrieval_in_progress:
                self.formatter.handle_memory_query_completed(
                    self.formatter.current_agent_branch,
                    event.source_type or "memory",
                    event.query_time_ms,
                    self.formatter.current_crew_tree,
                )

        @event_bus.on(MemoryQueryFailedEvent)
        def on_memory_query_failed(source: Any, event: MemoryQueryFailedEvent) -> None:
            self._handle_action_event("memory_query_failed", source, event)
            if self.formatter and self.memory_retrieval_in_progress:
                self.formatter.handle_memory_query_failed(
                    self.formatter.current_agent_branch,
                    self.formatter.current_crew_tree,
                    event.error,
                    event.source_type or "memory",
                )

        @event_bus.on(MemorySaveStartedEvent)
        def on_memory_save_started(source: Any, event: MemorySaveStartedEvent) -> None:
            self._handle_action_event("memory_save_started", source, event)
            if self.formatter:
                if self.memory_save_in_progress:
                    return

                self.memory_save_in_progress = True

                self.formatter.handle_memory_save_started(
                    self.formatter.current_agent_branch,
                    self.formatter.current_crew_tree,
                )

        @event_bus.on(MemorySaveCompletedEvent)
        def on_memory_save_completed(
            source: Any, event: MemorySaveCompletedEvent
        ) -> None:
            self._handle_action_event("memory_save_completed", source, event)
            if self.formatter:
                if not self.memory_save_in_progress:
                    return

                self.memory_save_in_progress = False

                self.formatter.handle_memory_save_completed(
                    self.formatter.current_agent_branch,
                    self.formatter.current_crew_tree,
                    event.save_time_ms,
                    event.source_type or "memory",
                )

        @event_bus.on(MemorySaveFailedEvent)
        def on_memory_save_failed(source: Any, event: MemorySaveFailedEvent) -> None:
            self._handle_action_event("memory_save_failed", source, event)
            if self.formatter and self.memory_save_in_progress:
                self.formatter.handle_memory_save_failed(
                    self.formatter.current_agent_branch,
                    event.error,
                    event.source_type or "memory",
                    self.formatter.current_crew_tree,
                )

        @event_bus.on(MemoryRetrievalStartedEvent)
        def on_memory_retrieval_started(
            source: Any, event: MemoryRetrievalStartedEvent
        ) -> None:
            if self.formatter:
                if self.memory_retrieval_in_progress:
                    return

                self.memory_retrieval_in_progress = True

                self.formatter.handle_memory_retrieval_started(
                    self.formatter.current_agent_branch,
                    self.formatter.current_crew_tree,
                )

        @event_bus.on(MemoryRetrievalCompletedEvent)
        def on_memory_retrieval_completed(
            source: Any, event: MemoryRetrievalCompletedEvent
        ) -> None:
            if self.formatter:
                if not self.memory_retrieval_in_progress:
                    return

                self.memory_retrieval_in_progress = False
                self.formatter.handle_memory_retrieval_completed(
                    self.formatter.current_agent_branch,
                    self.formatter.current_crew_tree,
                    event.memory_content,
                    event.retrieval_time_ms,
                )

        @event_bus.on(AgentReasoningStartedEvent)
        def on_agent_reasoning_started(
            source: Any, event: AgentReasoningStartedEvent
        ) -> None:
            self._handle_action_event("agent_reasoning_started", source, event)

        @event_bus.on(AgentReasoningCompletedEvent)
        def on_agent_reasoning_completed(
            source: Any, event: AgentReasoningCompletedEvent
        ) -> None:
            self._handle_action_event("agent_reasoning_completed", source, event)

        @event_bus.on(AgentReasoningFailedEvent)
        def on_agent_reasoning_failed(
            source: Any, event: AgentReasoningFailedEvent
        ) -> None:
            self._handle_action_event("agent_reasoning_failed", source, event)

        @event_bus.on(KnowledgeRetrievalStartedEvent)
        def on_knowledge_retrieval_started(
            source: Any, event: KnowledgeRetrievalStartedEvent
        ) -> None:
            self._handle_action_event("knowledge_retrieval_started", source, event)

        @event_bus.on(KnowledgeRetrievalCompletedEvent)
        def on_knowledge_retrieval_completed(
            source: Any, event: KnowledgeRetrievalCompletedEvent
        ) -> None:
            self._handle_action_event("knowledge_retrieval_completed", source, event)

        @event_bus.on(KnowledgeQueryStartedEvent)
        def on_knowledge_query_started(
            source: Any, event: KnowledgeQueryStartedEvent
        ) -> None:
            self._handle_action_event("knowledge_query_started", source, event)

        @event_bus.on(KnowledgeQueryCompletedEvent)
        def on_knowledge_query_completed(
            source: Any, event: KnowledgeQueryCompletedEvent
        ) -> None:
            self._handle_action_event("knowledge_query_completed", source, event)

        @event_bus.on(KnowledgeQueryFailedEvent)
        def on_knowledge_query_failed(
            source: Any, event: KnowledgeQueryFailedEvent
        ) -> None:
            self._handle_action_event("knowledge_query_failed", source, event)

    def _initialize_crew_batch(self, source: Any, event: Any) -> None:
        """Initialize trace batch.

        Args:
            source: Source object that triggered the event.
            event: Event object containing crew information.
        """
        user_context = self._get_user_context()
        execution_metadata = {
            "crew_name": getattr(event, "crew_name", "Unknown Crew"),
            "execution_start": event.timestamp if hasattr(event, "timestamp") else None,
            "crewai_version": get_crewai_version(),
        }

        self.batch_manager.batch_owner_type = "crew"
        self.batch_manager.batch_owner_id = getattr(source, "id", str(uuid.uuid4()))

        self._initialize_batch(user_context, execution_metadata)

    def _initialize_flow_batch(self, source: Any, event: Any) -> None:
        """Initialize trace batch for Flow execution.

        Args:
            source: Source object that triggered the event.
            event: Event object containing flow information.
        """
        user_context = self._get_user_context()
        execution_metadata = {
            "flow_name": getattr(event, "flow_name", "Unknown Flow"),
            "execution_start": event.timestamp if hasattr(event, "timestamp") else None,
            "crewai_version": get_crewai_version(),
            "execution_type": "flow",
        }

        self.batch_manager.batch_owner_type = "flow"
        self.batch_manager.batch_owner_id = getattr(source, "id", str(uuid.uuid4()))

        self._initialize_batch(user_context, execution_metadata)

    def _initialize_batch(
        self, user_context: dict[str, str], execution_metadata: dict[str, Any]
    ) -> None:
        """Initialize trace batch - auto-enable ephemeral for first-time users.

        Args:
            user_context: User context information.
            execution_metadata: Metadata about the execution.
        """
        if self.first_time_handler.is_first_time:
            self.batch_manager.initialize_batch(
                user_context, execution_metadata, use_ephemeral=True
            )
            return

        use_ephemeral = not self._check_authenticated()
        self.batch_manager.initialize_batch(
            user_context, execution_metadata, use_ephemeral=use_ephemeral
        )

    def _handle_trace_event(self, event_type: str, source: Any, event: Any) -> None:
        """Generic handler for context end events.

        Args:
            event_type: Type of the event.
            source: Source object that triggered the event.
            event: Event object.
        """
        self.batch_manager.begin_event_processing()
        try:
            trace_event = self._create_trace_event(event_type, source, event)
            self.batch_manager.add_event(trace_event)
        finally:
            self.batch_manager.end_event_processing()

    def _handle_action_event(self, event_type: str, source: Any, event: Any) -> None:
        """Generic handler for action events (LLM calls, tool usage).

        Args:
            event_type: Type of the event.
            source: Source object that triggered the event.
            event: Event object.
        """
        if not self.batch_manager.is_batch_initialized():
            user_context = self._get_user_context()
            execution_metadata = {
                "crew_name": getattr(source, "name", "Unknown Crew"),
                "crewai_version": get_crewai_version(),
            }
            self.batch_manager.initialize_batch(user_context, execution_metadata)

        self.batch_manager.begin_event_processing()
        try:
            trace_event = self._create_trace_event(event_type, source, event)
            self.batch_manager.add_event(trace_event)
        finally:
            self.batch_manager.end_event_processing()

    def _create_trace_event(
        self, event_type: str, source: Any, event: Any
    ) -> TraceEvent:
        """Create a trace event"""
        if hasattr(event, "timestamp") and event.timestamp:
            trace_event = TraceEvent(
                type=event_type,
                timestamp=event.timestamp.isoformat(),
            )
        else:
            trace_event = TraceEvent(
                type=event_type,
            )

        trace_event.event_data = self._build_event_data(event_type, event, source)

        return trace_event

    def _build_event_data(
        self, event_type: str, event: Any, source: Any
    ) -> dict[str, Any]:
        """Build event data"""
        if event_type not in self.complex_events:
            return safe_serialize_to_dict(event)
        if event_type == "task_started":
            return {
                "task_description": event.task.description,
                "expected_output": event.task.expected_output,
                "task_name": event.task.name or event.task.description,
                "context": event.context,
                "agent_role": source.agent.role,
                "task_id": str(event.task.id),
            }
        if event_type == "task_completed":
            return {
                "task_description": event.task.description if event.task else None,
                "task_name": event.task.name or event.task.description
                if event.task
                else None,
                "task_id": str(event.task.id) if event.task else None,
                "output_raw": event.output.raw if event.output else None,
                "output_format": str(event.output.output_format)
                if event.output
                else None,
                "agent_role": event.output.agent if event.output else None,
            }
        if event_type == "agent_execution_started":
            return {
                "agent_role": event.agent.role,
                "agent_goal": event.agent.goal,
                "agent_backstory": event.agent.backstory,
            }
        if event_type == "agent_execution_completed":
            return {
                "agent_role": event.agent.role,
                "agent_goal": event.agent.goal,
                "agent_backstory": event.agent.backstory,
            }
        if event_type == "llm_call_started":
            event_data = safe_serialize_to_dict(event)
            event_data["task_name"] = (
                event.task_name or event.task_description
                if hasattr(event, "task_name") and event.task_name
                else None
            )
            return event_data
        if event_type == "llm_call_completed":
            return safe_serialize_to_dict(event)

        return {
            "event_type": event_type,
            "event": safe_serialize_to_dict(event),
            "source": source,
        }
