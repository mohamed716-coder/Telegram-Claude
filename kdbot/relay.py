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
        self._lock = asyncio.Lock()  # one request at a time, so answers never get mixed up
        self._entity = None

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    async def ask(self, text: str, deliver: Deliver) -> bool:
        """Send `text` to the target bot and pass its answer to `deliver`.

        Bots often answer with several messages, or send "please wait" and edit
        it later. We collect messages until the bot is quiet for `settle` seconds
        and deliver them. Then, for `edit_wait` seconds, every edit (or extra
        message) is delivered again as an update.

        The lock is held the whole time, including the edit window, so the next
        user's request is only sent once this one is finished and nothing from the
        target bot can end up with the wrong user.

        Returns False if the target bot did not answer within `timeout`.
        """
        async with self._lock:
            if self._entity is None:
                self._entity = await self.client.get_input_entity(self.target)

            replies: dict[int, Message] = {}
            changed: set[int] = set()  # ids updated since the last delivery
            activity = asyncio.Event()

            async def on_update(event):
                replies[event.message.id] = event.message
                changed.add(event.message.id)
                activity.set()

            new_ev = events.NewMessage(chats=self._entity, incoming=True)
            edit_ev = events.MessageEdited(chats=self._entity, incoming=True)
            self.client.add_event_handler(on_update, new_ev)
            self.client.add_event_handler(on_update, edit_ev)
            try:
                sent = await self.client.send_message(self._entity, text)
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

                def mine(ids) -> list[int]:
                    # Only messages sent after our request belong to this user.
                    return sorted(i for i in ids if i > sent.id)

                async def settle_down() -> None:
                    while await wait_activity(self.settle):
                        pass

                # 1. Wait for the first reply, then until the bot goes quiet.
                deadline = loop.time() + self.timeout
                while not mine(replies) and await wait_activity(deadline - loop.time()):
                    pass
                if not mine(replies):
                    return False
                await settle_down()
                delivered = {i: _content(replies[i]) for i in mine(replies)}
                changed.clear()
                await deliver([replies[i] for i in delivered], False)

                # 2. Keep watching for edits / extra messages and send those too.
                edit_deadline = loop.time() + self.edit_wait
                while await wait_activity(edit_deadline - loop.time()) or mine(changed):
                    await settle_down()
                    updated = [i for i in mine(changed) if delivered.get(i) != _content(replies[i])]
                    changed.clear()
                    if updated:
                        delivered.update({i: _content(replies[i]) for i in updated})
                        await deliver([replies[i] for i in updated], True)
                return True
            finally:
                self.client.remove_event_handler(on_update, new_ev)
                self.client.remove_event_handler(on_update, edit_ev)
