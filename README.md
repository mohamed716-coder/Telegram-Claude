# Telegram-Claude

A Telegram bot that chats using Claude. Each chat keeps its own conversation history in memory.

## Setup

1. **Create a bot**: message [@BotFather](https://t.me/BotFather) on Telegram, send `/newbot`, and copy the token.
2. **Get a Claude API key** from https://platform.claude.com.
3. **Install and run**:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN=...   # from BotFather
export ANTHROPIC_API_KEY=...    # from the Claude Console
python bot.py
```

Then open your bot in Telegram and send it a message.

## Commands

- `/start`: show a greeting
- `/reset`: forget the conversation in this chat

## Options (environment variables)

| Variable | Default | Purpose |
|---|---|---|
| `CLAUDE_MODEL` | `claude-opus-5-5` | Which Claude model to use |
| `SYSTEM_PROMPT` | friendly assistant | The bot's personality and instructions |
| `ALLOWED_USER_IDS` | (anyone) | Comma-separated Telegram user IDs allowed to use the bot |
| `MAX_HISTORY` | `40` | Messages remembered per chat |

Set `ALLOWED_USER_IDS` so strangers can't run up your API bill. You can find your ID with [@userinfobot](https://t.me/userinfobot).

History is kept in memory only, so it is lost when the bot restarts.
