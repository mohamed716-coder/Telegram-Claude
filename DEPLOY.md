# Running the bot 24/7

The bot has to run on a computer that is always on and online. The easiest way
is a small rented Linux server (a "VPS"). Docker keeps the bot running: it
restarts the bot if it crashes and starts it again when the server reboots.

Total time: about 20–30 minutes the first time.

---

## Step 1 – Create your bot (2 min)

1. In Telegram, open [@BotFather](https://t.me/BotFather) and send `/newbot`.
2. Choose a name and a username (must end in `bot`).
3. BotFather sends you a **token** like `123456789:AAH...`. Save it; this is your `BOT_TOKEN`.

## Step 2 – Get API keys for your account (3 min)

1. Open <https://my.telegram.org> and log in with your phone number
   (the code arrives in your Telegram app).
2. Click **API development tools**.
3. Fill in the form (any app name and short name, platform "Other") and click **Create application**.
4. Save **App api_id** (a number) and **App api_hash** (a long text). These are
   your `API_ID` and `API_HASH`.

## Step 3 – Rent a server (5–10 min)

Rent the smallest Linux VPS from any provider (Hetzner, DigitalOcean, Vultr, Contabo,
Oracle Cloud, …). The bot needs very little:

- **System:** Ubuntu 24.04
- **Size:** 1 CPU, 1 GB RAM is plenty

After creating it you get an **IP address** and a **root password** (or you
upload an SSH key during setup).

## Step 4 – Connect to the server

On Windows open **PowerShell**, on Mac/Linux open **Terminal**, and run
(replace with your server's IP):

```bash
ssh root@YOUR_SERVER_IP
```

Type `yes` if asked, then the password (it stays invisible while you type).
Every command from here on is typed **on the server**.

## Step 5 – Install Docker and git

```bash
curl -fsSL https://get.docker.com | sh
apt-get install -y git
```

## Step 6 – Download the bot

```bash
git clone -b claude/telegram-bot-dynamic-command-pyuptf https://github.com/mohamed716-coder/Telegram-Claude.git
cd Telegram-Claude
```

If the repository is private, git asks for a username and password: enter your
GitHub username, and as the password a **personal access token** (GitHub →
Settings → Developer settings → Personal access tokens → Generate new token,
with read access to this repository).

## Step 7 – Fill in your settings

```bash
cp .env.example .env
nano .env
```

Fill in `API_ID`, `API_HASH` and `BOT_TOKEN` from steps 1 and 2. Leave
`SESSION_STRING` empty for now. Save with **Ctrl+O**, **Enter**, then exit with **Ctrl+X**.

## Step 8 – Build the bot

```bash
docker compose build
```

## Step 9 – Log in with your Telegram account (once)

```bash
docker compose run --rm kdbot python login.py
```

- Enter your phone number with country code, e.g. `+201001234567`.
- Enter the code Telegram sends to your Telegram app.
- If you have two-step verification, enter that password too.

It prints a long text (your session string). Copy all of it, then:

```bash
nano .env
```

and paste it after `SESSION_STRING=` (right-click pastes in most terminals).
Save and exit (**Ctrl+O**, **Enter**, **Ctrl+X**).

> ⚠️ The session string is full access to your Telegram account. Never send it
> to anyone, and never share the login code Telegram sends you.

## Step 10 – Start it

```bash
docker compose up -d
docker compose logs -f
```

You should see `Running. Relaying as <your username> to @KageNoKoroshiBot`.
Press **Ctrl+C** to stop watching the logs; the bot keeps running.

Now open your bot in Telegram and press **Start**. Because you logged in with
your account in step 9, you are the admin: you'll see the admin commands, and
you can try `/kd hello` right away. Generate a code for someone with
`/code 25 0` and they activate it with `/redeem <code>`.

You can close the terminal: the bot stays up 24/7, including after server reboots.

---

## Everyday commands

Run these inside the `Telegram-Claude` folder on the server
(`ssh root@YOUR_SERVER_IP`, then `cd Telegram-Claude`):

| What | Command |
|---|---|
| See if it's running | `docker compose ps` |
| Watch the logs | `docker compose logs -f --tail 100` |
| Restart | `docker compose restart` |
| Stop | `docker compose down` |
| Start again | `docker compose up -d` |
| After changing `.env` | `docker compose up -d --force-recreate` |
| Update to the newest code | `git pull && docker compose up -d --build` |

## Important

- **Run the bot in only one place.** Don't run it on your PC while it's running
  on the server: using the same session from two places at once can make
  Telegram log the session out, and both copies would answer every message.
- **If your account gets logged out** (for example you ended the session in
  Telegram → Settings → Devices), the logs say `SESSION_STRING is invalid or expired`.
  Repeat step 9, then `docker compose up -d --force-recreate`.
- **Use anti-spam on codes you give out** (e.g. `/code 30 20`) so your account
  never sends too many messages too fast.
- **Back up your codes and users** now and then:
  `docker compose cp kdbot:/app/data/kdbot.db ./kdbot-backup.db`
