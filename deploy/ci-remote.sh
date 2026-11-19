#!/usr/bin/env bash
# Runs on the server, started by deploy/ci-deploy.sh (not meant to be run by hand). It puts
# the uploaded code into ~/Telegram-Claude, a git checkout (git never touches the shop's
# data/ folder or .env), then runs deploy/install.sh from there.
set -euo pipefail
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH"
export GIT_TERMINAL_PROMPT=0

say() { printf '==> %s\n' "$*"; }
warn() { printf '==> %s\n' "$*" >&2; }
die() { printf 'Error: %s\n' "$*" >&2; exit 1; }

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
trap 'rm -rf "$here"' EXIT
branch="${1:?usage: ci-remote.sh BRANCH [OWNER/REPO]}"
repo="${2:-}"
app="$HOME/Telegram-Claude"
bundle="$here/code.bundle"
token="$(cat "$here/token" 2>/dev/null || true)"
rm -f "$here/token"

[ "$(id -u)" -eq 0 ] || die "Log in as root: this deploy needs root."
if ! command -v git >/dev/null; then
    say "Installing git (if the server is busy with its own updates, this waits for them) ..."
    DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 update -qq || true
    DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=600 install -y -qq git >/dev/null \
        || die "Couldn't install git. Log in and run: apt-get install git"
fi

first=""
if [ ! -e "$app" ]; then
    say "Putting the code in $app ..."
    first=yes
    git init --quiet "$app"
    # For a "git pull" by hand later (DEPLOY.md, steps 2-3 set up the key it needs).
    git -C "$app" config core.sshCommand "ssh -i ~/.ssh/shopbot_deploy"
elif [ -d "$app/.git" ]; then
    say "Updating the code in $app ..."
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
if [ -n "$token" ]; then export OFFERED_BOT_TOKEN="$token"; fi
bash deploy/install.sh

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
    "Nightly backup:" "$(systemctl is-active shopbot-backup.timer), next: $(systemctl show -P NextElapseUSecRealtime shopbot-backup.timer)" \
    "Backups kept:" "$backups (in $app/data/backups)" \
    "Free disk space:" "$(df -h --output=avail "$app" | tail -1 | tr -d ' ')" \
    "Updates:" "installed by GitHub when the bot's code changes (a backup is made first)"
if [ -f /var/run/reboot-required ]; then
    warn "The server needs a restart to finish its security updates: log in and type reboot (the bot starts again by itself)."
fi
echo
say "The bot's last messages:"
journalctl -u shopbot.service -n 8 --no-pager -o cat
exit 0
