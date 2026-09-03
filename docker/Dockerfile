# syntax=docker/dockerfile:1

# ---------- stage 1: build the SPA ----------
FROM node:22-alpine AS frontend

WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build


# ---------- stage 2: runtime ----------
# Debian trixie so that apt's ffmpeg is >= 7.0 (PLAN.md §10); the version is
# asserted below so a base image change can never silently ship an older one.
FROM python:3.12-slim-trixie AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    HF_HOME=/config/models \
    PUID=99 \
    PGID=100 \
    TZ=Etc/UTC \
    VIDCLEANER_PORT=8585 \
    VIDCLEANER_ROLE=all \
    VIDCLEANER_CONFIG_DIR=/config \
    VIDCLEANER_MEDIA_DIR=/media \
    VIDCLEANER_BACKUPS_DIR=/media/.vidcleaner-backups \
    VIDCLEANER_WORK_DIR=/work \
    VIDCLEANER_STATIC_DIR=/app/static

# tesseract-ocr(-eng) is M6: it reads a PGS subtitle track so a Blu-ray remux
# can stay in windowed STT mode instead of paying for a full-file pass. All of
# trixie main, on amd64 and arm64 alike, and no third-party repo -- unlike
# pgsrip's own Dockerfile, which we deliberately do not follow (see PLAN.md).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg gosu curl tzdata ca-certificates tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

# Fail the build rather than ship an ffmpeg older than the pipeline expects.
RUN ffmpeg -version | head -1 \
    && ffmpeg -version | head -1 | grep -Eq 'version n?([7-9]|[1-9][0-9])\.'

# Same idea for OCR: a missing or ancient tesseract degrades silently at runtime
# (the job just falls back to a full-file pass), so assert it at build time.
RUN tesseract --version | head -1 \
    && tesseract --version | head -1 | grep -Eq '^tesseract [5-9]\.'


COPY --from=ghcr.io/astral-sh/uv:0.7.21 /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies first so code edits do not invalidate the layer.
COPY backend/pyproject.toml backend/uv.lock ./
# --extra stt pulls faster-whisper and whisperX. torch comes from the PyTorch
# CPU index (pinned in pyproject.toml), so this does NOT drag the nvidia-*-cu12
# wheel set -- which would add roughly 2.5 GB to an image targeting ~3 GB.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra stt --extra ocr --no-install-project

COPY backend/ /app/
COPY --from=frontend /build/dist /app/static
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --extra stt --extra ocr

# NOT /backups: the default backups directory lives *inside* the /media mount,
# because `rename(2)` fails with EXDEV across two mount points even when they are
# the same filesystem -- so a separate /backups volume can never be a rename.
# Mount /backups yourself only alongside `allow_cross_device_backup`.
VOLUME ["/config", "/media", "/work"]
EXPOSE 8585

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS "http://localhost:${VIDCLEANER_PORT}/api/health" >/dev/null || exit 1

ENTRYPOINT ["/entrypoint.sh"]
