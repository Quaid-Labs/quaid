"""agentmsg — in-process message routing between named agents.

A minimal fan-out queue used as a livetest fixture. Not for production use.
"""

from .api import Mailbox, send, subscribe

__all__ = ["Mailbox", "send", "subscribe"]
