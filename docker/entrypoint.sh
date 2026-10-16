#!/bin/sh
# Runs Fumora as PUID:PGID with UMASK, so files it writes stay usable by
# other containers and SMB users (Unraid default: 99:100, umask 0002).
set -e

case "$UMASK" in
    [0-7][0-7][0-7]|[0-7][0-7][0-7][0-7]) umask "$UMASK" ;;
    *) echo "Ignoring invalid UMASK '$UMASK'" >&2 ;;
esac

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$FUMORA_CONFIG_DIR"
    # Only the config folder is chowned; media folders are never touched.
    chown -R "$PUID:$PGID" "$FUMORA_CONFIG_DIR"
    exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups \
        uvicorn fumora.asgi:app --host 0.0.0.0 --port "$FUMORA_PORT" --proxy-headers
fi

exec uvicorn fumora.asgi:app --host 0.0.0.0 --port "$FUMORA_PORT" --proxy-headers
