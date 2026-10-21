#!/usr/bin/env bash
# Start the real image the way Unraid runs it and check what it does to files.
#   tests/container/smoke.sh [image]   (default: builds fumora:smoke)
# Needs docker and sudo (to give the test folders to 99:100 like Unraid's nobody:users).
set -euo pipefail
IMAGE="${1:-}"
if [ -z "$IMAGE" ]; then
    IMAGE=fumora:smoke
    docker build -q -t "$IMAGE" "$(dirname "$0")/../.." >/dev/null
fi
WORK="$(mktemp -d)"
NAME="fumora-smoke-$$"
PORT=18792
cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; sudo rm -rf "$WORK"; }
trap cleanup EXIT
fail() { echo "FAIL: $*"; docker logs "$NAME" 2>&1 | tail -20; exit 1; }

mkdir -p "$WORK/config" "$WORK/data/media/comics"
sudo chown -R 99:100 "$WORK"

start() {  # $1 = mount mode for /data (ro|rw)
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    docker run -d --name "$NAME" -p "$PORT:8792" -e FUMORA_SCHEDULER=0 -e FUMORA_DOWNLOAD_MONITOR=0 \
        -v "$WORK/config:/config" -v "$WORK/data:/data:$1" "$IMAGE" >/dev/null
    for _ in $(seq 1 30); do
        curl -fs "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1 && return 0
        sleep 1
    done
    fail "no /api/health answer"
}

start ro
status="$(docker exec "$NAME" cat /proc/1/status)"
echo "$status" | grep -qE '^Uid:\s+99\s' || fail "app is not running as uid 99"
echo "$status" | grep -qE '^Gid:\s+100\s' || fail "app is not running as gid 100"
echo "$status" | grep -qE '^Umask:\s+0002' || fail "app umask is not 0002"
db="$WORK/config/fumora.db"
sudo test -f "$db" || fail "no database created"
[ "$(sudo stat -c %u:%g "$db")" = "99:100" ] || fail "database not owned by 99:100"
[ "$(sudo stat -c %a "$db")" = "664" ] || fail "database mode is $(sudo stat -c %a "$db"), not 664"
sudo find "$WORK" \( ! -uid 99 -o ! -gid 100 \) -print | grep -q . && fail "files not owned by 99:100: $(sudo find "$WORK" \( ! -uid 99 -o ! -gid 100 \) -print)"
docker logs "$NAME" 2>&1 | grep -q "is read-only for Fumora" || fail "read-only library was not reported"

start rw
docker logs "$NAME" 2>&1 | grep -q "is read-only for Fumora" && fail "writable library reported as read-only"
docker logs "$NAME" 2>&1 | grep -qi "running as root" && fail "reported running as root"
echo "container smoke test passed ($IMAGE)"
