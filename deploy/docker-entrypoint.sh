#!/bin/sh
# Container entrypoint: give the "bot" user its data folder, then run the bot as that user.
# Volumes on many hosts (Railway, Fly.io, bind mounts) start out owned by root, which
# would leave a non-root bot unable to create its database.
set -e

db="${DB_PATH:-data/shop.db}"
case "$db" in
    /*) data_dir="$(dirname "$db")" ;;
    *) data_dir="$(dirname "/app/$db")" ;;
esac

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$data_dir"
    chown -R bot:bot "$data_dir"
    export HOME=/home/bot
    exec setpriv --reuid=bot --regid=bot --init-groups -- "$@"
fi
exec "$@"
