import asyncio
from types import SimpleNamespace

from kdbot.relay import Relay


class FakeClient:
    """Pretends to be your account; `script` decides how the target bot answers."""

    def __init__(self, script):
        self.script = script
        self.handlers = []
        self.sent = []
        self.next_id = 100

    async def get_input_entity(self, target):
        return target

    def add_event_handler(self, cb, ev):
        self.handlers.append(cb)

    def remove_event_handler(self, cb, ev):
        self.handlers.remove(cb)

    async def send_message(self, entity, text):
        self.next_id += 1
        msg = SimpleNamespace(id=self.next_id, text=text)
        self.sent.append(text)
        asyncio.get_running_loop().create_task(self.script(self, msg))
        return msg

    async def emit(self, msg_id, text):
        for cb in list(self.handlers):
            await cb(SimpleNamespace(message=SimpleNamespace(id=msg_id, text=text)))


async def test_single_reply():
    async def script(c, sent):
        await asyncio.sleep(0.05)
        await c.emit(sent.id + 1, "answer for " + sent.text)

    relay = Relay(FakeClient(script), "@t", timeout=2, settle=0.1)
    replies = await relay.ask("/kd 1")
    assert [m.text for m in replies] == ["answer for /kd 1"]


async def test_multiple_messages_and_edits():
    async def script(c, sent):
        await c.emit(sent.id - 5, "old message edited - ignored")
        await asyncio.sleep(0.05)
        await c.emit(sent.id + 1, "please wait...")
        await asyncio.sleep(0.05)
        await c.emit(sent.id + 1, "result: OK")  # edit
        await c.emit(sent.id + 2, "second part")

    relay = Relay(FakeClient(script), "@t", timeout=2, settle=0.1)
    replies = await relay.ask("/kd 1")
    assert [m.text for m in replies] == ["result: OK", "second part"]


async def test_timeout_returns_empty():
    async def script(c, sent):
        pass

    client = FakeClient(script)
    relay = Relay(client, "@t", timeout=0.2, settle=0.1)
    assert await relay.ask("/kd 1") == []
    assert client.handlers == []  # cleaned up


async def test_requests_do_not_mix():
    async def script(c, sent):
        await asyncio.sleep(0.05)
        await c.emit(sent.id + 1, "answer for " + sent.text)

    relay = Relay(FakeClient(script), "@t", timeout=2, settle=0.1)
    a, b = await asyncio.gather(relay.ask("/kd A"), relay.ask("/kd B"))
    assert [m.text for m in a] == ["answer for /kd A"]
    assert [m.text for m in b] == ["answer for /kd B"]
