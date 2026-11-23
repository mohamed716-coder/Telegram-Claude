"""Sends a message to the target bot from your account and collects its answer."""

import asyncio
from typing import Awaitable, Callable

from telethon import TelegramClient, events
from telethon.tl.custom import Message

# deliver(messages, is_update): called with the first answer, then with every later edit.
Deliver = Callable[[list[Message], bool], Awaitable[None]]


def _content(msg) -> tuple:
    """What the user actually sees, so edits that change nothing visible are not re-sent."""
    file = getattr(msg, "file", None)
    return (
        getattr(msg, "message", None) or getattr(msg, "text", None) or "",
        (file.size, file.mime_type) if file else None,
    )


class Relay:
    def __init__(self, client: TelegramClient, target: str, timeout: float, settle: float,
                 edit_wait: float = 0):
        self.client = client
        self.target = target
        self.timeout = timeout  # max seconds to wait for the first answer
        self.settle = settle  # an answer/edit is complete once the target bot is quiet this long
        self.edit_wait = edit_wait  # after answering, keep watching for edits this long
        # Only one request may be waiting for its *first* answer at a time, so a new
        # message from the target bot can only belong to that request.
        self._lock = asyncio.Lock()
        self._entity = None

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    async def ask(self, text: str, deliver: Deliver) -> bool:
        """Send `text` to the target bot and pass its answer to `deliver`.

        1. While holding the lock, send the message and collect the target bot's
           reply messages until it is quiet for `settle` seconds. Holding the lock
           here means every new message belongs to this request.
        2. Release the lock (the next user's request can start right away) and
           deliver the answer.
        3. For `edit_wait` seconds, watch for edits to *this answer's message IDs
           only* and deliver each edited version. Other requests' messages have
           other IDs, so edits can never reach the wrong user.

        Returns False if the target bot did not answer within `timeout`.
        """
        replies: dict[int, Message] = {}
        changed: set[int] = set()  # ids updated since the last delivery
        activity = asyncio.Event()
        # Which of the target bot's messages this request listens to: before the
        # answer, anything newer than our own message; after it, only its own IDs.
        scope: dict[str, object] = {"after": None, "own": None}

        async def on_update(event):
            mid = event.message.id
            own = scope["own"]
            if own is not None:
                if mid not in own:
                    return  # another request's message
            elif scope["after"] is not None and mid <= scope["after"]:
                return  # older than our request
            replies[mid] = event.message
            changed.add(mid)
            activity.set()

        loop = asyncio.get_running_loop()

        async def wait_activity(limit: float) -> bool:
            if limit <= 0:
                return False
            try:
                await asyncio.wait_for(activity.wait(), limit)
                return True
            except asyncio.TimeoutError:
                return False
            finally:
                activity.clear()

        async def settle_down() -> None:
            while await wait_activity(self.settle):
                pass

        handlers = []
        try:
            # 1. One request at a time until its first answer is in.
            async with self._lock:
                if self._entity is None:
                    self._entity = await self.client.get_input_entity(self.target)
                handlers = [
                    events.NewMessage(chats=self._entity, incoming=True),
                    events.MessageEdited(chats=self._entity, incoming=True),
                ]
                for ev in handlers:
                    self.client.add_event_handler(on_update, ev)
                sent = await self.client.send_message(self._entity, text)
                scope["after"] = sent.id

                def mine() -> list[int]:
                    return sorted(i for i in replies if i > sent.id)

                deadline = loop.time() + self.timeout
                while not mine() and await wait_activity(deadline - loop.time()):
                    pass
                if not mine():
                    return False
                await settle_down()
                own = mine()
                scope["own"] = set(own)  # from now on: only edits to these messages
                changed.clear()

            # 2. Lock released: the next request is already free to go.
            delivered = {i: _content(replies[i]) for i in own}
            await deliver([replies[i] for i in own], False)

            # 3. Follow edits to this answer's messages, by message ID.
            edit_deadline = loop.time() + self.edit_wait
            while await wait_activity(edit_deadline - loop.time()) or changed:
                await settle_down()
                updated = [i for i in sorted(changed) if delivered.get(i) != _content(replies[i])]
                changed.clear()
                if updated:
                    delivered.update({i: _content(replies[i]) for i in updated})
                    await deliver([replies[i] for i in updated], True)
            return True
        finally:
            for ev in handlers:
                self.client.remove_event_handler(on_update, ev)
