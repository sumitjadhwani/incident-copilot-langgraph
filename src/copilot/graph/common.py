r"""Building blocks shared by the agent subgraphs.

`build_react_agent` creates a small subgraph:

    [prepare] -> agent --(tool calls, budget left)--> tools -> agent ...
                      \--(done or budget spent)----> conclude -> END

`agent` uses the thinking-enabled model with tools bound; `conclude` asks the structured model for
the agent's typed result. Tool calls are recorded and streamed as custom events, and the tool node
retries transient backend failures with a RetryPolicy.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from langchain_core.exceptions import OutputParserException
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.runtime import Runtime
from langgraph.types import RetryPolicy
from pydantic import BaseModel, ValidationError

from copilot.config import Models
from copilot.context import CopilotContext
from copilot.graph.prompts import CONCLUDE_INSTRUCTION
from copilot.tools.backend import TransientBackendError

_default_retry_on = RetryPolicy().retry_on
_RETRYABLE_API_ERRORS = {"APIConnectionError", "APITimeoutError", "RateLimitError", "InternalServerError"}


def _llm_retryable(exc: Exception) -> bool:
    if isinstance(exc, OutputParserException | ValidationError):
        return True
    if type(exc).__name__ in _RETRYABLE_API_ERRORS:
        return True
    return callable(_default_retry_on) and _default_retry_on(exc)


# Model calls: transient API failures and malformed structured output.
LLM_RETRY = RetryPolicy(max_attempts=3, initial_interval=1.0, backoff_factor=2.0, retry_on=_llm_retryable)
# Tool calls: only failures the backend marks as transient.
TOOL_RETRY = RetryPolicy(max_attempts=4, initial_interval=0.2, backoff_factor=2.0, retry_on=TransientBackendError)

PREVIEW_CHARS = 400


def emit(runtime: Runtime | None, event: dict[str, Any]) -> None:
    """Send a custom stream event (no-op when the caller is not streaming custom events)."""
    writer = getattr(runtime, "stream_writer", None)
    if writer is not None:
        writer(event)


def preview(text: Any, limit: int = PREVIEW_CHARS) -> str:
    s = text if isinstance(text, str) else json.dumps(text, default=str)
    s = s.replace("\n", " ")
    return s if len(s) <= limit else s[:limit] + " ..."


def message_text(msg: Any) -> str:
    text = getattr(msg, "text", None)
    if isinstance(text, str):
        return str(text)
    return str(getattr(msg, "content", ""))


def structured(model: Any, schema: type[BaseModel]) -> Any:
    return model.with_structured_output(schema, method="function_calling")


def make_tool_node(agent: str, tools: Sequence[BaseTool], label: Callable[[dict], str | None]) -> ToolNode:
    async def record_and_emit(request: Any, execute: Callable[[Any], Awaitable[Any]]) -> Any:
        runtime = request.runtime
        ctx: CopilotContext | None = getattr(runtime, "context", None)
        call = request.tool_call
        who = label(request.state) if isinstance(request.state, dict) else None
        try:
            result = await execute(request)
        except TransientBackendError as exc:
            if ctx and ctx.recorder:
                ctx.recorder.record(agent=agent, label=who, tool=call["name"], args=call["args"], ok=False)
            emit(
                runtime,
                {
                    "type": "tool_error",
                    "agent": agent,
                    "label": who,
                    "tool": call["name"],
                    "error": str(exc),
                    "retrying": True,
                },
            )
            raise
        content = result.content if isinstance(result, ToolMessage) else str(result)
        if ctx and ctx.recorder:
            ctx.recorder.record(agent=agent, label=who, tool=call["name"], args=call["args"], ok=True)
        emit(
            runtime,
            {
                "type": "tool_result",
                "agent": agent,
                "label": who,
                "tool": call["name"],
                "args": call["args"],
                "preview": preview(content),
            },
        )
        return result

    return ToolNode(list(tools), awrap_tool_call=record_and_emit)


def build_react_agent(
    *,
    name: str,
    models: Models,
    tools: Sequence[BaseTool],
    system_prompt: str,
    task: Callable[[dict], str],
    output_model: type[BaseModel],
    finish: Callable[[BaseModel, dict], dict],
    max_tool_rounds: int,
    state_schema: type,
    input_schema: type | None = None,
    output_schema: type | None = None,
    label: Callable[[dict], str | None] = lambda _s: None,
    prepare: Callable[[dict, Runtime], Awaitable[dict]] | None = None,
):
    llm = models.agent.bind_tools(list(tools))
    concluder = structured(models.structured, output_model)

    async def agent(state: dict, runtime: Runtime[CopilotContext]) -> dict:
        history = list(state.get("messages") or [])
        new: list[Any] = []
        if not history:
            new.append(HumanMessage(task(state)))
        response: AIMessage = await llm.ainvoke([SystemMessage(system_prompt), *history, *new])
        emit(
            runtime,
            {
                "type": "agent_step",
                "agent": name,
                "label": label(state),
                "thought": preview(message_text(response), 600),
                "tool_calls": [{"tool": c["name"], "args": c["args"]} for c in response.tool_calls],
            },
        )
        rounds = state.get("tool_rounds", 0) + (1 if response.tool_calls else 0)
        return {"messages": [*new, response], "tool_rounds": rounds}

    def route(state: dict) -> str:
        last = state["messages"][-1]
        if getattr(last, "tool_calls", None) and state.get("tool_rounds", 0) <= max_tool_rounds:
            return "tools"
        return "conclude"

    async def conclude(state: dict, runtime: Runtime[CopilotContext]) -> dict:
        history = list(state["messages"])
        if history and getattr(history[-1], "tool_calls", None):
            history = history[:-1]  # budget spent: drop the unanswered tool calls
        result = await concluder.ainvoke([SystemMessage(system_prompt), *history, HumanMessage(CONCLUDE_INSTRUCTION)])
        if result is None:
            raise OutputParserException(f"{name}: model returned no structured output")
        emit(
            runtime,
            {"type": "agent_result", "agent": name, "label": label(state), "result": result.model_dump(mode="json")},
        )
        return finish(result, state)

    kwargs: dict[str, Any] = {"context_schema": CopilotContext}
    if input_schema:
        kwargs["input_schema"] = input_schema
    if output_schema:
        kwargs["output_schema"] = output_schema
    builder = StateGraph(state_schema, **kwargs)
    builder.add_node("agent", agent, retry_policy=LLM_RETRY)
    builder.add_node("tools", make_tool_node(name, tools, label), retry_policy=TOOL_RETRY)
    builder.add_node("conclude", conclude, retry_policy=LLM_RETRY)
    if prepare is not None:
        builder.add_node("prepare", prepare)
        builder.add_edge(START, "prepare")
        builder.add_edge("prepare", "agent")
    else:
        builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", route, {"tools": "tools", "conclude": "conclude"})
    builder.add_edge("tools", "agent")
    builder.add_edge("conclude", END)
    return builder.compile(name=name)
