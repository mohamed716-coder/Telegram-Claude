#!/usr/bin/env bash
# Install or update the bot on a server over SSH. GitHub runs this for you (see
# .github/workflows/deploy.yml and DEPLOY.md, "Let GitHub install it"); it also works from
# any Linux or Mac computer, inside the bot folder:
#
#   VPS_HOST=203.0.113.25 VPS_PASSWORD='...' BOT_TOKEN='...' bash deploy/ci-deploy.sh
#
# It logs in to the server as root, puts this copy of the code in ~/Telegram-Claude there
# (a git checkout: the shop's data/ folder and .env are never touched) and runs
# deploy/install.sh, which makes a backup first and then restarts the bot.
#
#   VPS_HOST      the server's IP address (or name)                          required
#   VPS_PASSWORD  root's password                                            required
#   BOT_TOKEN     the bot token: used if Telegram accepts it (needed the first time;
#                 an outdated one never replaces a token on the server that still works)
#   VPS_PORT      the SSH port, if not 22
#   KNOWN_HOSTS   where the server's identity is remembered (default .deploy-known-hosts)
set -euo pipefail

say() { printf '==> %s\n' "$*"; }
on_github() { [ "${GITHUB_ACTIONS:-}" = true ]; }
fail() {  # one line: GitHub shows ::error:: lines at the top of the run
    if on_github; then printf '::error::%s\n' "$*" >&2; else printf 'Error: %s\n' "$*" >&2; fi
    exit 1
}
note() {
    if on_github; then printf '::warning::%s\n' "$*" >&2; else printf 'Warning: %s\n' "$*" >&2; fi
}
hide() {  # never show this value in GitHub's log (secrets are hidden, but not edited copies)
    if on_github && [ -n "$1" ]; then printf '::add-mask::%s\n' "$1"; fi
}

cd "$(dirname "${BASH_SOURCE[0]}")/.."
[ -f bot.py ] && [ -f deploy/install.sh ] && [ -f deploy/ci-remote.sh ] \
    || fail "Run this from the bot's folder (it needs bot.py and deploy/)."

# ----------------------------------------------------------------- settings
trim() {
    local v="$1"
    v="${v#"${v%%[![:space:]]*}"}"
    printf '%s' "${v%"${v##*[![:space:]]}"}"
}
host="$(trim "${VPS_HOST:-}")"
port="$(trim "${VPS_PORT:-}")"
password="${VPS_PASSWORD:-}"
password="${password%$'\n'}"  # pasted with the line ending
password="${password%$'\r'}"
token="$(printf '%s' "${BOT_TOKEN:-}" | tr -d "[:space:]\"'")"
hide "$host"; hide "$password"; hide "$token"

missing=""
[ -n "$host" ] || missing="$missing VPS_HOST"
[ -n "$password" ] || missing="$missing VPS_PASSWORD"
[ -z "$missing" ] || fail "Add the secret(s)$missing: in the repository, Settings -> Secrets and variables -> Actions -> New repository secret (DEPLOY.md: Let GitHub install it). Then run this again."

# Forgive the usual ways of writing an address: ssh://, root@, :port, a trailing slash.
host="${host#*://}"
host="${host#*@}"
host="${host%/}"
if [[ "$host" =~ ^([0-9.]+):([0-9]+)$ ]]; then
    host="${BASH_REMATCH[1]}"
    port="${port:-${BASH_REMATCH[2]}}"
fi
host="${host#[}"
host="${host%]}"
case "$host" in
    "" | -* | *[!A-Za-z0-9.:-]*)
        fail "VPS_HOST must be just the server's IP address, like 203.0.113.25 (no spaces, no http://)." ;;
esac
port="${port:-22}"
case "$port" in
    *[!0-9]* | "") fail "VPS_PORT must be a number, like 22." ;;
esac
if [ -n "$token" ] && ! [[ "$token" =~ ^[0-9]+:[A-Za-z0-9_-]{30,}$ ]]; then
    # Like an outdated token: not used, so a token already on the server keeps working.
    note "The BOT_TOKEN secret doesn't look like a bot token (like 123456789:AAH...), so it is not used. Copy it again from @BotFather (/mybots -> your bot -> API Token)."
    token=""
fi

branch="${DEPLOY_BRANCH:-${GITHUB_REF_NAME:-}}"
[ -n "$branch" ] || branch="$(git rev-parse --abbrev-ref HEAD)"
[ "$branch" != HEAD ] || fail "Check out the branch to deploy first (git checkout <branch>)."
repo="${GITHUB_REPOSITORY:-}"
if [ -z "$repo" ]; then  # the GitHub repository, for the server's "git pull" (optional)
    repo="$(git remote get-url origin 2>/dev/null | sed -n 's#^.*github\.com[:/]\(.*[^/]\)$#\1#p')"
    repo="${repo%.git}"
fi
if ! on_github && [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    note "Files you changed but didn't commit are not deployed (only commit $(git rev-parse --short HEAD) is)."
fi

# ---------------------------------------------------------------------- ssh
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
cat >"$work/askpass" <<'EOF'
#!/bin/sh
printf '%s\n' "$DEPLOY_SSH_PASSWORD"
EOF
chmod 700 "$work/askpass"
export DEPLOY_SSH_PASSWORD="$password" SSH_ASKPASS="$work/askpass" SSH_ASKPASS_REQUIRE=force

known="${KNOWN_HOSTS:-.deploy-known-hosts}"
if [ -s "$known" ]; then
    checking=yes
    say "Checking that the server is the same one as at the first deploy (its SSH key)."
else
    checking=accept-new
    say "First connection to this server: its identity (SSH key) is remembered for the next deploys."
fi
ssh_opts=(
    -p "$port"
    -o "UserKnownHostsFile=$known" -o GlobalKnownHostsFile=/dev/null
    -o "StrictHostKeyChecking=$checking" -o UpdateHostKeys=no -o HashKnownHosts=yes
    -o PubkeyAuthentication=no -o PreferredAuthentications=keyboard-interactive,password
    -o NumberOfPasswordPrompts=1 -o ConnectTimeout=30
    -o ServerAliveInterval=20 -o ServerAliveCountMax=6 -o LogLevel=ERROR
)
target="root@$host"

say "Logging in to the server ..."
rc=0
ssh "${ssh_opts[@]}" -T "$target" true 2>"$work/ssh.err" </dev/null || rc=$?
if [ "$rc" -ne 0 ]; then
    reason="$(tr '\r\n' '  ' <"$work/ssh.err" | sed 's/  */ /g; s/ $//')"
    case "$reason" in
        *"Host key verification failed"* | *"IDENTIFICATION HAS CHANGED"*)
            fail "The server's SSH key is not the one seen at the first deploy. If you reinstalled the server, delete the cache whose name starts with server-ssh-key (repository -> Actions -> Caches) and run this again. If not, someone may be intercepting the connection: don't continue." ;;
        *"Permission denied"* | *"Too many authentication failures"*)
            fail "The server refused the password. Check the VPS_PASSWORD secret (root's password); if it's right, the server may not allow root to log in with a password. ($reason)" ;;
        *"Connection refused"* | *"timed out"* | *"No route to host"* | *"Could not resolve"* | *"Network is unreachable"* | *"Connection closed"* | *"Connection reset"*)
            fail "Can't reach the server at VPS_HOST on port $port. Check the IP address and that the server is on. ($reason)" ;;
        *)
            fail "Couldn't log in to the server (ssh exit code $rc). $reason" ;;
    esac
fi
if [ "$checking" = accept-new ] && [ -s "$known" ]; then
    say "Server key: $(ssh-keygen -l -f "$known" 2>/dev/null | awk '{print $2, $4}' | head -1)"
    # Tells the workflow to keep the file for the next deploys (see deploy.yml).
    if on_github && [ -n "${GITHUB_OUTPUT:-}" ]; then echo "remember-server=true" >>"$GITHUB_OUTPUT"; fi
fi

# ------------------------------------------------------------------ upload
commit="$(git rev-parse --short HEAD)"
say "Packing the code: $branch at $commit ($(git log -1 --format=%s HEAD))"
git bundle create --quiet "$work/code.bundle" HEAD
git show HEAD:deploy/ci-remote.sh >"$work/remote.sh"  # the one that matches the code
(umask 077 && printf '%s' "$token" >"$work/token")

say "Uploading it to the server ..."
tar -C "$work" -cf - code.bundle remote.sh token | ssh "${ssh_opts[@]}" -T "$target" \
    'umask 077 && rm -rf ~/.shopbot-deploy && mkdir ~/.shopbot-deploy && tar -C ~/.shopbot-deploy --no-same-owner -xf -' \
    || fail "Uploading the code to the server failed (see the message above). Is its disk full?"
rm -f "$work/token"

# ----------------------------------------------------------------- install
say "Installing on the server (this takes a few minutes the first time) ..."
rc=0
ssh "${ssh_opts[@]}" -T "$target" \
    "bash ~/.shopbot-deploy/remote.sh $(printf '%q' "$branch") $(printf '%q' "$repo")" \
    </dev/null 2>&1 | tee "$work/install.log" || rc=$?
log="$(cat "$work/install.log")"
case "$log" in *"Telegram rejected the BOT_TOKEN secret"*)
    note "Telegram rejected the BOT_TOKEN secret, so the bot keeps the token it already had. Put the current token from @BotFather in the BOT_TOKEN secret." ;;
esac
case "$log" in *"another copy of it is running somewhere else"*)
    note "Another copy of the bot runs somewhere else (your PC, another server...). Stop it, or customers' messages go to the wrong one." ;;
esac
if [ "$rc" -ne 0 ]; then
    case "$log" in
        *"Put the bot token in"* | *"token"*"wrong or was revoked"*)
            fail "The server has no working bot token. Put the current token from @BotFather (/mybots -> your bot -> API Token) in the BOT_TOKEN secret, then run this again." ;;
        *)
            fail "Installing on the server failed: the messages above say why (lines starting with Error)." ;;
    esac
fi
running="$(sed -n 's/^==> Code on the server: //p' "$work/install.log" | tail -1)"
say "Done. The server runs: ${running:-$commit}"
