# Telegram-Claude

A Telegram bot that relays `/kd`, `/zz` and `/ko` messages through your own account
to [@KageNoKoroshiBot](https://t.me/KageNoKoroshiBot):

```
User ──/kd anything──▶ @YourBot ──▶ your account sends "/kd anything" to @KageNoKoroshiBot
User ◀── @YourBot sends back @KageNoKoroshiBot's answer ◀──────────────────┘
```

The message is passed on exactly as the user typed it (command and text).

Anyone can use your bot, but the target bot only ever sees **your account**.
The answer (text, formatting, photos, files) is copied back to whoever asked.
Multi-message answers and "please wait…" messages that the target bot later
edits are handled: the bot waits until the target goes quiet and sends the final version.

## Run it 24/7 on a server

See **[DEPLOY.md](DEPLOY.md)** for step-by-step instructions (Docker on a small VPS).

## Run it on your own computer

1. **Create your bot**: message [@BotFather](https://t.me/BotFather), `/newbot`, copy the token.
2. **Get API keys for your account**: log in at <https://my.telegram.org>, open
   *API development tools*, create an app, and copy `api_id` and `api_hash`.
3. **Install**:
   ```bash
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   cp .env.example .env      # then fill in API_ID, API_HASH, BOT_TOKEN
   ```
4. **Log in with your account** (once, on your own computer; it asks for your phone and the code Telegram sends you):
   ```bash
   .venv/bin/python login.py
   ```
   Put the printed string in `.env` as `SESSION_STRING`.
5. **Run**:
   ```bash
   .venv/bin/python -m kdbot.main
   ```

## Settings (`.env`)

| Setting | Meaning |
|---|---|
| `TARGET_BOT` | Bot your account messages (default `@KageNoKoroshiBot`) |
| `COMMANDS` | Commands passed through, comma-separated (default `kd,zz,ko`) |
| `ALLOWED_USERS` | Comma-separated user IDs allowed to use the bot; empty means everyone |
| `COOLDOWN_SECONDS` | Wait time per user between requests (default 20) |
| `REPLY_TIMEOUT` | Max seconds to wait for the target bot (default 30) |
| `SETTLE_SECONDS` | Answer counts as finished after this many quiet seconds (default 3) |

Requests are handled one at a time, so two users' answers never get mixed up;
extra requests wait in a queue.

## Safety

- `SESSION_STRING` gives **full access to your Telegram account**. Never share it
  or commit it; `.env` is already in `.gitignore`.
- Keep the volume low and the cooldown on: many automated messages from a
  personal account can get it limited by Telegram.

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```
