"""Sends a message to the target bot from your account and collects its answer."""

import asyncio

from telethon import TelegramClient, events
from telethon.tl.custom import Message


class Relay:
    def __init__(self, client: TelegramClient, target: str, timeout: float, settle: float):
        self.client = client
        self.target = target
        self.timeout = timeout  # max seconds to wait for the whole answer
        self.settle = settle  # stop once the target bot is quiet this long
        self._lock = asyncio.Lock()  # one request at a time, so answers never get mixed up
        self._entity = None

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    async def ask(self, text: str) -> list[Message]:
        """Send `text` to the target bot; return its reply messages (empty on timeout).

        Bots often answer with several messages, or send "please wait" and edit it,
        so we keep listening until the bot has been quiet for `settle` seconds and
        return the latest version of every message it sent.
        """
        async with self._lock:
            if self._entity is None:
                self._entity = await self.client.get_input_entity(self.target)

            replies: dict[int, Message] = {}
            activity = asyncio.Event()

            async def on_update(event):
                replies[event.message.id] = event.message
                activity.set()

            new_ev = events.NewMessage(chats=self._entity, incoming=True)
            edit_ev = events.MessageEdited(chats=self._entity, incoming=True)
            self.client.add_event_handler(on_update, new_ev)
            self.client.add_event_handler(on_update, edit_ev)
            try:
                sent = await self.client.send_message(self._entity, text)
                loop = asyncio.get_running_loop()
                deadline = loop.time() + self.timeout

                async def wait_activity(limit: float) -> bool:
                    limit = min(limit, deadline - loop.time())
                    if limit <= 0:
                        return False
                    try:
                        await asyncio.wait_for(activity.wait(), limit)
                        return True
                    except asyncio.TimeoutError:
                        return False

                def answer() -> list[Message]:
                    return [replies[i] for i in sorted(replies) if i > sent.id]

                # Wait for the first reply, then until the bot goes quiet.
                while not answer() and await wait_activity(self.timeout):
                    activity.clear()
                if answer():
                    activity.clear()
                    while await wait_activity(self.settle):
                        activity.clear()
                return answer()
            finally:
                self.client.remove_event_handler(on_update, new_ev)
                self.client.remove_event_handler(on_update, edit_ev)
