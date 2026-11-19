#!/bin/sh
# Container entrypoint: give the "bot" user its data folder, then run the bot as that user.
# Volumes on many hosts (Railway, Fly.io, bind mounts) start out owned by root, which
# would leave a non-root bot unable to create its database.
set -e

# On Railway, use the attached volume wherever it is mounted.
if [ -z "${DB_PATH:-}" ] && [ -n "${RAILWAY_VOLUME_MOUNT_PATH:-}" ]; then
    export DB_PATH="${RAILWAY_VOLUME_MOUNT_PATH%/}/shop.db"
fi
db="${DB_PATH:-data/shop.db}"
case "$db" in
    /*) data_dir="$(dirname "$db")" ;;
    *) data_dir="$(dirname "/app/$db")" ;;
esac

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$data_dir"
    chown -R bot:bot "$data_dir"
fi

# Data that is not on a volume disappears when the container is replaced (every update).
if [ -d "$data_dir" ] && [ "$(stat -c %d "$data_dir")" = "$(stat -c %d /)" ]; then
    echo "WARNING: $data_dir is not on a volume, so the shop's data will be LOST when this" \
        "container is replaced. Mount a volume at $data_dir (see DEPLOY.md)." >&2
fi

if [ "$(id -u)" = "0" ]; then
    export HOME=/home/bot
    exec setpriv --reuid=bot --regid=bot --init-groups -- "$@"
fi
exec "$@"
