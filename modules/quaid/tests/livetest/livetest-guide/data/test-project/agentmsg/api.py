"""Public API for agentmsg — send/subscribe/deliver over named mailboxes."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Callable, Deque


_MAILBOXES: dict[str, "Mailbox"] = {}
_SUBSCRIBERS: dict[str, list[Callable[["Message"], None]]] = defaultdict(list)


@dataclass(frozen=True)
class Message:
    sender: str
    body: str


class Mailbox:
    """Per-agent message queue. Bounded FIFO with synchronous delivery."""

    def __init__(self, name: str, capacity: int = 64) -> None:
        self.name = name
        self._queue: Deque[Message] = deque(maxlen=capacity)

    def deliver(self, msg: Message) -> None:
        """Append a message and fan out to subscribers."""
        self._queue.append(msg)
        for cb in _SUBSCRIBERS.get(self.name, ()):
            cb(msg)

    def pop(self) -> Message | None:
        return self._queue.popleft() if self._queue else None

    def __len__(self) -> int:
        return len(self._queue)


def _get_or_create(name: str) -> Mailbox:
    mb = _MAILBOXES.get(name)
    if mb is None:
        mb = Mailbox(name)
        _MAILBOXES[name] = mb
    return mb


def send(sender: str, target: str, body: str) -> None:
    """Send `body` from `sender` to the named `target` mailbox."""
    _get_or_create(target).deliver(Message(sender=sender, body=body))


def subscribe(target: str, callback: Callable[[Message], None]) -> None:
    """Register `callback` for every message delivered to `target`."""
    _SUBSCRIBERS[target].append(callback)
