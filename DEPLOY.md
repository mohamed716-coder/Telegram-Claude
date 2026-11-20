# Deploying the shop bot (running it 24/7)

The bot has to run on a computer that is always on and online. Two things matter:

- **Keep its files.** Every balance, product and order is in the `data` folder
  (`data/shop.db`). Hosts whose disk is wiped on every restart (for example free "web
  service" plans) would lose your shop. The options below all keep it. To copy the shop
  anywhere, use a backup made by the bot (below), never `shop.db` itself: while the bot
  runs, its newest changes are in a second file next to it.
- **Run one copy only.** Two copies with the same token fight over messages
  (Telegram answers *409 Conflict*). If the bot runs on your PC, stop it there before
  starting it on a server.

Choose one:

| | Good for | Cost |
|---|---|---|
| **A. A Linux server (VPS)** | Recommended. One command sets everything up. | A few $ a month, or a server you already have |
| **B. Docker** | If you already run things with Docker | Same as A |
| **C. Railway** | No server to look after, everything in the browser | About $5 a month (Hobby plan) |

---

## A. Linux server (Ubuntu 22.04+ or Debian 12+)

You need: the server's **IP address** and **root password** (or a user that can use `sudo`),
and the **bot token** from [@BotFather](https://t.me/BotFather). The commands below are for
root; logged in as a normal user, put `sudo` in front of `apt-get`, `systemctl`,
`journalctl` and `bash deploy/install.sh` (never in front of `git` or `.venv/bin/python`),
and use your user name instead of `root` in `scp`.

> **Were your root password or bot token ever sent in a chat, email or message?** Then
> do this first. In @BotFather send `/revoke`, choose the bot, and keep the new token for
> step 4 (or for the `BOT_TOKEN` secret). Log in once (step 1), type `passwd` and set a new
> long password (20+ random characters, kept in a password manager), and use that one for
> the `VPS_PASSWORD` secret.

### The easy way: GitHub installs it for you

No typing on the server: GitHub's computers log in to it and install the bot, and later
they install every change to the bot's code by themselves (with a backup first). You only
add three secrets, once.

1. Open <https://github.com/mohamed716-coder/Telegram-Claude/settings/secrets/actions/new>
   (the repository's **Settings** → **Secrets and variables** → **Actions** → **New
   repository secret**). Add these three, one at a time: type the **Name**, paste the value
   under **Secret**, click **Add secret**.

   | Name | Value |
   |---|---|
   | `VPS_HOST` | the server's IP address, e.g. `203.0.113.25` |
   | `VPS_PASSWORD` | root's password |
   | `BOT_TOKEN` | the bot token from @BotFather |

   If the server's SSH port isn't 22, also add `VPS_PORT` with the port number.
2. Open the repository's **Actions** tab → **Deploy to the server** → the newest run →
   **Re-run jobs** → **Re-run all jobs**. (A run starts by itself when the code changes;
   the very first one may have started before your secrets were there.)
3. After about 3 minutes it shows a green check. Click the run → **deploy** → **Install the
   bot on the server**: near the end it says *The bot @yourbot is running*. Open the bot in
   Telegram and send `/start`.

GitHub keeps the secrets encrypted and hides them in the logs. If something goes wrong:

| You see | What to do |
|---|---|
| A red cross | The red line at the top of the run says what's wrong (a missing secret, a wrong password...). Fix it, then **Re-run jobs** → **Re-run all jobs**. |
| A grey run (skipped) | Nothing was installed: GitHub installs only the branch the server follows (`claude/telegram-bot-vphwhx`, or the `DEPLOY_BRANCH` variable, see below). |
| `The server wants a new root password` | Log in once by hand (step 1), set the new password it asks for, put it in `VPS_PASSWORD`, then re-run. |
| `The new version didn't start, so the server went back` (a yellow warning) | The bot keeps running the code it had. Nothing to do for you; the log says what went wrong. |
| `Lost the connection to the server` | Check that the server is on. Wait 5 minutes, then re-run: the install may have finished anyway. |
| `The server refused the password` | Check `VPS_PASSWORD`. If it's right, the server doesn't let root log in with a password: set it up by hand instead (below). |
| `Telegram rejected the BOT_TOKEN secret` (a yellow warning) | The bot still runs with the token it had. Put the current token in the `BOT_TOKEN` secret. |
| `The server's SSH key doesn't match deploy/server-fingerprint` (or `is not the one seen at the first deploy`) | Did you reinstall the server or change its SSH keys? Then its new fingerprint has to go into `deploy/server-fingerprint` (ask whoever maintains the code). If not, stop and change root's password: someone may be intercepting the connection. |

- **Changed the root password?** Put the new one in `VPS_PASSWORD` (the same Secrets page →
  the pencil next to it).
- **New bot token (after `/revoke`), or a different bot?** The bot stops until the server
  has it. Put it in `BOT_TOKEN` (pencil), then let GitHub install it: Actions tab →
  **Deploy to the server** → the newest run → **Re-run jobs** → **Re-run all jobs**. (No
  Re-run button because that run is over 30 days old? Log in to the server and run
  `cd ~/Telegram-Claude && bash deploy/install.sh`: it asks for the new token.)
- **To stop the automatic updates:** Actions tab → **Deploy to the server** → **...** →
  **Disable workflow**. Do this first if you want to stop or remove the bot for good,
  or the next code change installs and starts it again.
- **Moved the code to `main`** (merged it)? Add the repository variable `DEPLOY_BRANCH` with
  the value `main`: Settings → Secrets and variables → Actions → **Variables** tab → **New
  repository variable**. Then re-run the newest run on `main`.
- Everything further down (everyday commands, backups, restore) works once you log in to
  the server (the bot is in `/root/Telegram-Claude`), except: updates come from GitHub
  (`git pull` there needs the key from step 2), and to switch bots use the `BOT_TOKEN`
  secret as above.

**Or set it up by hand**, step by step:

### 1. Log in to the server

- **Windows 10/11:** press the Windows key, type `PowerShell`, press Enter. Then type
  `ssh root@` followed by your server's IP, e.g. `ssh root@203.0.113.25`, and press Enter.
- **Mac:** open *Terminal* and type the same.
- **Phone:** use an SSH app such as Termius (long-press to paste).

The first time, type `yes` to trust the server. Then type or paste the password: nothing
appears while you type, that's normal. Press Enter.

**Pasting:** right-click (Windows), Cmd+V (Mac). After pasting a box below, press Enter if
nothing happens, and wait until the line ending in `#` comes back before the next one.
Provider "web consoles" often can't paste; use PowerShell or an SSH app instead.

### 2. Let the server download the code

The repository is private, so the server needs its own read-only key. Paste this (it's one
command):

```bash
{ command -v git >/dev/null || { apt-get update && apt-get -o DPkg::Lock::Timeout=600 install -y git; }; } && \
mkdir -p ~/.ssh && chmod 700 ~/.ssh && \
{ [ -f ~/.ssh/shopbot_deploy ] || ssh-keygen -t ed25519 -N "" -C "shop-server" -f ~/.ssh/shopbot_deploy; } && \
cat ~/.ssh/shopbot_deploy.pub
```

It ends by printing one line starting with `ssh-ed25519`. (If it says `Could not get lock`
instead, the new server is still installing its own updates: wait 5 minutes and paste it
again.) Copy that whole line,
then on GitHub open the repository → **Settings** → **Deploy keys** → **Add deploy key**
(direct link: <https://github.com/mohamed716-coder/Telegram-Claude/settings/keys/new>).
Title: `shop server`; Key: paste the line; leave **Allow write access** unticked (the server
only needs to read); click **Add key**.

### 3. Download the bot

```bash
cd ~ && \
GIT_SSH_COMMAND="ssh -i ~/.ssh/shopbot_deploy -o StrictHostKeyChecking=accept-new" \
  git clone --branch claude/telegram-bot-vphwhx git@github.com:mohamed716-coder/Telegram-Claude.git && \
cd Telegram-Claude && \
git config core.sshCommand "ssh -i ~/.ssh/shopbot_deploy"
```

(`claude/telegram-bot-vphwhx` is the branch with the code. Once it is merged into `main`,
clone `main` instead.)

**Already used the bot on your PC?** Stop it there now, and bring its data along: see
[Moving an existing shop](#moving-an-existing-shop-to-the-server) right after step 4.

### 4. Install and start it

```bash
bash deploy/install.sh
```

Logged in as a normal user instead of root? Use `sudo bash deploy/install.sh`.

It installs what's needed, asks for the bot token (paste it, press Enter), checks it with
Telegram, and starts the bot as a service: it keeps running when you log out, starts
again when the server reboots, and restarts by itself if it ever crashes. It also makes a
backup of the database every 6 hours. (As root it says the bot runs as root: that's expected.)

When it says **"The bot @yourbot is running"**, open the bot in Telegram and send `/start`.
You can now close the window.

### Everyday commands

Run these on the server, inside the bot folder (`cd ~/Telegram-Claude`).

| What | Command |
|---|---|
| Is it running? (look for *active (running)*) | `systemctl status shopbot --no-pager -l` |
| The last messages, e.g. why it doesn't answer | `journalctl -u shopbot -n 30 --no-pager` |
| Watch what it's doing (Ctrl+C to stop watching) | `journalctl -u shopbot -f` |
| Restart / stop / start | `systemctl restart shopbot` (or `stop`, `start`) |
| Update to the newest code | `git pull && bash deploy/install.sh` (if GitHub installs it for you: automatic) |
| Make a backup now | `systemctl start shopbot-backup` |
| List the backups | `.venv/bin/python -m shopbot.backup --list` |
| New token (after `/revoke` in @BotFather) | `bash deploy/install.sh`: it sees the old one is revoked and asks for the new one (if GitHub installs it for you: the `BOT_TOKEN` secret, see above) |
| Switch to a different bot | `BOT_TOKEN=paste-the-new-token bash deploy/install.sh` (as a normal user: `sudo BOT_TOKEN=paste-the-new-token bash deploy/install.sh`; if GitHub installs it for you: the `BOT_TOKEN` secret, see above) |
| Remove the service (keeps your data) | `bash deploy/install.sh --uninstall` (if GitHub installs it for you, disable its workflow first, see above) |

If the screen shows `(END)` or `lines 1-23`, press `q` to get back.

### Backups

A backup is made every 6 hours, at about 00:17, 06:17, 12:17 and 18:17 **server time** (most
servers use UTC: in Istanbul that's 03:17, 09:17, 15:17 and 21:17), in `data/backups/`; the
newest 28 (one week) are kept. If the server was off at that time, the backup is made as
soon as it is on again. The
installer also keeps a safety copy from before each run in `data/backups/before-update/`.
Backups are on the same server, so now and then copy them to your own computer. On **your
computer** (not the server), in PowerShell or Terminal:

```bash
scp "root@YOUR_SERVER_IP:Telegram-Claude/data/backups/*.db" .
```

It asks for the server password. The files arrive in the folder the window is in (on
Windows usually `C:\Users\YOUR-NAME`). On a phone, use your SSH app's SFTP feature.

**Restore a backup** (on the server):

```bash
cd ~/Telegram-Claude && .venv/bin/python -m shopbot.backup --list
```

Then, with one of the listed file names (tip: type `data/backups/shop-` and press Tab):

```bash
systemctl stop shopbot; .venv/bin/python -m shopbot.backup --restore data/backups/shop-2026-11-19_03-17-02_123456.db; systemctl start shopbot
```

The bot starts again even if the restore fails, and the database you had before is kept
too (as `before-restore-....db`). **A restore turns the shop back to that moment:** deposits,
orders and products added since then are gone, and products sold since then are for sale
again. So pick the newest good backup.

### Automatic deposits with Binance Pay

Normally the owner checks a receipt for every deposit. With a **read-only Binance API
key**, the bot checks the payments itself:

1. The customer picks an amount (💳 Add Balance), sends **exactly** that much USDT with
   Binance Pay to your Binance ID, and sends the payment's **Order ID** to the bot.
2. The bot finds the payment in your Binance Pay history. When the coin and the amount
   match, the balance is added at once and you get a message about it.
3. When they don't (another amount or coin, an Order ID it can't find, or one that was
   already used), you get a ⚠️ message and the customer is asked for the Order ID again.
   When the second try fails too, the deposit comes to you with **Approve** / **Reject**,
   like a receipt (`/pending` shows it again).

Set it up once:

1. **Check your Binance ID.** In the Binance app, tap your profile picture (top left): the
   number after *UID* is your Binance ID. It must be the one customers pay to, `850940840`
   (`BINANCE_PAY_ID`). The bot warns you if the key belongs to another account.
2. **Create the key** at <https://www.binance.com/en/my/settings/api-management> (on a
   computer): **Create API** → **System generated** → name it `shop bot` → confirm. Then
   **Edit restrictions**: keep only **Enable Reading** ticked (never trading, transfers or
   withdrawals), choose **Restrict access to trusted IPs only** and add your server's IP
   address, then **Save**. Copy the **API Key** and the **Secret Key** (Binance shows the
   secret only once).
3. **Add two secrets** on the same GitHub page as the others
   (<https://github.com/mohamed716-coder/Telegram-Claude/settings/secrets/actions/new>):
   `BINANCE_API_KEY` (the API Key) and `BINANCE_API_SECRET` (the Secret Key). Never send
   them in a chat.
4. **Re-run the deploy:** Actions tab → **Deploy to the server** → the newest run →
   **Re-run jobs** → **Re-run all jobs**. Near the end of the log it says
   `Binance check: Binance: the key works from this server`. If something is wrong, a
   yellow warning at the top of the run says what.
5. **Test it with 1$:** from another Binance account (a friend's), 💳 Add Balance →
   ✏️ Custom amount → `1`, pay exactly 1 USDT to your Binance ID, then send the Order ID:
   the balance goes up by 1$. Binance has no test mode, so this is the real check.

| You see | What to do |
|---|---|
| `Binance refused the API key` | Copy both keys into the secrets again, check that *Enable Reading* is ticked and the server's IP address is in the key's list, then re-run. |
| `this key belongs to Binance ID ...` | The key is from another Binance account than the one customers pay to: create the key on that account. |
| `Binance doesn't serve this server's location` | Binance doesn't allow its API from the server's country, so automatic deposits can't work from this server. Deposits come to you to approve. |
| `this key can do more than read` | Delete that key in API Management and create one with only *Enable Reading*. |

- While Binance refuses the key (or doesn't serve the server's location), the bot tells you
  when it starts and at the next deposit (again at most every 6 hours), and every deposit
  comes to you to approve: customers can still deposit. So does a deposit while Binance
  can't be reached for a minute or more.
- A request marked ❗ has an Order ID Binance didn't confirm as a new payment to you (a
  typo, someone else's payment, or Binance couldn't be asked). Approve it only if you find
  the payment in your Binance app and haven't added it already: the request shows what was
  added since it was made.
- The bot can't tell which payment you approve such a request for, so it never adds a
  payment by itself that you may have added already: one made before you decided a ❗
  request, or while that customer's ❗ request waits, or before balance you added to that
  customer with `/addbal`. Such a payment comes to you instead, with a ⚠️ line saying why.
  So does a payment that was in a request you already decided (e.g. rejected for its
  amount, or approved for the other payment it showed).
- A request with a "⚠️ May be added already" or "⚠️ Added since this request was made" line
  isn't approved by ✅ Approve: the bot shows why once more and offers **⚠️ Approve
  anyway**, which approves only if nothing new turned up meanwhile.
- When a ❗ request's payment is then added automatically (Binance listed it late, or could
  be reached again), that request is closed by itself and you're told.
- **A payment you add by hand** (the customer wrote to you): add it with
  `/addbal ID AMOUNT ORDER_ID`, or register it afterwards with `/regbin ORDER_ID USER_ID`.
  Then it's never added again automatically (a request naming it needs ⚠️ Approve anyway),
  and the bot tells you what Binance has for it. A payment waiting with that customer's
  request is settled with it (the request is closed); with anyone else's, decide that
  request first.
- Only payments made after the key was added, and in the last 48 hours, are found. Only
  USDT counts by default (1 USDT = 1$); to take USDC too, add `BINANCE_COINS=USDT,USDC` to
  `~/Telegram-Claude/.env` on the server.
- **To turn it off:** set the `BINANCE_API_KEY` secret to `off` and re-run: deposits use
  receipts again. Delete the key on Binance too.
- Without GitHub: put `BINANCE_API_KEY=...` and `BINANCE_API_SECRET=...` in
  `~/Telegram-Claude/.env` and run `systemctl restart shopbot`;
  `cd ~/Telegram-Claude && .venv/bin/python -m shopbot.binance --check` tests the key.
- The server is in Türkiye, where paying for goods and services with crypto assets has been
  banned since April 2021 (a regulation of the Central Bank of the Republic of Türkiye).
  Make sure taking Binance payments is allowed where you and your customers are.

### Moving an existing shop to the server

Do this right after step 4, if the bot already ran on your PC:

1. On the PC, stop the bot (Ctrl+C) and don't start it there again. In the bot folder run
   `python -m shopbot.backup`. It prints the backup file's name.
2. Copy that file to the server, from the PC (keep the `:` at the end):
   `scp data/backups/shop-....db root@YOUR_SERVER_IP:`
3. On the server, with the file's real name:
   `cd ~/Telegram-Claude && systemctl stop shopbot; .venv/bin/python -m shopbot.backup --restore ~/shop-....db; systemctl start shopbot`

### Keep the server and the shop safe

- **The shop lives in `~/Telegram-Claude/data`** (the database and its backups). Never
  delete the `Telegram-Claude` folder, and don't download the bot again into a new one
  to update it: `git pull && bash deploy/install.sh` (above) updates it in place.
- **Root password:** if it was ever shared, type `passwd` on the server and set a long
  random one (kept in a password manager). Never use your provider's *Rebuild* or
  *Reinstall* button to change it: that erases the server.
- **Bot token:** if it was ever shared, in @BotFather send `/revoke`, choose the bot,
  then run `bash deploy/install.sh` and paste the new token when it asks.
- **Your Telegram account** controls the shop (owner panel, @BotFather): turn on
  *Settings → Privacy and Security → Two-Step Verification*.
- **Backups off the server:** turn on your provider's automatic backups or snapshots
  in its panel, and now and then copy the backups to your computer (above). They hold
  your unsold products and customers' data: don't send them to anyone.
- **Updates:** Ubuntu installs security updates by itself. If you see
  `*** System restart required ***` when you log in, type `reboot`: the bot starts again
  by itself within a minute. `git pull && bash deploy/install.sh` also updates the
  bot's own packages.
- **Is it alive?** Send `/start` to the bot now and then; if it doesn't answer, see below.
- If you stop using a server, delete its key on GitHub (repository → Settings → Deploy keys).

### If something goes wrong

| You see | What to do |
|---|---|
| `Telegram rejected BOT_TOKEN` | The token is wrong or was revoked. Run `bash deploy/install.sh` and paste the right token from @BotFather (`/mybots` → your bot → *API Token*). |
| `another copy of this bot is running` | Another copy runs somewhere (your PC, an old server). Stop that one. If none of yours runs anywhere, someone else has your token: `/revoke` it in @BotFather, then `bash deploy/install.sh`. |
| `Another copy of the bot is already running` | You started `python bot.py` by hand while the service runs. Use `systemctl restart shopbot` instead. |
| `Still trying to reach Telegram` / `Can't reach Telegram` | The server has no internet right now. The bot keeps trying by itself. |
| `Could not get lock` | The server is installing its own updates. Wait 5 minutes, then run the command again. |
| `destination path 'Telegram-Claude' already exists` | The bot is already downloaded: `cd ~/Telegram-Claude && git pull`, then go on with step 4. |
| `Can't open the database` | The database file is damaged. Restore the newest backup (see *Restore a backup*). |
| `Can't use the database` / `Can't use the data folder` | The disk may be full: `df -h /` shows it (delete files you don't need, or get a bigger disk). Or the files belong to another user: run `bash deploy/install.sh` again. |
| `git pull`: `Permission denied (publickey)` | The server has no key for GitHub (never added, or removed). Do step 2 again (it shows the same key), add it on GitHub, then `git pull`. If GitHub installs the bot for you, you don't need `git pull` at all. |
| `git pull`: `would be overwritten` | A code file was changed on the server. `git stash && git pull` puts that change aside. The shop itself (`data`, `.env`) is never touched by `git`. |
| `git pull`: `no such ref was fetched` | The code moved to the `main` branch: `git checkout main && git pull`. |
| The bot doesn't answer | `journalctl -u shopbot -n 30 --no-pager` shows why. |

---

## B. Docker

On a server with Docker and the Compose plugin, after steps 1–3 above:

```bash
cp .env.example .env
nano .env                       # put your token after BOT_TOKEN=
docker compose up -d --build
docker compose logs -f          # Ctrl+C to stop watching
```

The data is in the Docker volume `shopbot_shop-data`, so it survives restarts and updates.

| What | Command |
|---|---|
| Update | `git pull && docker compose up -d --build` |
| Change the token or a setting | `nano .env`, then `docker compose up -d` (`restart` keeps the old settings) |
| Stop / start | `docker compose stop` / `docker compose start` |
| Backup now | `docker compose exec -u bot bot python -m shopbot.backup` |
| Copy the backups out | `docker compose cp bot:/app/data/backups/. ./backups/` (then fetch `Telegram-Claude/backups/*.db` with `scp`, as in part A) |

Docker makes **no automatic backups**. For one every 6 hours, run `crontab -e` and add this
line (with your folder):

```
17 */6 * * * cd /root/Telegram-Claude && docker compose exec -T -u bot bot python -m shopbot.backup >/dev/null
```

Restore: `docker compose stop`, then
`docker compose cp ./backups/shop-....db bot:/app/data/restore.db`, then
`docker compose run --rm bot python -m shopbot.backup --restore /app/data/restore.db`,
then `docker compose start`.

Keep `DB_PATH` at its default in `.env` when using Docker.

---

## C. Railway (no server)

Check the current price on [railway.com/pricing](https://railway.com/pricing) first: the
bot needs the **Hobby** plan (about $5 a month); the trial's small one-time credit runs out.

1. Sign up at [railway.com](https://railway.com) with your GitHub account, and let the
   Railway GitHub app see this repository (GitHub → Settings → Applications → Railway →
   Configure).
2. **New Project** → **Deploy from GitHub repo** → pick the repository. In the service's
   **Settings → Source**, choose the branch that has the code (not an empty `main`).
   Railway finds the `Dockerfile` and builds it (the build log says it used the Dockerfile).
3. In **Variables**, add:
   - `BOT_TOKEN` = your token
   - `RAILWAY_DEPLOYMENT_DRAINING_SECONDS` = `300` (gives the bot time to finish sending
     an order when Railway restarts it; the default stops it at once)
4. Add a **Volume** to the service (right-click the service → *Attach Volume*) with the
   mount path **`/app/data`**. Without it, the shop's data is lost on every deploy.
   Leave `DB_PATH` unset.
5. In **Settings**, set the restart policy to **Always** (the default gives up after 10
   failed starts), leave *Serverless* off, add no public domain or healthcheck, and leave
   the *Custom Start Command* empty. Click **Deploy**, then check **Deploy Logs** for
   "Bot @yourbot is running".

Settings changes go in Railway's Variables tab, not a `.env` file. Railway's own volume
backups need its Pro plan; on Hobby, make a backup with the Railway CLI
(`railway ssh -- sh -c 'cd /app && python -m shopbot.backup'`) and download it from the
volume (`railway volume files download /backups/<file>`).

---

Hosts that won't work well: anything that wipes the disk on restart or puts apps to sleep
(free web-service plans such as Render's free tier), and anything that runs two copies at
once. Fly.io and Render's paid background workers with a persistent disk also work, using
the `Dockerfile` and a volume mounted at `/app/data`.
