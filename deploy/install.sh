#!/usr/bin/env bash
# Install (or update) the Telegram shop bot as a service on an Ubuntu or Debian server.
#
#   cd Telegram-Claude
#   bash deploy/install.sh               install, or update after "git pull" (as root)
#   sudo bash deploy/install.sh          the same, logged in as a normal user
#   bash deploy/install.sh --uninstall   remove the service (keeps your data)
#
# The bot then starts by itself when the server boots and restarts if it crashes.
# A backup of the database is made every night (data/backups, the newest 14 are kept).
# Running this again is safe: it never deletes data/shop.db.
[ -n "${BASH_VERSION:-}" ] || exec bash "$0" "$@"  # started with "sh install.sh"
set -euo pipefail
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH"

SERVICE=shopbot
UNIT_DIR=/etc/systemd/system

say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m==> %s\033[0m\n' "$*" >&2; }
die() { printf '\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
VENV="$APP_DIR/.venv"
ENV_FILE="$APP_DIR/.env"
S=""  # "sudo " in the commands we print, for someone who used sudo
[ -z "${SUDO_USER:-}" ] || S="sudo "

[ "$(id -u)" -eq 0 ] || die "Run this as root, or with sudo:  sudo bash deploy/install.sh"
[ -f "$APP_DIR/bot.py" ] && [ -f "$APP_DIR/requirements.txt" ] \
    || die "Run deploy/install.sh from inside the bot's folder."

if [ "${1:-}" = "--uninstall" ]; then
    systemctl disable --now "$SERVICE.service" "$SERVICE-backup.timer" 2>/dev/null || true
    rm -f "$UNIT_DIR/$SERVICE.service" "$UNIT_DIR/$SERVICE-backup.service" \
        "$UNIT_DIR/$SERVICE-backup.timer"
    systemctl daemon-reload
    say "The service was removed. Your data is still in $APP_DIR/data."
    exit 0
fi
[ $# -eq 0 ] || die "Unknown option: $1 (use --uninstall, or nothing)"

[ -d /run/systemd/system ] && command -v systemctl >/dev/null \
    || die "This needs a Linux server that runs systemd (Ubuntu 22.04+ or Debian 12+)."
case "$APP_DIR" in
    *[!A-Za-z0-9._/-]*) die "Move the bot to a folder whose path has no spaces or special characters (now: $APP_DIR)." ;;
esac

# The bot runs as the user who owns the folder (the one who downloaded it).
owner_uid="$(stat -c %u "$APP_DIR")"
RUN_USER="$(id -nu "$owner_uid" 2>/dev/null)" \
    || die "The folder belongs to user id $owner_uid, which has no name here. Run: chown -R root: $APP_DIR"
RUN_GROUP="$(id -gn "$RUN_USER")"
as_user() {
    if [ "$RUN_USER" = root ]; then "$@"; else runuser -u "$RUN_USER" -- "$@"; fi
}
if [ "$RUN_USER" = root ]; then
    say "The folder belongs to root, so the bot runs as root (normal when you log in as root)."
elif ! as_user test -r "$APP_DIR/bot.py"; then
    die "The user $RUN_USER can't open $APP_DIR (a folder above it is private). Move it to /home/$RUN_USER and run this again."
fi

apt_install() {
    command -v apt-get >/dev/null || die "Please install $* yourself, then run this again."
    say "Installing $* (if the server is busy with its own updates, this waits for them) ..."
    # A mirror hiccup in "update" shouldn't stop us if the package can still be installed.
    DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 update -qq \
        || warn "apt-get update reported problems; trying to install anyway."
    DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 install -y -qq "$@" >/dev/null \
        || die "Couldn't install $*. If it says 'Could not get lock', the server is still installing updates: wait 5 minutes and run this again. Otherwise run 'apt-get update' and fix what it reports."
}

# ---------------------------------------------------------------- Python
PY="$(command -v python3 || true)"
if [ -z "$PY" ]; then
    apt_install python3 python3-venv
    PY="$(command -v python3)"
fi
"$PY" -c 'import sys; sys.exit(sys.version_info < (3, 10))' \
    || die "Python 3.10 or newer is needed; this server has $("$PY" -V 2>&1). Use Ubuntu 22.04+ or Debian 12+, or Docker (DEPLOY.md, part B)."

if ! { [ -x "$VENV/bin/python" ] && "$VENV/bin/python" -m pip --version >/dev/null 2>&1; }; then
    rm -rf "$VENV"
    if ! as_user "$PY" -m venv "$VENV" >/dev/null 2>&1; then
        rm -rf "$VENV"
        apt_install python3-venv
        as_user "$PY" -m venv "$VENV"
    fi
fi
chown -R "$RUN_USER:$RUN_GROUP" "$VENV"
say "Installing the bot's Python packages ..."
(cd "$APP_DIR" && as_user "$VENV/bin/python" -m pip install --quiet --disable-pip-version-check \
    -r requirements.txt)

# ----------------------------------------------------------------- token
token_in_env_file() {  # the token written in .env (not from the environment)
    env -u BOT_TOKEN -u TELEGRAM_BOT_TOKEN PYTHONDONTWRITEBYTECODE=1 "$VENV/bin/python" - "$APP_DIR" <<'PY'
import os, sys
sys.path.insert(0, sys.argv[1])
from shopbot.config import PROJECT_ROOT, load_dotenv
load_dotenv(PROJECT_ROOT / ".env")
print((os.environ.get("BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip())
PY
}

check_token() {  # prints @username; exit 2 = Telegram rejected it, 3 = couldn't ask Telegram
    TOKEN="$1" PYTHONDONTWRITEBYTECODE=1 "$VENV/bin/python" - <<'PY'
import json, os, sys, urllib.error, urllib.request
url = "https://api.telegram.org/bot%s/getMe" % os.environ["TOKEN"]
try:
    with urllib.request.urlopen(url, timeout=20) as response:
        print("@" + json.load(response)["result"]["username"])
except urllib.error.HTTPError as exc:
    sys.exit(2 if exc.code in (401, 404) else 3)
except Exception:
    sys.exit(3)
PY
}

save_token() (  # write BOT_TOKEN=... into .env, keeping every other line (subshell: umask stays here)
    umask 077
    [ -f "$ENV_FILE" ] || cp "$APP_DIR/.env.example" "$ENV_FILE"
    ENV_FILE="$ENV_FILE" TOKEN="$1" PYTHONDONTWRITEBYTECODE=1 "$VENV/bin/python" - <<'PY'
import os, re
path, token = os.environ["ENV_FILE"], os.environ["TOKEN"]
with open(path, encoding="utf-8") as fh:
    lines = fh.read().splitlines()
key = re.compile(r"^\s*(export\s+)?(BOT_TOKEN|TELEGRAM_BOT_TOKEN)\s*=")
out, done = [], False
for line in lines:
    if key.match(line):
        if not done:
            out.append("BOT_TOKEN=" + token)
            done = True
        continue
    out.append(line)
if not done:
    out.insert(0, "BOT_TOKEN=" + token)
with open(path, "w", encoding="utf-8") as fh:
    fh.write("\n".join(out) + "\n")
PY
)

ask_token() {
    ( exec </dev/tty ) 2>/dev/null \
        || die "Put the bot token in $ENV_FILE as BOT_TOKEN=..., then run this again."
    printf 'Paste the bot token from @BotFather (looks like 123456789:AAH...) and press Enter:\n> ' >/dev/tty
    IFS= read -r token </dev/tty || die "No token entered."
    token="$(printf '%s' "$token" | tr -d "[:space:]\"'")"
    where=""
}

file_token="$(token_in_env_file || true)"
token="${BOT_TOKEN:-$file_token}"
where=""
[ -n "${BOT_TOKEN:-}" ] || where=" in .env"
bot_name=""
while :; do
    if ! [[ "$token" =~ ^[0-9]+:[A-Za-z0-9_-]{30,}$ ]]; then
        [ -z "$token" ] || warn "The token$where doesn't look like a real bot token (is it still the example?)."
        ask_token
        continue
    fi
    rc=0
    bot_name="$(check_token "$token")" || rc=$?
    if [ "$rc" -eq 0 ]; then
        say "Token OK: the bot is $bot_name"
        break
    elif [ "$rc" -eq 2 ]; then
        warn "Telegram says the token$where is wrong or was revoked. Copy it again from @BotFather (/mybots -> your bot -> API Token)."
        token=""
    else
        warn "Couldn't reach Telegram to check the token (no internet yet?). Using it anyway."
        break
    fi
done
[ "$token" = "$file_token" ] || { save_token "$token"; say "Saved the token in $ENV_FILE"; }
chown "$RUN_USER:$RUN_GROUP" "$ENV_FILE"
chmod 600 "$ENV_FILE"

# ------------------------------------------------------------------ data
DB_FILE="$(cd "$APP_DIR" && PYTHONDONTWRITEBYTECODE=1 as_user "$VENV/bin/python" - <<'PY'
import sys
from shopbot.config import ConfigError, Settings
try:
    print(Settings.from_env().db_path)
except ConfigError as exc:
    print(f"Error in the settings: {exc}", file=sys.stderr)
    sys.exit(1)
PY
)" || die "Fix that in $ENV_FILE, then run this again."
DATA_DIR="$(dirname "$DB_FILE")"
case "$DATA_DIR" in
    *[!A-Za-z0-9._/-]*) die "DB_PATH must not contain spaces or special characters (now: $DB_FILE)." ;;
esac
case "$DATA_DIR" in
    "$APP_DIR"/?*) ;;
    *) die "Keep the database inside the bot folder (the default DB_PATH=data/shop.db). Now: $DB_FILE" ;;
esac
# Already installed from another copy of the code? Its data must not be left behind.
old_dir="$(sed -n 's/^WorkingDirectory=//p' "$UNIT_DIR/$SERVICE.service" 2>/dev/null || true)"
if [ -n "$old_dir" ] && [ "$old_dir" != "$APP_DIR" ] && [ -d "$old_dir" ] \
        && ls "$old_dir"/data/*.db >/dev/null 2>&1; then
    die "The bot is already installed from $old_dir, with its data there. Update that copy instead (cd $old_dir && git pull && ${S}bash deploy/install.sh). To switch to this copy, first run '${S}bash deploy/install.sh --uninstall' in $old_dir, then bring the data over with a backup and --restore."
fi
if [ -d "$DATA_DIR" ]; then  # it gets chown'ed and made writable: it must be the bot's own
    for entry in "$DATA_DIR"/* "$DATA_DIR"/.[!.]*; do
        [ -e "$entry" ] || continue
        name="$(basename "$entry")"
        case "$name" in
            *.db | *.db-* | *.db.* | bot.lock | backups | tmp*) ;;
            *) die "DB_PATH must point into a folder of its own, but $DATA_DIR also holds '$name'. Use the default (DB_PATH=data/shop.db) or a new folder." ;;
        esac
    done
fi
mkdir -p "$DATA_DIR"
chown -R "$RUN_USER:$RUN_GROUP" "$DATA_DIR"
if [ -f "$DB_FILE" ]; then  # a safety copy before changing anything (own folder: 5 kept)
    backup="$(cd "$APP_DIR" && as_user "$VENV/bin/python" -m shopbot.backup \
        --dir "$DATA_DIR/backups/before-update" --keep 5)" \
        && say "Backed up the database first: $backup" \
        || warn "Couldn't back up the database first (see above). Continuing."
fi

# -------------------------------------------------------------- services
say "Setting up the $SERVICE service ..."
# The bot only needs the internet and its data folder: lock everything else away.
HARDENING="NoNewPrivileges=yes
CapabilityBoundingSet=
AmbientCapabilities=
PrivateTmp=yes
PrivateDevices=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=$DATA_DIR
InaccessiblePaths=-/root/.ssh -/etc/ssh -/etc/shadow -/etc/gshadow
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes
RestrictNamespaces=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
LockPersonality=yes
SystemCallArchitectures=native
SystemCallFilter=@system-service
SystemCallErrorNumber=EPERM
UMask=0077"

umask 022
cat >"$UNIT_DIR/$SERVICE.service" <<EOF
[Unit]
Description=Telegram shop bot
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=$RUN_USER
Group=$RUN_GROUP
WorkingDirectory=$APP_DIR
ExecStart=$VENV/bin/python $APP_DIR/bot.py
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=10
# 78 = wrong settings (e.g. BOT_TOKEN): restarting would not help.
RestartPreventExitStatus=78
# Finishing a big delivery before stopping can take a few minutes.
TimeoutStopSec=300
$HARDENING

[Install]
WantedBy=multi-user.target
EOF

cat >"$UNIT_DIR/$SERVICE-backup.service" <<EOF
[Unit]
Description=Back up the Telegram shop bot database

[Service]
Type=oneshot
User=$RUN_USER
Group=$RUN_GROUP
WorkingDirectory=$APP_DIR
ExecStart=$VENV/bin/python -m shopbot.backup
$HARDENING
EOF

cat >"$UNIT_DIR/$SERVICE-backup.timer" <<EOF
[Unit]
Description=Daily backup of the Telegram shop bot database

[Timer]
OnCalendar=*-*-* 03:17:00
RandomizedDelaySec=15min
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --quiet "$SERVICE.service" "$SERVICE-backup.timer"
systemctl reset-failed "$SERVICE.service" 2>/dev/null || true
started_at="$(date +%s)"
systemctl restart "$SERVICE.service"
systemctl start "$SERVICE-backup.timer"

say "Starting ..."
# Wait until the bot says it is connected to Telegram (or stops).
connected=""
for _ in $(seq 1 45); do
    sleep 1
    if journalctl -u "$SERVICE.service" --since "@$started_at" -o cat --no-pager 2>/dev/null \
            | grep -q "is running. Owner ID"; then
        connected=yes
        break
    fi
    systemctl is-active --quiet "$SERVICE.service" || break
done

help_text() {
    cat <<EOF

  See what it's doing:    ${S}journalctl -u $SERVICE -f        (Ctrl+C to stop watching)
  Stop / start / restart: ${S}systemctl stop|start|restart $SERVICE
  Update to new code:     git pull && ${S}bash deploy/install.sh
  Backups:                $DATA_DIR/backups (made every night)

EOF
}
conflict() {  # another copy elsewhere takes the messages (Telegram: Conflict)
    for _ in $(seq 1 15); do  # Telegram reports it within one long-poll (~10 s)
        sleep 1
        journalctl -u "$SERVICE.service" --since "@$started_at" -o cat --no-pager 2>/dev/null \
            | grep -q "(Conflict)" && return 0
    done
    return 1
}
if [ -n "$connected" ] && systemctl is-active --quiet "$SERVICE.service" && conflict; then
    warn "The bot runs here, but another copy of it is running somewhere else (your PC, another"
    warn "server, Docker...). Stop that copy, or customers' messages go to the wrong one."
    help_text
elif [ -n "$connected" ] && systemctl is-active --quiet "$SERVICE.service"; then
    say "The bot ${bot_name:+$bot_name }is running. Open it in Telegram and send /start."
    help_text
elif systemctl is-active --quiet "$SERVICE.service"; then
    warn "The service runs, but the bot can't reach Telegram yet. It keeps trying by itself."
    warn "Check the server's internet connection, and watch it with: ${S}journalctl -u $SERVICE -f"
    help_text
else
    warn "The bot did not start. Its last messages:"
    journalctl -u "$SERVICE.service" --since "@$started_at" -n 25 --no-pager -o cat >&2 || true
    exit 1
fi
