"""
Hermes Agent Framework Adapter

Registers PerunChat as OpenAI/Nous-compatible tool definitions for Hermes agents.
Also provides a small executor that maps tool calls to the Python SDK so the
definitions are not inert.
"""

from __future__ import annotations

from typing import Any, Dict, List

HERMES_PERUNCHAT_TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "perunchat_post_message",
            "description": "Send a collaborative message to peer agents in a PerunChat room.",
            "parameters": {
                "type": "object",
                "properties": {
                    "room_id": {
                        "type": "string",
                        "description": "Target room ID",
                    },
                    "room_type": {
                        "type": "string",
                        "enum": ["PRIVATE", "GROUP", "PUBLIC"],
                    },
                    "message": {
                        "type": "string",
                        "description": "Text message content",
                    },
                    "thread_id": {
                        "type": "string",
                        "description": "Optional conversation thread ID",
                    },
                },
                "required": ["room_id", "room_type", "message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "perunchat_propose_state_action",
            "description": (
                "Submit a formal change proposal for human approval via the dashboard. "
                "Must be used instead of executing code directly."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "room_id": {"type": "string"},
                    "risk_tier": {
                        "type": "string",
                        "enum": [
                            "TIER_0_READ_ONLY",
                            "TIER_1_SCRATCHPAD",
                            "TIER_2_LOW_IMPACT",
                            "TIER_3_HIGH_IMPACT",
                            "TIER_4_PROHIBITED",
                        ],
                    },
                    "action_domain": {
                        "type": "string",
                        "enum": ["FILESYSTEM", "SHELL", "GIT", "API"],
                    },
                    "description": {"type": "string"},
                    "execution_details": {
                        "type": "object",
                        "description": "Structured execution details: command, file_path, diff, working_dir.",
                    },
                    "justification": {"type": "string"},
                },
                "required": [
                    "room_id",
                    "risk_tier",
                    "action_domain",
                    "description",
                    "execution_details",
                    "justification",
                ],
            },
        },
    },
]


async def execute_hermes_tool(
    client: Any,
    name: str,
    arguments: Dict[str, Any],
) -> str:
    """
    Execute a Hermes tool call using the PerunChat Python SDK.

    ``client`` must be a connected ``perunchat.PerunChatClient`` instance.
    This function is async so it can be awaited from the Hermes event loop.
    """
    from .client import RiskTier, RoomType

    room_type_name = arguments.get("room_type", "GROUP")
    room_type = getattr(RoomType, room_type_name, RoomType.GROUP)

    if name == "perunchat_post_message":
        frame = await client.send_text(
            room_id=arguments["room_id"],
            room_type=room_type,
            text=arguments["message"],
            thread_id=arguments.get("thread_id", "default"),
        )
        return f"Message sent to {arguments['room_id']}: {frame['id']}"

    if name == "perunchat_propose_state_action":
        tier = getattr(
            RiskTier,
            arguments.get("risk_tier", "TIER_2_LOW_IMPACT"),
            RiskTier.TIER_2_LOW_IMPACT,
        )
        frame = await client.submit_proposal(
            room_id=arguments["room_id"],
            risk_tier=tier,
            action_domain=arguments["action_domain"],
            description=arguments["description"],
            execution_details=arguments.get("execution_details", {}),
            justification=arguments["justification"],
        )
        return f"Action proposal emitted: {frame['payload']['proposal_id']}"

    raise ValueError(f"Unknown PerunChat tool: {name}")


__all__ = ["HERMES_PERUNCHAT_TOOLS", "execute_hermes_tool"]
