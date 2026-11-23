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


async def ask(relay, text):
    """Run relay.ask and record every delivery as ("first"|"update", [texts])."""
    got = []

    async def deliver(msgs, is_update):
        got.append(("update" if is_update else "first", [m.text for m in msgs]))

    ok = await relay.ask(text, deliver)
    return ok, got


async def test_single_reply():
    async def script(c, sent):
        await asyncio.sleep(0.05)
        await c.emit(sent.id + 1, "answer for " + sent.text)

    relay = Relay(FakeClient(script), "@t", timeout=2, settle=0.1, edit_wait=0.2)
    assert await ask(relay, "/kd 1") == (True, [("first", ["answer for /kd 1"])])


async def test_multiple_messages_and_quick_edits():
    async def script(c, sent):
        await c.emit(sent.id - 5, "old message edited - ignored")
        await asyncio.sleep(0.05)
        await c.emit(sent.id + 1, "please wait...")
        await asyncio.sleep(0.05)
        await c.emit(sent.id + 1, "result: OK")  # edit before the answer settled
        await c.emit(sent.id + 2, "second part")

    relay = Relay(FakeClient(script), "@t", timeout=2, settle=0.1, edit_wait=0.2)
    assert await ask(relay, "/kd 1") == (True, [("first", ["result: OK", "second part"])])


async def test_late_edit_is_sent_as_update():
    async def script(c, sent):
        await asyncio.sleep(0.05)
        await c.emit(sent.id + 1, "please wait...")
        await asyncio.sleep(0.3)  # longer than settle: first version is sent already
        await c.emit(sent.id + 1, "final result")
        await asyncio.sleep(0.05)
        await c.emit(sent.id + 1, "final result")  # no visible change - not re-sent

    relay = Relay(FakeClient(script), "@t", timeout=2, settle=0.1, edit_wait=1)
    assert await ask(relay, "/kd 1") == (
        True, [("first", ["please wait..."]), ("update", ["final result"])]
    )


async def test_no_updates_when_edit_wait_off():
    async def script(c, sent):
        await asyncio.sleep(0.05)
        await c.emit(sent.id + 1, "please wait...")
        await asyncio.sleep(0.3)
        await c.emit(sent.id + 1, "final result")

    relay = Relay(FakeClient(script), "@t", timeout=2, settle=0.1, edit_wait=0)
    assert await ask(relay, "/kd 1") == (True, [("first", ["please wait..."])])


async def test_timeout_returns_false():
    async def script(c, sent):
        pass

    client = FakeClient(script)
    relay = Relay(client, "@t", timeout=0.2, settle=0.1, edit_wait=0.2)
    assert await ask(relay, "/kd 1") == (False, [])
    assert client.handlers == []  # cleaned up


async def test_requests_do_not_mix():
    async def script(c, sent):
        await asyncio.sleep(0.05)
        await c.emit(sent.id + 1, "answer for " + sent.text)

    relay = Relay(FakeClient(script), "@t", timeout=2, settle=0.1, edit_wait=0.2)
    a, b = await asyncio.gather(ask(relay, "/kd A"), ask(relay, "/kd B"))
    assert a == (True, [("first", ["answer for /kd A"])])
    assert b == (True, [("first", ["answer for /kd B"])])


async def test_late_edits_go_to_the_right_user():
    """Two users at once; the target bot edits each answer later. Each user only gets their own."""
    async def script(c, sent):
        await asyncio.sleep(0.05)
        await c.emit(sent.id + 1, "wait " + sent.text)
        await asyncio.sleep(0.3)
        await c.emit(sent.id + 1, "done " + sent.text)

    client = FakeClient(script)
    relay = Relay(client, "@t", timeout=2, settle=0.1, edit_wait=0.6)
    a, b = await asyncio.gather(ask(relay, "/kd A"), ask(relay, "/kd B"))
    assert a == (True, [("first", ["wait /kd A"]), ("update", ["done /kd A"])])
    assert b == (True, [("first", ["wait /kd B"]), ("update", ["done /kd B"])])
    assert client.sent == ["/kd A", "/kd B"]  # B only sent after A was finished


async def test_exits_when_either_client_disconnects():
    from kdbot.main import run_until_either_disconnects

    class Conn:
        def __init__(self, lifetime):
            self.lifetime = lifetime
            self.disconnected = False

        async def run_until_disconnected(self):
            await asyncio.sleep(self.lifetime)

        async def disconnect(self):
            self.disconnected = True

    short, forever = Conn(0.05), Conn(3600)
    await asyncio.wait_for(run_until_either_disconnects(short, forever), 2)
    assert short.disconnected and forever.disconnected
