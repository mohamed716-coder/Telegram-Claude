# Running the bot 24/7 on your VPS

The setup script installs everything, asks for your keys, logs in your Telegram
account and starts the bot. Docker then keeps it running: it restarts the bot
after a crash and starts it again when the server reboots.

Works on **Ubuntu or Debian** servers. Total time: about 15 minutes.

---

## Before you start: collect 3 things

1. **Bot token** – in Telegram open [@BotFather](https://t.me/BotFather), send
   `/newbot`, pick a name and a username ending in `bot`. Copy the token it
   sends you (looks like `123456789:AAH...`).
2. **API_ID and API_HASH** – open <https://my.telegram.org>, log in with your
   phone number, click **API development tools**, fill in the form (any name,
   platform "Other"), and copy **App api_id** (a number) and **App api_hash**
   (32 letters/digits).
3. **A GitHub token** (because this repository is private) – on GitHub go to
   **Settings → Developer settings → Personal access tokens → Fine-grained tokens →
   Generate new token**. Under *Repository access* pick **Only select repositories →
   Telegram-Claude**, under *Permissions* set **Contents: Read-only**, and
   generate it. Copy the token (starts with `github_pat_`).

## Step 1 – Connect to your VPS

On Windows open **PowerShell**, on Mac open **Terminal**:

```bash
ssh root@YOUR_SERVER_IP
```

(If your provider gave you a different username, use it instead of `root`.)

## Step 2 – Download the bot

```bash
apt-get update && apt-get install -y git
git clone -b claude/telegram-bot-dynamic-command-pyuptf https://github.com/mohamed716-coder/Telegram-Claude.git
cd Telegram-Claude
```

When git asks:
- **Username:** your GitHub username
- **Password:** paste the GitHub token (it stays invisible; just paste and press Enter)

## Step 3 – Run the setup

```bash
bash install.sh
```

It will:
1. Install Docker (first time only).
2. Ask for **API_ID**, **API_HASH** and **BOT_TOKEN**. Paste each one and press Enter.
3. Build the bot (a minute or two).
4. Log in with **your Telegram account**:
   - enter your phone number with country code, e.g. `+201001234567`
   - enter the code Telegram sends to your Telegram app
   - if you use two-step verification, enter that password
5. Start the bot and print `Done!`.

## Step 4 – Try it

Open your bot in Telegram and press **Start**. You are the admin, so you'll see
the admin commands. Try:

- `/kd hello` – goes to @KageNoKoroshiBot from your account; the answer comes back
- `/code 25 0` – makes a code; another user activates it with `/redeem <code>`

That's it. You can close the terminal; the bot keeps running.

---

## Everyday commands

First connect and go into the folder: `ssh root@YOUR_SERVER_IP`, then `cd Telegram-Claude`.

| What | Command |
|---|---|
| Is it running? | `docker compose ps` |
| Watch the logs (Ctrl+C to stop watching) | `docker compose logs -f --tail 100` |
| Restart | `docker compose restart` |
| Stop | `docker compose down` |
| Start again | `docker compose up -d` |
| Change settings | `nano .env`, then `docker compose up -d --force-recreate` |
| **Update to the newest version** | `git pull && bash install.sh` |
| Log in with your account again | `bash install.sh --relogin` |
| Back up codes and users | `docker compose cp kdbot:/app/data/kdbot.db ./kdbot-backup.db` |

## If something goes wrong

- **`git clone` says "Authentication failed"**: the token is wrong or doesn't
  have access to Telegram-Claude. Make a new one (see "Before you start" step 3).
- **Logs say "Your Telegram login is invalid or expired"**: you were logged out
  (for example you ended the session in Telegram → Settings → Devices). Run
  `bash install.sh --relogin`.
- **Logs mention `BOT_TOKEN` / "token invalid"**: fix it with `nano .env`, then
  `docker compose up -d --force-recreate`.
- **The bot doesn't answer at all**: run `docker compose logs --tail 50` and
  send me what it shows.

## Important

- **Run the bot in only one place.** Don't also run it on your PC: using the
  same Telegram login from two places at once can get it logged out, and both
  copies would answer every message.
- **Keep your login private.** `.env` and the bot's data folder give access to
  your Telegram account and your bot. Never share them, and never share the
  login code Telegram sends you.
- **Use anti-spam on codes you give out** (e.g. `/code 30 20`) so your account
  never sends too many messages too fast.
