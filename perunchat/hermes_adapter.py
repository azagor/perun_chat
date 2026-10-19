"""
Hermes Agent Framework Adapter

Registers PerunChat as OpenAI/Nous-compatible tool definitions for Hermes agents.
"""

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
                            "TIER_1_SCRATCHPAD",
                            "TIER_2_LOW_IMPACT",
                            "TIER_3_HIGH_IMPACT",
                        ],
                    },
                    "action_domain": {
                        "type": "string",
                        "enum": ["FILESYSTEM", "SHELL", "GIT", "API"],
                    },
                    "description": {"type": "string"},
                    "file_path": {"type": "string"},
                    "diff": {"type": "string"},
                    "command": {"type": "string"},
                    "justification": {"type": "string"},
                },
                "required": [
                    "room_id",
                    "risk_tier",
                    "action_domain",
                    "description",
                    "justification",
                ],
            },
        },
    },
]

__all__ = ["HERMES_PERUNCHAT_TOOLS"]
