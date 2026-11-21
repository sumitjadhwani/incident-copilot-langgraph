"""Shared helpers for graph tests."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore

from copilot.approval.audit import InMemoryAuditStore
from copilot.approval.service import ApprovalService
from copilot.auth.rbac import ThreadInfo
from copilot.config import Models, Settings
from copilot.context import Actor, CopilotContext, ToolCallRecorder
from copilot.graph.build import build_graph
from copilot.scenarios import load_scenario
from copilot.tools.mock_backend import ScenarioBackend
from copilot.tools.write_tools import RiskPolicy
from tests.fakes import FakeModel, oom_responder

ALICE = Actor("u-alice", "alice", "acme", frozenset({"sre"}))
BOB = Actor("u-bob", "bob", "acme", frozenset({"sre", "sre-approver"}))
CAROL = Actor("u-carol", "carol", "globex", frozenset({"sre", "sre-approver"}))


def make_settings(**overrides: Any) -> Settings:
    return Settings(**{"auth_mode": "dev", **overrides})


@dataclass
class Harness:
    """A compiled graph on in-memory checkpointer, store and audit log, playing one scenario."""

    responder: Any = None
    scenario_id: str = "oomkilled_loop"
    settings: Settings = field(default_factory=make_settings)
    risk_policy: RiskPolicy = field(default_factory=RiskPolicy)
    backend: Any = None
    store: Any = None

    def __post_init__(self) -> None:
        model = FakeModel(self.responder or oom_responder())
        self.model = model
        self.audit = InMemoryAuditStore()
        self.store = self.store if self.store is not None else InMemoryStore()
        self.checkpointer = InMemorySaver()
        self.scenario = load_scenario(self.scenario_id)
        self.backend = self.backend or ScenarioBackend(self.scenario)
        self.recorder = ToolCallRecorder()
        self.graph = build_graph(
            Models(agent=model, structured=model),  # type: ignore[arg-type]
            settings=self.settings,
            checkpointer=self.checkpointer,
            store=self.store,
        )
        self.thread_id = f"t-{uuid.uuid4().hex[:8]}"
        self.thread = ThreadInfo(self.thread_id, ALICE.tenant_id, ALICE.user_id)
        self.approvals = ApprovalService(self.graph, self.audit, self.settings)

    @property
    def config(self) -> dict[str, Any]:
        return {"configurable": {"thread_id": self.thread_id}, "recursion_limit": 100}

    def context(self, actor: Actor = ALICE) -> CopilotContext:
        return CopilotContext(
            backend=self.backend, actor=actor, audit=self.audit, risk_policy=self.risk_policy, recorder=self.recorder
        )

    def inputs(self) -> dict[str, Any]:
        return {"alert": self.scenario.alert.model_dump(), "tenant_id": ALICE.tenant_id, "requested_by": ALICE.user_id}

    async def start(self) -> dict[str, Any]:
        return await self.graph.ainvoke(self.inputs(), self.config, context=self.context())

    async def resume(self, command: Any, actor: Actor = BOB) -> dict[str, Any]:
        return await self.graph.ainvoke(command, self.config, context=self.context(actor))

    async def state(self) -> dict[str, Any]:
        return (await self.graph.aget_state(self.config)).values
