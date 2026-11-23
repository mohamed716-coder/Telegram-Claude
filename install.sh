#!/usr/bin/env bash
# Sets up (or updates) the bot on an Ubuntu/Debian server and keeps it running 24/7.
#
#   bash install.sh            first install, or update after `git pull`
#   bash install.sh --relogin  log in with your Telegram account again
set -euo pipefail

cd "$(dirname "$0")"
if [ "$(id -u)" -ne 0 ]; then
    exec sudo bash "$0" "$@"
fi

say() { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
fail() { printf '\n\033[1;31m%s\033[0m\n' "$*" >&2; exit 1; }

# 1. Docker ------------------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
    say "Installing Docker (takes a minute or two)"
    curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker >/dev/null 2>&1 || true
docker compose version >/dev/null 2>&1 || fail "Docker Compose is missing. Run: apt-get install -y docker-compose-plugin"

# 2. Settings (.env) -----------------------------------------------------------
ask() {  # ask <prompt> <regex>  -> prints the answer
    local answer
    while true; do
        read -rp "$1: " answer </dev/tty
        answer="${answer// /}"
        if [[ "$answer" =~ $2 ]]; then printf '%s' "$answer"; return; fi
        echo "  That doesn't look right, please try again." >/dev/tty
    done
}

if [ ! -f .env ]; then
    say "Your settings (see DEPLOY.md steps 1 and 2 for where to find them)"
    API_ID=$(ask "API_ID (a number, from my.telegram.org)" '^[0-9]+$')
    API_HASH=$(ask "API_HASH (32 letters/digits, from my.telegram.org)" '^[0-9a-fA-F]{32}$')
    BOT_TOKEN=$(ask "BOT_TOKEN (from @BotFather, like 123456:ABC...)" '^[0-9]+:[A-Za-z0-9_-]+$')
    cp .env.example .env
    sed -i -e "s|^API_ID=.*|API_ID=${API_ID}|" \
           -e "s|^API_HASH=.*|API_HASH=${API_HASH}|" \
           -e "s|^BOT_TOKEN=.*|BOT_TOKEN=${BOT_TOKEN}|" .env
    chmod 600 .env
    echo "Saved to .env (edit it later with: nano .env)"
fi

# 3. Build ---------------------------------------------------------------------
say "Building the bot"
docker compose build

# 4. Log in with your Telegram account (only the first time) ---------------------
has_login() {
    grep -qE '^SESSION_STRING=.+' .env ||
        docker compose run --rm -T --no-deps kdbot test -s /app/data/user_session.txt
}
if [ "${1:-}" = "--relogin" ] || ! has_login; then
    say "Log in with YOUR Telegram account (the one that will message @KageNoKoroshiBot)"
    echo "Telegram will send a login code to your Telegram app. Never share that code."
    docker compose stop >/dev/null 2>&1 || true
    docker compose run --rm kdbot python login.py
fi

# 5. Start ---------------------------------------------------------------------
say "Starting the bot"
docker compose up -d --force-recreate
sleep 8
docker compose logs --tail 15

if docker compose logs --tail 50 2>&1 | grep "Running as @" >/dev/null; then
    say "Done! The bot is running 24/7 and restarts by itself after crashes and reboots."
    echo "Open your bot in Telegram and press Start. You are the admin: try /code 25 0"
else
    say "The bot didn't report 'Running' yet. Check the messages above, or watch with:"
    echo "  docker compose logs -f"
fi
