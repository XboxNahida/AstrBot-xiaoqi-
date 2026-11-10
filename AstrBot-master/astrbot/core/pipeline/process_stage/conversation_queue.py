"""An in-process FIFO shared by foreground turns and durable task callbacks."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from astrbot import logger
from astrbot.core.agent.message import Message

_QUEUES: dict[tuple[asyncio.AbstractEventLoop, str], deque[ConversationTurn]] = {}


@dataclass(eq=False)
class ConversationTurn:
    umo: str
    sender_id: str
    key: str
    kind: str
    prepare: Callable[[], Awaitable[Message | None]] | None
    queue: deque[ConversationTurn]
    loop: asyncio.AbstractEventLoop
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    consumed: bool = False
    committed: bool = False
    claimed: bool = False
    members: list[ConversationTurn] = field(default_factory=list)
    finished: asyncio.Future[str] = field(init=False)

    def __post_init__(self):
        self.finished = self.loop.create_future()

    async def wait_turn(self) -> bool:
        """Wait for ownership or capture by the preceding turn.

        Returns:
            Whether this entry owns the next foreground turn.
        """
        await self.ready.wait()
        if self.consumed or self.finished.done():
            return False
        if self.claimed:
            await asyncio.shield(self.finished)
            return False
        self.claimed = True
        return True

    async def drain(self) -> list[Message]:
        """Prepare a bounded snapshot of same-identity inputs in arrival order.

        Returns:
            Complete user messages, retaining image and attachment content.
        """
        if self.committed or not self.queue or self.queue[0] is not self:
            return []
        messages = []
        # A snapshot prevents a continuous producer from starving the consumer.
        for entry in list(self.queue)[1:]:
            if entry.sender_id != self.sender_id:
                break
            if self.kind == "callback" and entry.kind != "callback":
                break
            if entry.prepare is None:
                break
            try:
                message = await entry.prepare()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Queued input preparation failed; retaining its turn: %s",
                    type(exc).__name__,
                )
                break
            # A cancelled waiter must never be revived after an awaited prepare.
            if entry not in self.queue:
                continue
            self.queue.remove(entry)
            entry.consumed = True
            entry.ready.set()
            self.members.append(entry)
            if message is not None:
                messages.append(message)
        return messages

    def commit(self) -> None:
        """Close draft revision before handing output to delivery hooks."""
        self.committed = True

    def finish(self, outcome: str) -> None:
        """Release ownership using the actual delivery outcome.

        Args:
            outcome: sent, silent, handled, unknown, or not_sent.
        """
        if self.finished.done():
            return
        self.committed = True
        self.finished.set_result(outcome)
        self.ready.set()
        for entry in self.members:
            if not entry.finished.done():
                # Reading a callback does not prove a separate notification was sent.
                result = (
                    "handled" if outcome in {"sent", "silent", "handled"} else outcome
                )
                entry.finished.set_result(result)
        if self in self.queue:
            self.queue.remove(self)
        if self.queue:
            self.queue[0].ready.set()
        else:
            _QUEUES.pop((self.loop, self.umo), None)

    def cancel(self) -> None:
        """Remove an unconsumed waiter without cancelling another user's run."""
        if not self.consumed:
            self.finish("not_sent")


def enqueue_conversation(
    umo: str,
    sender_id: str,
    *,
    key: str,
    kind: str = "user",
    prepare: Callable[[], Awaitable[Message | None]] | None = None,
) -> ConversationTurn:
    """Join a session FIFO before performing asynchronous request preparation.

    Args:
        umo: Platform/bot-qualified conversation origin.
        sender_id: Trusted account identity, never a display name.
        key: Stable source message ID or durable outbox notification ID.
        kind: user or callback; callback owners cannot absorb user instructions.
        prepare: Lazy input preparation under the owning conversation lock.

    Returns:
        Existing idempotent entry or a newly queued turn.
    """
    loop = asyncio.get_running_loop()
    queue = _QUEUES.setdefault((loop, umo), deque())
    for entry in queue:
        for candidate in [entry, *entry.members]:
            if candidate.key == key and candidate.sender_id == sender_id:
                return candidate
    turn = ConversationTurn(umo, sender_id, key, kind, prepare, queue, loop)
    queue.append(turn)
    if len(queue) == 1:
        turn.ready.set()
    return turn
