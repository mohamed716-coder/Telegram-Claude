"""Offline test harness: a fake Telegram Bot API server plus helpers to drive the bot.

Nothing talks to the real Telegram. ``FakeTelegram`` records every Bot API call the bot
makes and answers with plausible results; ``ShopTest`` feeds updates (messages and
button presses) into the real Application, exactly like Telegram would.

Typical use inside a test::

    async def scenario(t: ShopTest):
        calls = await t.send(OWNER, "/start")
        assert "Welcome Owner" in t.last_text(calls)
        calls = await t.press(OWNER, t.button(calls, "Add Label"))

    def test_something(tmp_path):
        run(tmp_path, scenario)
"""

from __future__ import annotations

import asyncio
import itertools
import json
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from telegram import Update
from telegram.request import BaseRequest

from shopbot.app import build_application
from shopbot.config import Settings
from shopbot.handlers import broadcast as broadcast_module
from shopbot.handlers import shop as shop_module
from shopbot.handlers import stock as stock_module

OWNER_ID = 6883493877
BOT_USER = {
    "id": 7000000001,
    "is_bot": True,
    "first_name": "Test Shop",
    "username": "test_shop_bot",
    "can_join_groups": True,
    "can_read_all_group_messages": False,
    "supports_inline_queries": False,
}

MESSAGE_METHODS = {
    "sendMessage",
    "sendPhoto",
    "sendDocument",
    "sendVideo",
    "sendAnimation",
    "sendAudio",
    "sendVoice",
    "sendSticker",
    "sendVideoNote",
    "editMessageText",
    "editMessageCaption",
}


class Call(dict):
    """One recorded Bot API call: {'method': ..., 'params': {...}, 'files': {...}}."""

    @property
    def method(self) -> str:
        return self["method"]

    @property
    def params(self) -> dict:
        return self["params"]

    @property
    def text(self) -> Optional[str]:
        return self.params.get("text", self.params.get("caption"))

    @property
    def chat_id(self) -> Optional[int]:
        chat = self.params.get("chat_id")
        return int(chat) if chat is not None else None

    @property
    def buttons(self) -> list[dict]:
        markup = self.params.get("reply_markup") or {}
        if isinstance(markup, str):
            markup = json.loads(markup)
        return [b for row in markup.get("inline_keyboard", []) for b in row]


class FakeTelegram(BaseRequest):
    """Stands in for api.telegram.org."""

    def __init__(self) -> None:
        self.calls: list[Call] = []
        self._message_ids = itertools.count(10_000)
        # method name -> error description; makes that method fail (HTTP 400)
        self.fail: dict[str, str] = {}

    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    @property
    def read_timeout(self) -> float:
        return 5.0

    async def do_request(self, url, method, request_data=None, read_timeout=None,
                         write_timeout=None, connect_timeout=None, pool_timeout=None):
        api_method = url.rsplit("/", 1)[-1]
        params: dict[str, Any] = {}
        files: dict[str, Any] = {}
        if request_data is not None:
            params = dict(request_data.parameters)
            if request_data.contains_files:
                for name, value in (request_data.multipart_data or {}).items():
                    if isinstance(value, tuple) and len(value) >= 2:
                        files[name] = {"filename": value[0], "content": value[1]}
        call = Call(method=api_method, params=params, files=files)
        self.calls.append(call)
        if api_method in self.fail:
            body = {"ok": False, "error_code": 400, "description": self.fail[api_method]}
            return 400, json.dumps(body).encode()
        return 200, json.dumps({"ok": True, "result": self._result(api_method, params)}).encode()

    # ------------------------------------------------------------------ results
    def _message(self, params: dict, **extra: Any) -> dict:
        chat_id = int(params.get("chat_id", 0) or 0)
        message = {
            "message_id": int(params.get("message_id") or next(self._message_ids)),
            "date": int(time.time()),
            "chat": {"id": chat_id, "type": "private", "first_name": "Chat"},
            "from": BOT_USER,
        }
        message.update({k: v for k, v in extra.items() if v is not None})
        if params.get("reply_markup"):
            markup = params["reply_markup"]
            message["reply_markup"] = json.loads(markup) if isinstance(markup, str) else markup
        return message

    @staticmethod
    def _file(params: dict, key: str, **extra: Any) -> dict:
        value = params.get(key)
        file_id = value if isinstance(value, str) and not value.startswith("attach://") else f"uploaded-{key}"
        return {"file_id": file_id, "file_unique_id": f"u-{file_id}", **extra}

    def _result(self, method: str, params: dict) -> Any:
        if method == "getMe":
            return BOT_USER
        caption = params.get("caption")
        if method == "sendMessage":
            return self._message(params, text=params.get("text", ""))
        if method == "sendPhoto":
            return self._message(params, caption=caption,
                                 photo=[self._file(params, "photo", width=100, height=100)])
        if method == "sendDocument":
            return self._message(params, caption=caption, document=self._file(params, "document"))
        if method == "sendVideo":
            return self._message(params, caption=caption,
                                 video=self._file(params, "video", width=1, height=1, duration=1))
        if method == "sendAnimation":
            return self._message(params, caption=caption,
                                 animation=self._file(params, "animation", width=1, height=1, duration=1))
        if method == "sendAudio":
            return self._message(params, caption=caption, audio=self._file(params, "audio", duration=1))
        if method == "sendVoice":
            return self._message(params, caption=caption, voice=self._file(params, "voice", duration=1))
        if method == "sendSticker":
            return self._message(params, sticker=self._file(
                params, "sticker", type="regular", width=1, height=1, is_animated=False, is_video=False))
        if method == "sendVideoNote":
            return self._message(params, video_note=self._file(params, "video_note", length=1, duration=1))
        if method == "editMessageText":
            return self._message(params, text=params.get("text", ""))
        if method == "editMessageCaption":
            return self._message(params, caption=caption,
                                 photo=[{"file_id": "p", "file_unique_id": "p", "width": 1, "height": 1}])
        return True


def tg_user(user_id: int, first_name: str = "User", username: Optional[str] = None,
            last_name: Optional[str] = None) -> dict:
    user = {"id": user_id, "is_bot": False, "first_name": first_name}
    if username:
        user["username"] = username
    if last_name:
        user["last_name"] = last_name
    return user


OWNER = tg_user(OWNER_ID, "Mohamed", "the_owner")


class ShopTest:
    """Drives the real Application with fake updates."""

    def __init__(self, tmp_path: Path, **settings: Any) -> None:
        self.tg = FakeTelegram()
        options = {"bot_token": "123456:TEST-TOKEN", "owner_id": OWNER_ID,
                   "db_path": str(Path(tmp_path) / "shop.db")}
        options.update(settings)
        self.settings = Settings(**options)
        self.app = build_application(self.settings, request=self.tg, rate_limit=False)
        self.db = self.app.bot_data["db"]
        self._update_ids = itertools.count(1)
        self._incoming_ids = itertools.count(1)
        stock_module.STATUS_DELAY = 0  # post the "Received N" status immediately
        shop_module.DELIVERY_INTERVAL = 0  # deliver products without pacing
        broadcast_module.SEND_INTERVAL = 0  # announcements and stock alerts without pacing
        broadcast_module.NEW_STOCK_DELAY = 0  # the stock alert goes out right away

    async def __aenter__(self) -> "ShopTest":
        await self.app.initialize()
        await self.app.start()  # no polling: updates are fed in by send()/press()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.settle()
        await self.app.stop()
        await self.app.shutdown()
        self.db.close()

    # ----------------------------------------------------------------- updates
    async def _process(self, data: dict) -> list[Call]:
        mark = len(self.tg.calls)
        data = {"update_id": next(self._update_ids), **data}
        await self.app.process_update(Update.de_json(data, self.app.bot))
        await self.settle()
        return self.tg.calls[mark:]

    async def settle(self) -> None:
        """Let background tasks (deliveries, the add-product status message) finish."""
        for _ in range(3):
            await asyncio.sleep(0.005)
        for _ in range(100):
            busy = [task for task in asyncio.all_tasks()
                    if task.get_name().startswith("shopbot:") and not task.done()]
            if not busy:
                break
            await asyncio.wait(busy, timeout=1)
        else:
            raise AssertionError(f"background tasks did not finish: {busy}")
        for _ in range(2):
            await asyncio.sleep(0.005)

    def _chat(self, user: dict) -> dict:
        return {"id": user["id"], "type": "private", "first_name": user.get("first_name", "")}

    async def send(self, user: dict, text: Optional[str] = None, **message: Any) -> list[Call]:
        """Send a message from ``user``. Extra fields go straight into the Message dict,
        e.g. photo=[...], caption="...", document={...}, entities=[...]."""
        msg: dict[str, Any] = {
            "message_id": next(self._incoming_ids),
            "date": int(time.time()),
            "chat": self._chat(user),
            "from": user,
        }
        if text is not None:
            msg["text"] = text
            if text.startswith("/"):
                command = text.split()[0]
                msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(command)}]
        msg.update(message)
        return await self._process({"message": msg})

    async def send_photo(self, user: dict, file_id: str = "receipt-photo", caption: Optional[str] = None) -> list[Call]:
        photo = [
            {"file_id": f"{file_id}-small", "file_unique_id": f"{file_id}-s", "width": 90, "height": 90},
            {"file_id": file_id, "file_unique_id": f"{file_id}-l", "width": 800, "height": 800},
        ]
        extra: dict[str, Any] = {"photo": photo}
        if caption:
            extra["caption"] = caption
        return await self.send(user, **extra)

    async def press(self, user: dict, data: str, *, message_id: int = 1,
                    message_text: Optional[str] = "message", photo: bool = False) -> list[Call]:
        """Press an inline button with ``data`` on a bot message."""
        message: dict[str, Any] = {
            "message_id": message_id,
            "date": int(time.time()),
            "chat": self._chat(user),
            "from": BOT_USER,
        }
        if photo:
            message["photo"] = [{"file_id": "p", "file_unique_id": "p", "width": 1, "height": 1}]
            message["caption"] = message_text or ""
        elif message_text is not None:
            message["text"] = message_text
        query = {
            "id": str(next(self._update_ids) + 100_000),
            "from": user,
            "chat_instance": f"ci-{user['id']}",
            "data": data,
            "message": message,
        }
        return await self._process({"callback_query": query})

    # ---------------------------------------------------------------- queries
    @staticmethod
    def messages(calls: list[Call], chat_id: Optional[int] = None) -> list[Call]:
        """Calls that send or edit a message (optionally only in one chat)."""
        return [c for c in calls if c.method in MESSAGE_METHODS
                and (chat_id is None or c.chat_id == chat_id)]

    @classmethod
    def texts(cls, calls: list[Call], chat_id: Optional[int] = None) -> list[str]:
        return [c.text or "" for c in cls.messages(calls, chat_id)]

    @classmethod
    def last_text(cls, calls: list[Call], chat_id: Optional[int] = None) -> str:
        texts = cls.texts(calls, chat_id)
        assert texts, f"no message was sent/edited; calls: {[c.method for c in calls]}"
        return texts[-1]

    @staticmethod
    def all_buttons(calls: list[Call]) -> list[dict]:
        return [b for c in calls for b in c.buttons]

    @classmethod
    def button(cls, calls: list[Call], label: str) -> str:
        """callback_data of the last button whose text contains ``label``."""
        matches = [b for b in cls.all_buttons(calls) if label in b.get("text", "")]
        assert matches, f"no button containing {label!r}; buttons: {[b.get('text') for b in cls.all_buttons(calls)]}"
        return matches[-1]["callback_data"]

    @classmethod
    def has_button(cls, calls: list[Call], label: str) -> bool:
        return any(label in b.get("text", "") for b in cls.all_buttons(calls))

    @staticmethod
    def answers(calls: list[Call]) -> list[dict]:
        """answerCallbackQuery calls: [{'text': ..., 'show_alert': ...}]"""
        return [c.params for c in calls if c.method == "answerCallbackQuery"]

    @staticmethod
    def alert(calls: list[Call]) -> str:
        texts = [c.params.get("text", "") for c in calls if c.method == "answerCallbackQuery"]
        return texts[-1] if texts else ""

    @staticmethod
    def documents(calls: list[Call]) -> list[Call]:
        return [c for c in calls if c.method == "sendDocument"]

    @staticmethod
    def document_text(call: Call) -> str:
        """Decoded contents of an uploaded document (e.g. a CSV)."""
        files = call["files"]
        assert files, f"no uploaded file in {call.method}"
        content = next(iter(files.values()))["content"]
        return content.decode("utf-8-sig") if isinstance(content, bytes) else str(content)


def run(tmp_path: Path, scenario: Callable[[ShopTest], Awaitable[None]], **settings: Any) -> None:
    """Run an async scenario against a fresh bot + database."""

    async def main() -> None:
        async with ShopTest(tmp_path, **settings) as t:
            await scenario(t)

    asyncio.run(main())
