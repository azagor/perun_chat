"""PerunChat — federated agent-to-agent communication subsystem."""

from .client import PerunChatClient, RiskTier, RoomType
from .custom_runtime import InMemoryTransport, PerunChatRuntime, Transport
from .models import ActionProposal, AgentKey, ChatFrame, ContentType, Runtime, SenderInfo
from .security import generate_api_key
from .social import SocialGraph, SocialGraphError

__all__ = [
    "PerunChatClient",
    "PerunChatRuntime",
    "InMemoryTransport",
    "Transport",
    "RoomType",
    "RiskTier",
    "ContentType",
    "Runtime",
    "ChatFrame",
    "ActionProposal",
    "SenderInfo",
    "AgentKey",
    "SocialGraph",
    "SocialGraphError",
    "generate_api_key",
]
__version__ = "0.3.0"
