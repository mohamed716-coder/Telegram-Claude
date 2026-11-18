"""Telegram bot that chats with Claude.

Each Telegram chat keeps its own conversation history in memory.
Commands:
  /start  - greeting
  /reset  - forget the conversation in this chat
"""

import logging
import os
from collections import defaultdict

import anthropic
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")
SYSTEM_PROMPT = os.environ.get(
    "SYSTEM_PROMPT",
    "You are a friendly, helpful assistant chatting in Telegram. "
    "Keep replies concise and conversational. Use plain text; avoid Markdown tables.",
)
# Keep at most this many messages per chat so requests stay small.
MAX_HISTORY = int(os.environ.get("MAX_HISTORY", "40"))
# Optional comma-separated list of Telegram user IDs allowed to use the bot.
ALLOWED_USERS = {
    int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").split(",") if x.strip()
}
TELEGRAM_LIMIT = 4096

claude = anthropic.AsyncAnthropic()  # reads ANTHROPIC_API_KEY
histories: dict[int, list[dict]] = defaultdict(list)


def is_allowed(update: Update) -> bool:
    return not ALLOWED_USERS or (
        update.effective_user is not None and update.effective_user.id in ALLOWED_USERS
    )


def trim(history: list[dict]) -> None:
    """Drop the oldest messages, keeping the history starting on a user turn."""
    while len(history) > MAX_HISTORY:
        del history[:2]
    while history and history[0]["role"] != "user":
        del history[0]


async def ask_claude(history: list[dict]):
    return await claude.beta.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=history,
        output_config={"effort": "low"},
        # If the model declines on safety grounds, retry on a fallback model.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    await update.message.reply_text(
        "Hi! I'm a bot powered by Claude. Send me a message to chat.\n"
        "Use /reset to start a new conversation."
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return
    histories.pop(update.effective_chat.id, None)
    await update.message.reply_text("Conversation cleared.")


async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.message.reply_text("Sorry, you're not allowed to use this bot.")
        return

    chat_id = update.effective_chat.id
    history = histories[chat_id]
    history.append({"role": "user", "content": update.message.text})

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    try:
        response = await ask_claude(history)
    except anthropic.RateLimitError:
        history.pop()
        await update.message.reply_text("I'm being rate limited. Try again shortly.")
        return
    except anthropic.APIStatusError as e:
        history.pop()
        log.exception("Claude API error")
        await update.message.reply_text(f"Claude API error ({e.status_code}).")
        return
    except anthropic.APIConnectionError:
        history.pop()
        await update.message.reply_text("Couldn't reach Claude. Try again shortly.")
        return

    if response.stop_reason == "refusal":
        history.pop()
        await update.message.reply_text("Sorry, I can't help with that.")
        return

    # Store the full content (including thinking blocks) so it can be replayed.
    history.append({"role": "assistant", "content": response.content})
    trim(history)

    text = "".join(b.text for b in response.content if b.type == "text").strip()
    if not text:
        text = "(no reply)"
    for i in range(0, len(text), TELEGRAM_LIMIT):
        await update.message.reply_text(text[i : i + TELEGRAM_LIMIT])


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN (get one from @BotFather).")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        log.warning("ANTHROPIC_API_KEY is not set; Claude calls may fail.")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))

    log.info("Bot running with model %s", MODEL)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
