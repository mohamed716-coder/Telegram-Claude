# Deploying the shop bot (running it 24/7)

The bot has to run on a computer that is always on and online. Two things matter:

- **Keep its files.** Every balance, product and order is in one file, `data/shop.db`.
  Hosts whose disk is wiped on every restart (for example free "web service" plans) would
  lose your shop. The options below all keep it.
- **Run one copy only.** Two copies with the same token fight over messages
  (Telegram answers *409 Conflict*). Stop the bot on your PC once it runs on a server.

Choose one:

| | Good for | Cost |
|---|---|---|
| **A. A Linux server (VPS)** | Recommended. One command sets everything up. | A few $ a month, or a server you already have |
| **B. Docker** | If you already run things with Docker | Same as A |
| **C. Railway** | No server to look after, everything in the browser | About $5 a month (Hobby plan) |

---

## A. Linux server (Ubuntu or Debian)

You need: the server's **IP address** and **root password** (or a user that can use `sudo`),
and the **bot token** from [@BotFather](https://t.me/BotFather).

### 1. Log in to the server

- **Windows 10/11:** open *PowerShell* and type `ssh root@YOUR_SERVER_IP`.
- **Mac or Linux:** open *Terminal* and type the same.
- **Phone:** use an SSH app (for example Termius), or your provider's web console.

The first time, type `yes` to trust the server. When it asks for the password, type or
paste it: nothing appears while you type, that's normal. Press Enter.

To paste into the terminal: right-click (Windows), Cmd+V (Mac), or Ctrl+Shift+V (Linux).

### 2. Let the server download the code

The repository is private, so the server needs its own read-only key. Paste this:

```bash
apt-get update && apt-get install -y git
ssh-keygen -t ed25519 -N "" -C "shop-server" -f ~/.ssh/shopbot_deploy
cat ~/.ssh/shopbot_deploy.pub
```

It prints one line starting with `ssh-ed25519`. Copy that whole line, then on GitHub open
the repository → **Settings** → **Deploy keys** → **Add deploy key**
(direct link: <https://github.com/mohamed716-coder/Telegram-Claude/settings/keys/new>).
Title: `shop server`; Key: paste the line; leave **Allow write access** unticked; click
**Add key**.

### 3. Download the bot

```bash
cd ~
GIT_SSH_COMMAND="ssh -i ~/.ssh/shopbot_deploy -o StrictHostKeyChecking=accept-new" \
  git clone --branch main git@github.com:mohamed716-coder/Telegram-Claude.git
cd Telegram-Claude
git config core.sshCommand "ssh -i ~/.ssh/shopbot_deploy"
```

(If the code is on another branch, put its name instead of `main`.)

### 4. Install and start it

```bash
bash deploy/install.sh
```

Logged in as a normal user instead of root? Use `sudo bash deploy/install.sh`.

It installs what's needed, asks for the bot token (paste it, press Enter), checks it with
Telegram, and starts the bot as a service: it keeps running when you log out, starts
again when the server reboots, and restarts by itself if it ever crashes. It also makes a
backup of the database every night.

When it says **"The bot @yourbot is running"**, open the bot in Telegram and send `/start`.
You can now close the terminal.

### Everyday commands

Run these on the server, inside the bot folder (`cd ~/Telegram-Claude`). Add `sudo` in
front if you're not root.

| What | Command |
|---|---|
| Watch what the bot is doing (Ctrl+C to stop watching) | `journalctl -u shopbot -f` |
| Is it running? | `systemctl status shopbot` |
| Restart / stop / start | `systemctl restart shopbot` (or `stop`, `start`) |
| Update to the newest code | `git pull && bash deploy/install.sh` |
| Make a backup now | `.venv/bin/python -m shopbot.backup` |
| List the backups | `.venv/bin/python -m shopbot.backup --list` |
| Change the bot token | `nano .env` (edit `BOT_TOKEN=`, save with Ctrl+O, Enter, Ctrl+X), then `bash deploy/install.sh` |
| Remove the service (keeps your data) | `bash deploy/install.sh --uninstall` |

### Backups

A backup is made every night (around 03:17 server time) in `data/backups/`; the newest 14
are kept. They are on the same server, so now and then copy one to your own computer.
On **your computer** (not the server):

```bash
scp root@YOUR_SERVER_IP:Telegram-Claude/data/backups/*.db .
```

**Restore a backup** (on the server):

```bash
systemctl stop shopbot
.venv/bin/python -m shopbot.backup --restore data/backups/shop-2026-11-19_03-17-02_123456.db
systemctl start shopbot
```

The database you had before the restore is kept as a backup too.

### Moving an existing shop to the server

Already used the bot on your PC? Bring its data along:

1. On the PC, stop the bot (Ctrl+C), then in the bot folder run
   `python -m shopbot.backup`. It prints the backup file's name.
2. Copy that file to the server, e.g. from the PC: `scp data/backups/shop-....db root@YOUR_SERVER_IP:`
3. On the server: `systemctl stop shopbot`, then
   `.venv/bin/python -m shopbot.backup --restore ~/shop-....db`, then `systemctl start shopbot`.

Don't start the bot on the PC again afterwards.

### Keep the server safe

- **Change the root password** if it was ever shared (for example in a chat or email):
  type `passwd` on the server.
- Better still, log in with an SSH key instead of a password
  ([GitHub's guide to SSH keys](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/generating-a-new-ssh-key-and-adding-it-to-the-ssh-agent)
  shows how to make one; your provider's panel usually lets you add it).
- If the bot token was ever shared, get a new one: in @BotFather send `/revoke`, choose
  the bot, then put the new token in `.env` and run `bash deploy/install.sh`.

### If something goes wrong

| You see | What to do |
|---|---|
| `Telegram rejected BOT_TOKEN` | The token is wrong or was revoked. Get it from @BotFather (`/mybots` → your bot → *API Token*), put it in `.env`, run `bash deploy/install.sh`. |
| `Conflict: terminated by other getUpdates request` in the logs | Another copy of the bot runs somewhere (your PC, an old server). Stop that one. |
| `Another copy of the bot is already running` | You started `python bot.py` by hand while the service runs. Use `systemctl restart shopbot` instead. |
| `Couldn't reach Telegram` | The server has no internet right now. The bot keeps trying by itself. |
| The bot doesn't answer | `systemctl status shopbot` and `journalctl -u shopbot -n 50` show why. |

---

## B. Docker

On a server with Docker and the Compose plugin, after steps 1–3 above:

```bash
cp .env.example .env
nano .env                       # put your token after BOT_TOKEN=
docker compose up -d --build
docker compose logs -f          # Ctrl+C to stop watching
```

The data is in the Docker volume `shop-data`, so it survives restarts and updates.

| What | Command |
|---|---|
| Update | `git pull && docker compose up -d --build` |
| Stop / start | `docker compose stop` / `docker compose start` |
| Backup now | `docker compose exec -u bot bot python -m shopbot.backup` |
| Copy the backups out | `docker compose cp bot:/app/data/backups ./backups` |

Restore: `docker compose stop`, then
`docker compose cp ./shop-....db bot:/app/data/restore.db`, then
`docker compose run --rm bot python -m shopbot.backup --restore /app/data/restore.db`,
then `docker compose start`.

Keep `DB_PATH` at its default in `.env` when using Docker.

---

## C. Railway (no server)

1. Sign up at [railway.com](https://railway.com) with your GitHub account, and allow the
   Railway GitHub app to see this repository.
2. **New Project** → **Deploy from GitHub repo** → pick the repository. Railway finds the
   `Dockerfile` and builds it. In the service's **Settings → Source**, pick the branch
   that has the code.
3. In **Variables**, add:
   - `BOT_TOKEN` = your token
   - `RAILWAY_DEPLOYMENT_DRAINING_SECONDS` = `300` (gives the bot time to finish sending
     an order when Railway restarts it; the default stops it at once)
4. Add a **Volume** to the service (right-click the service → *Attach Volume*) with the
   mount path **`/app/data`**. Without it, the shop's data is lost on every deploy.
5. Leave *Serverless* off, don't add a public domain or a healthcheck, and leave the
   *Custom Start Command* empty. Click **Deploy**, then check **Deploy Logs** for
   "Bot @yourbot is running".

Railway's trial gives a small one-time credit; to keep the bot running you need the
Hobby plan (about $5 a month). Back up from time to time with Railway's volume backups.

---

Hosts that won't work well: anything that wipes the disk on restart or puts apps to sleep
(free web-service plans such as Render's free tier), and anything that runs two copies at
once. Fly.io and Render's paid background workers with a persistent disk also work, using
the `Dockerfile` and a volume mounted at `/app/data`.
