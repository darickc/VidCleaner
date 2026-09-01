#!/usr/bin/env bash
# Starts the api and worker as one supervised pair. If either exits, so does the
# container, and Docker's restart policy brings both back (PLAN.md §4).
set -euo pipefail

umask 0002

APP_USER=vidcleaner
PUID="${PUID:-99}"
PGID="${PGID:-100}"
VENV=/app/.venv/bin

# Recreate the runtime user with the ids the host expects (unraid: 99:100).
if ! getent group "$PGID" >/dev/null; then
    groupadd -g "$PGID" "$APP_USER"
fi
if ! getent passwd "$PUID" >/dev/null; then
    useradd -u "$PUID" -g "$PGID" -M -s /usr/sbin/nologin "$APP_USER"
fi
RUN_AS="${PUID}:${PGID}"

for dir in /config /work /backups; do
    mkdir -p "$dir"
    # Only the top level: chowning a large backups share on every boot is costly.
    chown "$RUN_AS" "$dir" 2>/dev/null || echo "warn: cannot chown $dir (read-only mount?)"
done

# Migrate once, before anything opens the database, so the two processes never race.
echo "==> alembic upgrade head"
gosu "$RUN_AS" "$VENV/alembic" -c /app/alembic.ini upgrade head

pids=()

start_api() {
    echo "==> starting api on :${VIDCLEANER_PORT}"
    gosu "$RUN_AS" "$VENV/uvicorn" vidcleaner.main:app \
        --host 0.0.0.0 --port "${VIDCLEANER_PORT}" --no-access-log &
    pids+=($!)
}

start_worker() {
    echo "==> starting worker"
    gosu "$RUN_AS" "$VENV/python" -m vidcleaner.worker_main &
    pids+=($!)
}

case "${VIDCLEANER_ROLE:-all}" in
    api) start_api ;;
    worker) start_worker ;;
    all) start_api; start_worker ;;
    *) echo "unknown VIDCLEANER_ROLE=${VIDCLEANER_ROLE}" >&2; exit 64 ;;
esac

terminate() {
    for pid in "${pids[@]}"; do kill -TERM "$pid" 2>/dev/null || true; done
    wait
}
trap terminate SIGTERM SIGINT

# Exit as soon as any child does, taking the others with it.
wait -n
status=$?
echo "==> a process exited with status ${status}; shutting down"
terminate
exit "$status"
