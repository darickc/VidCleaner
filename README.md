# VidCleaner

Self-hosted Docker app that mutes profanity in the *audio* of movies and TV episodes in a
Jellyfin library managed by Sonarr/Radarr, with a web UI to review exactly what was removed
and undo mistakes.

It adds a **Clean** audio track as the new default track, keeps the original audio as track 2,
and keeps an untouched backup of the source file — so every change is reversible. Speech is
located locally on the CPU (faster-whisper + whisperX), muted with ffmpeg, and Sonarr/Radarr
and Jellyfin are refreshed afterwards.

> **`PLAN.md` is the source of truth** for design decisions, the job pipeline, and the
> milestone order. Read it before changing anything here.

## Status

Milestone **M1 — core clean via CLI** is complete. The pipeline runs end to end on a real file:

```bash
cd backend
uv run vidcleaner detect "/media/Movies/Some Film (2024)/Some Film.mkv"          # counts only
uv run vidcleaner clean  "/media/Movies/Some Film (2024)/Some Film.mkv" --out out.mkv
```

`detect` prints per-word counts and flags anything that needs review; `clean` writes an MKV whose
first and default audio track is the muted "Clean" track, with the untouched original kept as
"Original", English subtitles redacted, and chapters, attachments and every other stream copied
through. Both record the run in the database. `uv run vidcleaner words` shows the built-in word
lists and runs the false-positive gate over them.

There is no automation yet — no job queue, no Sonarr/Radarr integration, and the review UI is
still a shell. Those are M3 and M4.

## Development

Backend (Python 3.12, [uv](https://docs.astral.sh/uv/)):

```bash
cd backend && uv sync && uv run pytest
```

```bash
cd backend && uv run uvicorn vidcleaner.main:app --reload --port 8585
```

```bash
cd backend && uv run python -m vidcleaner.worker_main
```

Frontend (Vite + React + TypeScript), proxies `/api` to the backend above:

```bash
cd frontend && npm install && npm run dev
```

```bash
cd frontend && npm test
```

With no container mounts present, the backend writes to `.local/` in the repo root and serves
the SPA from `frontend/dist` once you have run `npm run build`. **`ffmpeg` >= 7.0 must be on
PATH** (`brew install ffmpeg`) -- the pipeline and the integration tests both need it. The
speech-to-text stack is an optional extra: `uv sync --extra stt` (faster-whisper + whisperX,
CPU-only torch). Without it everything except the `transcribe` stage still runs, and the
integration tests that need speech recognition skip themselves.

## Container

```bash
docker compose up --build
```

Volumes: `/config` (database, settings, STT models, logs), `/media` (the library — **must** be
the same path Sonarr/Radarr/Jellyfin use), `/backups` (originals, kept on the media share so
swaps are same-filesystem renames) and `/work` (scratch, put it on an SSD). On unraid, use
`unraid/vidcleaner.xml`. Override the host paths with `MEDIA_DIR`, `BACKUPS_DIR`, `CONFIG_DIR`
and `WORK_DIR` when running compose elsewhere.

The image is about 3 GB (Debian trixie + ffmpeg 7.1 + CPU-only torch) and has been built and
exercised for both `linux/amd64` (unraid) and `linux/arm64`. The image runs both processes;
`VIDCLEANER_ROLE=api|worker|all` splits them if you ever want
them on different hosts. `GET /api/health` backs the healthcheck and reports the database
revision, ffmpeg version and free disk per volume.
