#!/usr/bin/env bash
# Starts the api and worker as one supervised pair. If either exits, so does the
# container, and Docker's restart policy brings both back (PLAN.md §4).
set -euo pipefail

# §10's default. Overridable because a share whose group bit matters (or one shared
# with a user outside PGID) needs a different one, and hard-coding it meant the only
# fix was rebuilding the image.
umask "${UMASK:-0002}"

APP_USER=vidcleaner
PUID="${PUID:-99}"
PGID="${PGID:-100}"
VENV=/app/.venv/bin

# §10 lists OMP_NUM_THREADS in the entrypoint's env contract, and this is why:
# libgomp reads it **once**, at its own initialisation, so setting it from inside the
# worker after torch has loaded is a no-op. `cores - 2` matches §4, leaving room for
# the api and ffmpeg.
if [ -z "${OMP_NUM_THREADS:-}" ]; then
    CORES="$(nproc 2>/dev/null || echo 4)"
    OMP_NUM_THREADS="$(( CORES > 3 ? CORES - 2 : 1 ))"
fi
export OMP_NUM_THREADS
export MKL_NUM_THREADS="${MKL_NUM_THREADS:-$OMP_NUM_THREADS}"

# Recreate the runtime user with the ids the host expects (unraid: 99:100).
if ! getent group "$PGID" >/dev/null; then
    groupadd -g "$PGID" "$APP_USER"
fi
if ! getent passwd "$PUID" >/dev/null; then
    useradd -u "$PUID" -g "$PGID" -M -s /usr/sbin/nologin "$APP_USER"
fi
RUN_AS="${PUID}:${PGID}"

# `${HF_HOME}` too: the first job downloads a model into it, and on an existing
# appdata dir that directory would otherwise be created by whoever got there first.
for dir in /config /work /backups "${HF_HOME:-/config/models}"; do
    mkdir -p "$dir"
    # Only the top level: chowning a large backups share on every boot is costly.
    chown "$RUN_AS" "$dir" 2>/dev/null || echo "warn: cannot chown $dir (read-only mount?)"
done

# The first thing anyone needs when a fresh install cannot write to the media share.
echo "==> vidcleaner: uid=${PUID} gid=${PGID} umask=$(umask) threads=${OMP_NUM_THREADS} role=${VIDCLEANER_ROLE:-all} tz=${TZ:-unset}"
if [ ! -w /media ]; then
    echo "warn: /media is not writable by ${PUID}:${PGID} -- swaps will fail. Check PUID/PGID against the share's owner." >&2
fi

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
