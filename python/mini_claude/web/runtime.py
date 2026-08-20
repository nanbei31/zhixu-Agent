"""Web session orchestration backed by the production Agent loop."""

from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass, field
from typing import Any

from ..agent import Agent
from ..tools import tool_definitions
from .events import EventBus
from .task_classifier import classify_task
from .workspace import WebWorkspace, WorkspaceManager


def resolve_model_config() -> dict[str, Any]:
    model = os.environ.get("MINI_CLAUDE_MODEL", "claude-opus-4-6")
    openai_key = os.environ.get("OPENAI_API_KEY")
    openai_base = os.environ.get("OPENAI_BASE_URL")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if openai_key:
        return {
            "configured": True,
            "provider": "openai-compatible",
            "model": model,
            "api_key": openai_key,
            "api_base": openai_base or "https://api.openai.com/v1",
        }
    if anthropic_key:
        return {
            "configured": True,
            "provider": "anthropic",
            "model": model,
            "api_key": anthropic_key,
            "anthropic_base_url": os.environ.get("ANTHROPIC_BASE_URL"),
        }
    return {
        "configured": False,
        "provider": "未配置",
        "model": model,
        "api_key": None,
    }


@dataclass
class AgentSession:
    id: str
    workspace: WebWorkspace
    agent: Agent
    bus: EventBus
    allow_shell: bool
    task: asyncio.Task | None = None
    pending_confirmation: asyncio.Future | None = None
    pending_confirmation_id: str | None = None


class SessionManager:
    def __init__(self, workspaces: WorkspaceManager):
        self.workspaces = workspaces
        self.sessions: dict[str, AgentSession] = {}

    def create(self, workspace_id: str, *, allow_shell: bool = False) -> AgentSession:
        workspace = self.workspaces.get(workspace_id)
        config = resolve_model_config()
        if not config["configured"]:
            raise ValueError(
                "尚未配置模型。请设置 ANTHROPIC_API_KEY，或设置 "
                "OPENAI_API_KEY 与 OPENAI_BASE_URL。"
            )
        bus = EventBus()
        policy = workspace.policy(allow_shell)
        effective_allow_shell = policy.allow_agent_shell
        allowed_tools = [
            tool for tool in tool_definitions
            if tool["name"] in policy.allowed_agent_tools
        ]
        session_id = uuid.uuid4().hex[:12]

        def event_sink(event_type: str, data: dict[str, Any]) -> None:
            bus.publish(event_type, data)

        kwargs = {
            "permission_mode": "acceptEdits",
            "model": config["model"],
            "api_key": config["api_key"],
            "custom_tools": allowed_tools,
            "workspace_policy": policy,
            "working_directory": workspace.root,
            "event_sink": event_sink,
            "quiet": True,
            "enable_memory": False,
            "enable_mcp": False,
        }
        if config["provider"] == "openai-compatible":
            kwargs["api_base"] = config.get("api_base")
        else:
            kwargs["anthropic_base_url"] = config.get("anthropic_base_url")
        agent = Agent(**kwargs)
        session = AgentSession(session_id, workspace, agent, bus, effective_allow_shell)

        async def confirm(message: str) -> bool:
            confirmation_id = uuid.uuid4().hex[:10]
            future = asyncio.get_running_loop().create_future()
            session.pending_confirmation = future
            session.pending_confirmation_id = confirmation_id
            bus.publish("confirmation_required", {
                "confirmation_id": confirmation_id,
                "message": message,
            })
            try:
                return bool(await future)
            finally:
                session.pending_confirmation = None
                session.pending_confirmation_id = None

        agent.set_confirm_fn(confirm)
        bus.publish("session_created", {
            "session_id": session_id,
            "workspace": str(workspace.root),
            "model": config["model"],
            "provider": config["provider"],
            "allow_shell": effective_allow_shell,
        })
        self.sessions[session_id] = session
        return session

    def get(self, session_id: str) -> AgentSession:
        session = self.sessions.get(session_id)
        if session is None:
            raise KeyError("会话不存在或服务已重启")
        return session

    def send(self, session_id: str, message: str) -> dict:
        session = self.get(session_id)
        if session.task and not session.task.done():
            raise RuntimeError("Agent 正在执行当前任务，请先等待或停止")
        message = message.strip()
        if not message:
            raise ValueError("消息不能为空")
        session.bus.publish("user_message", {"text": message})
        classification = classify_task(message)
        session.bus.publish("task_classified", classification)
        self.workspaces.create_checkpoint(session.workspace.id)

        async def run() -> None:
            try:
                await session.agent.chat(message)
            finally:
                can_undo = self.workspaces.finalize_checkpoint(session.workspace.id)
                session.bus.publish("workspace_diff_updated", {
                    "changed_files": len(self.workspaces.diff(session.workspace.id)["files"]),
                    "can_undo": can_undo,
                })

        session.task = asyncio.create_task(run())
        return classification

    def confirm(self, session_id: str, confirmation_id: str, approved: bool) -> None:
        session = self.get(session_id)
        if (
            session.pending_confirmation is None
            or session.pending_confirmation.done()
            or session.pending_confirmation_id != confirmation_id
        ):
            raise ValueError("确认请求已过期或不存在")
        session.pending_confirmation.set_result(approved)
        session.bus.publish("confirmation_resolved", {
            "confirmation_id": confirmation_id,
            "approved": approved,
        })

    def abort(self, session_id: str) -> None:
        session = self.get(session_id)
        session.agent.abort()
        if session.pending_confirmation and not session.pending_confirmation.done():
            session.pending_confirmation.set_result(False)
