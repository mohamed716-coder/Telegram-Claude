# Telegram-Claude

A Telegram bot that relays `/kd`, `/zz` and `/ko` messages through your own account
to [@KageNoKoroshiBot](https://t.me/KageNoKoroshiBot):

```
User ──/kd anything──▶ @YourBot ──▶ your account sends "/kd anything" to @KageNoKoroshiBot
User ◀── @YourBot sends back @KageNoKoroshiBot's answer ◀──────────────────┘
```

The message is passed on exactly as the user typed it (command and text).

## Plans and codes

Only **Premium** users (and admins) can use `/kd`, `/ko` and `/zz`. Premium is
activated with single-use codes that admins generate.

| Command | Who | What it does |
|---|---|---|
| `/start` | everyone | Welcome screen with your plan and the commands |
| `/status` | everyone | Your plan, Premium time left and expiry date |
| `/redeem <code>` | everyone | Activate a code (days are added on top of any time you still have) |
| `/kd` `/ko` `/zz` `<message>` | Premium | Sent to @KageNoKoroshiBot; its answer comes back to you |
| `/code <days> <antispam_seconds> [count]` | admin | Generate code(s), e.g. `/code 25 0` → `ANONYMOUS-81ja5j9H5R4` |
| `/codes` | admin | List unused codes |
| `/delcode <code>` | admin | Delete an unused code |
| `/users` | admin | List Premium users with time left |
| `/removepremium <user_id>` | admin | Take Premium away from a user |

- **Admins**: the Telegram account you logged in with is always admin;
  add more with `ADMIN_IDS`. Admins have no limits.
- **Anti-spam**: the seconds from the code a user redeemed is how long they must
  wait between requests (`0` = no wait). Redeeming a newer code replaces it.
- **Notifications**: admins get a message whenever someone redeems a code
  (open your bot and press Start once so it's allowed to message you).
- Codes and users are stored in `data/kdbot.db` (a Docker volume on the server),
  so they survive restarts and updates.

Anyone can use your bot, but the target bot only ever sees **your account**.
The answer (text, formatting, photos, files) is copied back to whoever asked.
Multi-message answers and "please wait…" messages that the target bot later
edits are handled: the bot waits until the target goes quiet and sends the answer.
If the target bot edits its answer later (within `EDIT_WAIT` seconds), the user's
message is edited too, and ends with a footer:

```

USER: @their_username   (or Unknown)
Status: Premium
Owner: @anony6916
```

## What users see (`/setresponse…`)

Admins can replace the target bot's message with their own text, separately for each
command (`/kd`, `/zz`, `/ko`). Write the text on the lines below the command, in the
same message:

```
/setresponseaft
hey, thanks for using our service
<line_6>
have a good day
```

The bot then asks which command it's for, with buttons: **/kd**, **/zz**, **/ko** or
**All commands**. Tap one to save it.

| Command | Used for |
|---|---|
| `/setresponsebef` | The target bot's first answer |
| `/setresponseaft` | After the target bot edits its answer (footer added) |
| `/setresponse0bef` | First answer, when line 6 is `• PROCESSED: 0 + 10 / 10` |
| `/setresponse0aft` | After the edit, when line 6 is `• PROCESSED: 0 + 10 / 10` (footer added) |

- `<line_4>`, `<line_6>` (any number) are replaced by that line of the target bot's message;
  nothing else from it is shown. A line that doesn't exist becomes empty.
- The text is shown exactly as written. It is saved in `data/kdbot.db`, so it survives restarts.
- Send the command with no text (then tap a command) to reset it: the target bot's
  message is then shown as it is for that command.

## Run it 24/7 on a server

See **[DEPLOY.md](DEPLOY.md)**: on an Ubuntu/Debian server it's `git clone …` then `bash install.sh`.

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
   The login is saved in `data/user_session.txt`, where the bot finds it.
5. **Run**:
   ```bash
   .venv/bin/python -m kdbot.main
   ```

## Settings (`.env`)

| Setting | Meaning |
|---|---|
| `TARGET_BOT` | Bot your account messages (default `@KageNoKoroshiBot`) |
| `COMMANDS` | Commands passed through, comma-separated (default `kd,zz,ko`) |
| `ADMIN_IDS` | Extra admin user IDs, comma-separated (your own account is always admin) |
| `CODE_PREFIX` | Start of generated codes (default `ANONYMOUS`) |
| `REPLY_TIMEOUT` | Max seconds to wait for the target bot (default 30) |
| `SETTLE_SECONDS` | Answer counts as finished after this many quiet seconds (default 3) |
| `EDIT_WAIT` | After answering, keep watching this many seconds for the target bot to edit its answer and update the user's message too (default 20, 0 = off) |

Several users can use the bot at once. Requests only queue ("In queue…") while an
earlier one waits for the target bot's *first* reply, so a new message can only
belong to one request. After that, the next request goes ahead, and edits are
matched to the right user by message ID, so answers never get mixed up.

## Safety

- Your saved login (`data/user_session.txt`, or `SESSION_STRING` if you set it) gives
  **full access to your Telegram account**. Never share it; `.env` and `data/` are in `.gitignore`.
- Keep the volume low: many automated messages from a personal account can get
  it limited by Telegram. Use a non-zero anti-spam value on codes you give out.

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```
