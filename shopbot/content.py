"""Store a Telegram message as a product and send it again exactly as it was sent.

The message is saved as JSON (text/caption, formatting entities and file id), so a
product can still be delivered after the seller deletes the original message.
"""

from __future__ import annotations

from typing import Any, Optional

from telegram import Bot, Message, MessageEntity
from telegram.ext import ExtBot

SUPPORTED_KINDS = (
    "text",
    "photo",
    "video",
    "animation",
    "document",
    "audio",
    "voice",
    "sticker",
    "video_note",
)


def _entities(entities: Any) -> list[dict]:
    return [entity.to_dict() for entity in entities or ()]


def extract_content(message: Message) -> Optional[dict]:
    """Turn a message into a JSON-serialisable product, or None if unsupported."""
    if message.text is not None:
        return {"kind": "text", "text": message.text, "entities": _entities(message.entities)}

    media: dict[str, Any] = {
        "caption": message.caption,
        "caption_entities": _entities(message.caption_entities),
    }
    if message.show_caption_above_media:
        media["show_caption_above_media"] = True
    if message.has_media_spoiler:
        media["has_spoiler"] = True

    if message.photo:
        return {"kind": "photo", "file_id": message.photo[-1].file_id, **media}
    # A GIF arrives with both .animation and .document set: check animation first.
    if message.animation:
        return {"kind": "animation", "file_id": message.animation.file_id, **media}
    if message.video:
        return {"kind": "video", "file_id": message.video.file_id, **media}
    if message.document:
        return {"kind": "document", "file_id": message.document.file_id, **media}
    if message.audio:
        return {"kind": "audio", "file_id": message.audio.file_id, **media}
    if message.voice:
        return {"kind": "voice", "file_id": message.voice.file_id, **media}
    if message.sticker:
        return {"kind": "sticker", "file_id": message.sticker.file_id}
    if message.video_note:
        return {"kind": "video_note", "file_id": message.video_note.file_id}
    return None


def _to_entities(raw: Any, bot: Bot) -> Optional[list[MessageEntity]]:
    entities = [MessageEntity.de_json(item, bot) for item in raw or ()]
    return [e for e in entities if e is not None] or None


async def send_content(
    bot: Bot | ExtBot, chat_id: int, content: dict, reply_markup: Any = None
) -> Message:
    """Send a stored product to ``chat_id`` exactly as the seller sent it."""
    kind = content.get("kind")
    if kind == "text":
        return await bot.send_message(
            chat_id,
            content["text"],
            entities=_to_entities(content.get("entities"), bot),
            reply_markup=reply_markup,
        )

    file_id = content["file_id"]
    if kind == "sticker":
        return await bot.send_sticker(chat_id, file_id, reply_markup=reply_markup)
    if kind == "video_note":
        return await bot.send_video_note(chat_id, file_id, reply_markup=reply_markup)

    caption = content.get("caption")
    caption_entities = _to_entities(content.get("caption_entities"), bot)
    common: dict[str, Any] = {
        "caption": caption,
        "caption_entities": caption_entities,
        "reply_markup": reply_markup,
    }
    above = content.get("show_caption_above_media") or None
    spoiler = content.get("has_spoiler") or None
    if kind == "photo":
        return await bot.send_photo(
            chat_id, file_id, has_spoiler=spoiler, show_caption_above_media=above, **common
        )
    if kind == "video":
        return await bot.send_video(
            chat_id, file_id, has_spoiler=spoiler, show_caption_above_media=above, **common
        )
    if kind == "animation":
        return await bot.send_animation(
            chat_id, file_id, has_spoiler=spoiler, show_caption_above_media=above, **common
        )
    if kind == "document":
        return await bot.send_document(chat_id, file_id, **common)
    if kind == "audio":
        return await bot.send_audio(chat_id, file_id, **common)
    if kind == "voice":
        return await bot.send_voice(chat_id, file_id, **common)
    raise ValueError(f"Unknown product kind: {kind!r}")


def describe_content(content: dict) -> str:
    """Short, human readable summary such as 'photo' or 'text: abc…'."""
    kind = content.get("kind", "?")
    if kind == "text":
        text = " ".join(str(content.get("text", "")).split())
        return f"text: {text[:30]}…" if len(text) > 30 else f"text: {text}"
    return kind.replace("_", " ")
