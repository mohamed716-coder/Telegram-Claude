#!/usr/bin/env bash
# Runs on the server, started by deploy/ci-deploy.sh (not meant to be run by hand). It puts
# the uploaded code into ~/Telegram-Claude, a git checkout (git never touches the shop's
# data/ folder or .env), runs deploy/install.sh from there, and goes back to the code
# the server had if the new version doesn't install or start.
set -euo pipefail
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH"
export GIT_TERMINAL_PROMPT=0

say() { printf '==> %s\n' "$*"; }
warn() { printf '==> %s\n' "$*" >&2; }
die() { printf 'Error: %s\n' "$*" >&2; exit 1; }

# Only ever clean up the folder deploy/ci-deploy.sh uploaded this to, never "wherever
# this file happens to be".
src="${BASH_SOURCE[0]:-}"
here="$(cd "$(dirname "${src:-/nonexistent/x}")" 2>/dev/null && pwd -P || true)"
home="$(cd "$HOME" 2>/dev/null && pwd -P || true)"
case "$here" in
    "$home"/.shopbot-deploy.*) ;;
    *) die "This is started by deploy/ci-deploy.sh only. Nothing was changed." ;;
esac
[ -n "$src" ] && [ -n "$home" ] && [ -f "$here/code.bundle" ] \
    || die "This is started by deploy/ci-deploy.sh only. Nothing was changed."
trap 'rm -f "$here/code.bundle" "$here/remote.sh" "$here/token" "$here/binance"; rmdir "$here" 2>/dev/null || true' EXIT
token="$(cat "$here/token" 2>/dev/null || true)"
binance_key="$(sed -n 1p "$here/binance" 2>/dev/null || true)"
binance_secret="$(sed -n 2p "$here/binance" 2>/dev/null || true)"
rm -f "$here/token" "$here/binance"

branch="${1:?usage: ci-remote.sh BRANCH [OWNER/REPO]}"
repo="${2:-}"
app="$HOME/Telegram-Claude"
bundle="$here/code.bundle"
[ "$(id -u)" -eq 0 ] || die "Log in as root: this deploy needs root."
# Uploads left behind by a deploy that was cut off (older than a day).
find "$HOME" -maxdepth 1 -name '.shopbot-deploy.*' -type d -mmin +1440 -exec rm -rf {} + 2>/dev/null || true

# One deploy at a time (e.g. a re-run while the last one still runs here).
exec 9>"$HOME/.shopbot-deploy-lock"
if ! flock -n 9; then
    say "Waiting for another deploy on this server to finish ..."
    flock -w 1800 9 || die "Another deploy is running on this server for 30 minutes already."
fi

if ! command -v git >/dev/null; then
    say "Installing git (if the server is busy with its own updates, this waits for them) ..."
    for _ in $(seq 1 20); do  # "update" doesn't wait for apt's lock by itself
        DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 update -qq && break
        sleep 30
    done
    DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 install -y -qq git >/dev/null \
        || die "Couldn't install git. Log in and run: apt-get update && apt-get install git"
fi

first=""
previous=""
if [ ! -e "$app" ]; then
    say "Putting the code in $app ..."
    first=yes
    git init --quiet "$app"
    # For a "git pull" by hand later (DEPLOY.md, steps 2-3 set up the key it needs).
    git -C "$app" config core.sshCommand "ssh -i ~/.ssh/shopbot_deploy"
elif [ -d "$app/.git" ]; then
    say "Updating the code in $app ..."
    previous="$(git -C "$app" rev-parse --verify --quiet HEAD || true)"
else
    die "$app exists but is not a git copy of the bot. Rename it (mv $app $app.old) and run the deploy again. Its data stays in $app.old/data: bring it over with a backup and --restore (DEPLOY.md)."
fi
cd "$app"
if [ -n "$repo" ] && ! git remote get-url origin >/dev/null 2>&1; then
    git remote add origin "git@github.com:$repo.git"
fi
git fetch --quiet "$bundle" "+HEAD:refs/remotes/origin/$branch"
new="$(git rev-parse "refs/remotes/origin/$branch")"
if [ -n "$first" ]; then
    git checkout --quiet -B "$branch" "$new"
else
    if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
        git stash push --quiet --message "changed on the server, put aside by a deploy"
        warn "Code files that were changed on the server were put aside (see them: cd $app && git stash show -p)."
    fi
    if [ "$(git symbolic-ref --quiet --short HEAD || true)" != "$branch" ]; then
        if git show-ref --verify --quiet "refs/heads/$branch"; then
            git checkout --quiet "$branch"
        else
            git checkout --quiet -B "$branch" "$new"
        fi
    fi
    if git merge-base --is-ancestor HEAD "$new"; then
        git merge --quiet --ff-only "$new"
    elif git merge-base --is-ancestor "$new" HEAD; then
        warn "The server already has newer code ($(git rev-parse --short HEAD)), so it stays; not going back to $(git rev-parse --short "$new")."
    elif [ "$(git log -1 --format=%ct "$new")" -lt "$(git log -1 --format=%ct HEAD)" ]; then
        warn "The uploaded code is older than the server's ($(git rev-parse --short "$new") vs $(git rev-parse --short HEAD)), so the server keeps its code."
    else  # the history on GitHub was rewritten (or someone committed on the server)
        keep="before-deploy-$(date -u +%Y%m%d-%H%M%S)"
        git branch -f "$keep" HEAD
        git reset --quiet --hard "$new"  # only files git tracks: data/ and .env stay as they are
        warn "The code on GitHub went a different way than the code on the server, so the server follows GitHub. Its previous code is kept in the git branch $keep."
    fi
fi
if git remote get-url origin >/dev/null 2>&1; then
    git branch --quiet --set-upstream-to="origin/$branch" 2>/dev/null || true
fi
say "Code on the server: $(git log -1 --format='%h %s')"

# The BOT_TOKEN secret: offered to the installer, which takes it only if Telegram accepts
# it. One that hasn't changed since the last deploy doesn't undo a token changed here.
seen="$app/.git/shopbot-token-secret.sha256"  # git ignores files in .git; not in data/
sum=""
if [ -n "$token" ]; then
    export OFFERED_BOT_TOKEN="$token"
    sum="$(printf '%s' "$token" | sha256sum | cut -c1-64)"
    if [ "$(cat "$seen" 2>/dev/null || true)" = "$sum" ]; then export OFFERED_BOT_TOKEN_SEEN=yes; fi
fi
if [ -n "$binance_key" ]; then  # the BINANCE_API_KEY/SECRET secrets: saved in .env ("off": removed)
    export OFFERED_BINANCE_API_KEY="$binance_key" OFFERED_BINANCE_API_SECRET="$binance_secret"
fi
export SHOPBOT_VIA_CI=1  # the installer's messages then talk about secrets, not .env
rc=0
bash deploy/install.sh || rc=$?
if [ "$rc" -ne 0 ]; then
    if [ -n "$previous" ] && [ "$previous" != "$(git rev-parse HEAD)" ]; then
        warn "The new version didn't install or start, so the server went back to $(git rev-parse --short "$previous")."
        git reset --quiet --hard "$previous"
        if [ "$rc" -eq 3 ]; then  # the bot was restarted on the new code: start the old one
            bash deploy/install.sh || true
        fi
    fi
    exit 1
fi
if [ -n "$sum" ]; then (umask 077 && printf '%s\n' "$sum" >"$seen"); fi

# ------------------------------------------------------------------ status
# Only information from here on: nothing below may fail the deploy.
set +e +o pipefail
backups=0
if [ -d data/backups ]; then backups="$(find data/backups -maxdepth 1 -name 'shop-*.db' | wc -l)"; fi
echo
say "On the server now:"
printf '  %-16s %s\n' \
    "Code:" "$(git log -1 --format='%h %s')" \
    "Bot service:" "$(systemctl is-active shopbot.service), starts at boot: $(systemctl is-enabled shopbot.service)" \
    "Backups:" "every 6 hours ($(systemctl is-active shopbot-backup.timer)), next: $(systemctl show -P NextElapseUSecRealtime shopbot-backup.timer)" \
    "Backups kept:" "$backups of the last 28 (in $app/data/backups)" \
    "Free disk space:" "$(df -h --output=avail "$app" | tail -1 | tr -d ' ')" \
    "Updates:" "installed by GitHub when the bot's code changes (a backup is made first)"
if [ -f /var/run/reboot-required ]; then
    warn "The server needs a restart to finish its security updates: log in and type reboot (the bot starts again by itself)."
fi
echo
say "Automatic deposits: the server asks Binance with the key in .env ..."
report="$(timeout 60 env PYTHONDONTWRITEBYTECODE=1 "$app/.venv/bin/python" -m shopbot.binance --check 2>&1)"
[ "$?" -ne 124 ] || report="Binance: no answer within 60 seconds, so the key couldn't be checked."
printf '%s\n' "$report" | sed 's/^/  Binance check: /'
echo
say "The bot's last messages:"
journalctl -u shopbot.service -n 8 --no-pager -o cat
exit 0
