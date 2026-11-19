#!/usr/bin/env bash
# Install or update the bot on a server over SSH. GitHub runs this for you (see
# .github/workflows/deploy.yml and DEPLOY.md, "The easy way: GitHub installs it for you").
# It also works from any Linux or Mac computer, inside the bot folder:
#
#   VPS_HOST=203.0.113.25 bash deploy/ci-deploy.sh
#
# It then asks for root's password and the bot token (typed values are not saved in your
# shell history, unlike VPS_PASSWORD=... on the command line).
#
# It logs in to the server as root, puts this copy of the code in ~/Telegram-Claude there
# (a git checkout: the shop's data/ folder and .env are never touched) and runs
# deploy/install.sh, which makes a backup first and then restarts the bot. If the new
# version doesn't start, the server goes back to the code it had.
#
#   VPS_HOST      the server's IP address (or name)                          required
#   VPS_PASSWORD  root's password                                            required
#   BOT_TOKEN     the bot token: needed the first time; later only a changed one is used
#                 (and never one Telegram rejects instead of a token that still works)
#   VPS_PORT      the SSH port, if not 22
#   KNOWN_HOSTS   where the server's identity is remembered when deploy/server-fingerprint
#                 doesn't pin it (default .deploy-known-hosts)
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
GUIDE="DEPLOY.md, \"The easy way: GitHub installs it for you\""

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
token="${BOT_TOKEN:-}"
if ! on_github && [ -t 0 ]; then  # by hand: ask, so nothing secret lands in shell history
    [ -n "$host" ] || read -rp "Server IP address: " host
    [ -n "$password" ] || { read -rsp "Root password: " password; echo; }
    [ -n "$token" ] || { read -rsp "Bot token (Enter to keep the server's): " token; echo; }
fi
password="${password%$'\n'}"  # pasted with the line ending
password="${password%$'\r'}"
token="$(printf '%s' "$token" | tr -d "[:space:]\"'")"
hide "$host"; hide "$password"; hide "$token"

missing=""
[ -n "$host" ] || missing="$missing VPS_HOST"
[ -n "$password" ] || missing="$missing VPS_PASSWORD"
[ -n "$missing" ] && [ -z "$token" ] && missing="$missing BOT_TOKEN"
[ -z "$missing" ] || fail "Add the repository secret(s)$missing: Settings -> Secrets and variables -> Actions -> New repository secret ($GUIDE). Then open the newest run in the Actions tab and press Re-run jobs -> Re-run all jobs."

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
hide "$host"  # the cleaned-up address is a new string: hide it too
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
if [ -z "$repo" ]; then  # the GitHub repository, for a "git pull" on the server (optional)
    repo="$( (git remote get-url origin 2>/dev/null || true) | sed -n 's#^.*github\.com[:/]\(.*[^/]\)$#\1#p')"
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
case "$1" in  # the server wants a new password first: never answer that
    *"ew password"* | *"urrent password"* | *"UNIX password"*) : >"$DEPLOY_SSH_PWCHANGE"; exit 1 ;;
esac
printf '%s\n' "$DEPLOY_SSH_PASSWORD"
EOF
chmod 700 "$work/askpass"
export DEPLOY_SSH_PASSWORD="$password" DEPLOY_SSH_PWCHANGE="$work/pwchange"
export SSH_ASKPASS="$work/askpass" SSH_ASKPASS_REQUIRE=force

# The server's identity. deploy/server-fingerprint pins it for good (a public value, saved
# after the first deploy); otherwise the key seen at the first deploy is remembered in
# the Actions cache (which GitHub drops after 7 days without a deploy).
pins="$(grep -Eo 'SHA256:[A-Za-z0-9+/=]+' deploy/server-fingerprint 2>/dev/null || true)"
known="${KNOWN_HOSTS:-.deploy-known-hosts}"
alias_opts=()
if [ -n "$pins" ]; then
    say "Checking the server's identity against deploy/server-fingerprint ..."
    known="$work/known_hosts"
    : >"$known"
    scanned="$(ssh-keyscan -T 20 -p "$port" -t rsa,ecdsa,ed25519 "$host" 2>/dev/null | grep -v '^#' || true)"
    [ -n "$scanned" ] || fail "Can't reach the server at VPS_HOST on port $port. Check the IP address and that the server is on."
    while read -r _ type key; do
        [ -n "$key" ] || continue
        fingerprint="$(printf 'x %s %s\n' "$type" "$key" | ssh-keygen -lf - 2>/dev/null | awk '{print $2}')"
        case $'\n'"$pins"$'\n' in  # one of the pinned fingerprints?
            *$'\n'"$fingerprint"$'\n'*) printf 'shopbot-server %s %s\n' "$type" "$key" >>"$known" ;;
        esac
    done <<<"$scanned"
    [ -s "$known" ] || fail "The server's SSH key doesn't match deploy/server-fingerprint. If you reinstalled the server or changed its SSH keys, put its new fingerprint in that file. If not, don't continue: someone may be intercepting the connection (and change root's password)."
    checking=yes
    alias_opts=(-o HostKeyAlias=shopbot-server)
elif [ -s "$known" ]; then
    checking=yes
    say "Checking that the server is the same one as at the first deploy (its SSH key)."
else
    checking=accept-new
    say "First connection to this server: its identity (SSH key) is remembered for the next deploys."
fi
ssh_opts=(
    -p "$port" "${alias_opts[@]}"
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
    [ ! -e "$work/pwchange" ] || reason="Password change required. $reason"
    case "$reason" in
        *"Password change required"* | *"password has expired"* | *"required to change your password"*)
            fail "The server wants a new root password before anything else. Log in once by hand (ssh root@<server IP>), set a new password as it asks, put that in the VPS_PASSWORD secret, then Re-run all jobs." ;;
        *"Host key verification failed"* | *"IDENTIFICATION HAS CHANGED"*)
            fail "The server's SSH key is not the one seen at the first deploy. If you reinstalled the server, delete the cache whose name starts with server-ssh-key (repository -> Actions -> Caches) and run this again. If not, don't continue: someone may be intercepting the connection (and change root's password)." ;;
        *"Permission denied"* | *"Too many authentication failures"*)
            fail "The server refused the password. Check the VPS_PASSWORD secret (root's password); if it's right, the server may not allow root to log in with a password. ($reason)" ;;
        *"Connection refused"* | *"timed out"* | *"No route to host"* | *"Could not resolve"* | *"Network is unreachable"* | *"Connection closed"* | *"Connection reset"*)
            fail "Can't reach the server at VPS_HOST on port $port. Check the IP address and that the server is on. ($reason)" ;;
        *)
            fail "Couldn't log in to the server (ssh exit code $rc). $reason" ;;
    esac
fi
if [ "$checking" = accept-new ] && [ -s "$known" ]; then
    fingerprint="$(ssh-keygen -l -f "$known" 2>/dev/null | awk '{print $2, $4}' | head -1)"
    say "Server key: $fingerprint"
    say "To check it on every deploy for good, put ${fingerprint%% *} in deploy/server-fingerprint."
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
rc=0
upload="$(tar -C "$work" -cf - code.bundle remote.sh token | ssh "${ssh_opts[@]}" -T "$target" \
    'umask 077 && d="$(mktemp -d "$HOME/.shopbot-deploy.XXXXXXXX")" && tar -C "$d" --no-same-owner -xf - && printf "%s\n" "$d"')" \
    || rc=$?
rm -f "$work/token"
[ "$rc" -ne 255 ] || fail "Lost the connection to the server while uploading (no answer for 2 minutes, or it restarted). Check that the server is on, then Re-run all jobs."
[ "$rc" -eq 0 ] || fail "Uploading the code to the server failed (see the message above). Is its disk full?"
case "$upload" in
    /*/.shopbot-deploy.*) ;;
    *) fail "The server didn't say where it put the upload. Re-run all jobs." ;;
esac

# ----------------------------------------------------------------- install
say "Installing on the server (this takes a few minutes the first time) ..."
rc=0
ssh "${ssh_opts[@]}" -T "$target" \
    "bash $(printf '%q' "$upload/remote.sh") $(printf '%q' "$branch") $(printf '%q' "$repo")" \
    </dev/null 2>&1 | tee "$work/install.log" || rc=$?
log="$(cat "$work/install.log")"
warn_if() {  # a GitHub warning when the server printed $1
    case "$log" in *"$1"*) note "$2" ;; esac
}
warn_if "Telegram rejected the BOT_TOKEN secret" \
    "Telegram rejected the BOT_TOKEN secret, so the bot keeps the token it already had. Put the current token from @BotFather in the BOT_TOKEN secret."
warn_if "token in .env was changed on the server" \
    "The server uses another bot token than the BOT_TOKEN secret (it was changed on the server). Put the server's token in the secret too, or the next changed secret replaces it."
warn_if "another copy of it is running somewhere else" \
    "Another copy of the bot runs somewhere else (your PC, another server...). Stop it, or customers' messages go to the wrong one."
warn_if "The server already has newer code" \
    "Nothing was updated: the server keeps its newer code. A deploy never takes the server back; to undo a change, push a commit that reverts it."
warn_if "were put aside" \
    "Code files that someone changed on the server were put aside (on the server: git stash list), so those edits no longer run."
warn_if "The uploaded code is older" \
    "Nothing was updated: the uploaded code is older than the server's."
warn_if "can't reach Telegram yet" \
    "The bot is installed but can't reach Telegram yet. It keeps trying by itself; check the server's internet connection."
warn_if "went back to" \
    "The new version didn't start, so the server went back to the code it had. The log above says why."
if [ "$rc" -ne 0 ]; then
    [ "$rc" -ne 255 ] || fail "Lost the connection to the server during the install (no answer for 2 minutes, or it restarted). The install may finish there by itself: wait 5 minutes, then Re-run all jobs to check."
    case "$log" in
        *"BOT_TOKEN secret"*"then run the deploy again"* | *"Put the bot token in"* | *"token"*"wrong or was revoked"*)
            fail "The server has no working bot token. Put the current token from @BotFather (/mybots -> your bot -> API Token) in the BOT_TOKEN secret, then Re-run all jobs." ;;
        *"Another deploy is running"*)
            fail "Another deploy was still running on the server. Wait for it, then Re-run all jobs." ;;
        *"so the server went back to"*)
            fail "The new version didn't start, so the server went back to the code it had: the bot runs as before. The messages above show why the new version failed." ;;
        *)
            fail "Installing on the server failed: the messages above say why (lines starting with Error)." ;;
    esac
fi
running="$(sed -n 's/^==> Code on the server: //p' "$work/install.log" | tail -1)"
say "Done. The server runs: ${running:-$commit}"
