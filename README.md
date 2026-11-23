# Telegram-Claude

A Telegram bot that relays `/kd` requests through your own account:

```
User ──/kd xxxx──▶ @YourBot ──▶ your account sends "<template with xxxx>" to @TargetBot
User ◀── @YourBot sends back @TargetBot's answer ◀────────────────────┘
```

Anyone can use your bot, but the target bot only ever sees **your account**.
The answer (text, formatting, photos, files) is copied back to whoever asked.
Multi-message answers and "please wait…" messages that the target bot later
edits are handled: the bot waits until the target goes quiet and sends the final version.

## Setup

1. **Create your bot**: message [@BotFather](https://t.me/BotFather), `/newbot`, copy the token.
2. **Get API keys for your account**: log in at <https://my.telegram.org>, open
   *API development tools*, create an app, and copy `api_id` and `api_hash`.
3. **Install**:
   ```bash
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   cp .env.example .env      # then fill in API_ID, API_HASH, BOT_TOKEN, TARGET_BOT, MESSAGE_TEMPLATE
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
| `TARGET_BOT` | Username of the bot your account messages, e.g. `@some_bot` |
| `MESSAGE_TEMPLATE` | What your account sends; `{arg}` becomes the user's value. Default `/kd {arg}` |
| `ARG_PATTERN` | Which values are accepted after `/kd` (regex). Default: letters, digits, `_ . -`, up to 64 |
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
