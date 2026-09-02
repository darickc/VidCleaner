# VidCleaner

Self-hosted Docker app that mutes profanity in movie/TV audio for a Jellyfin library managed by
Sonarr/Radarr, with a review UI. Python (FastAPI + worker) backend, React/TypeScript frontend.

## Before doing anything

1. Read `PLAN.md`. It is the source of truth: §2 locked decisions, §6–§7 pipeline and matching
   logic, §11 milestones (work the next unchecked one), §14 decision log.
2. Never diverge from a locked decision silently. If a change is needed, add a line to the
   Decision Log in `PLAN.md` in the same commit as the code.
3. Tick milestone checkboxes in `PLAN.md` only after the milestone's tests pass and its demo is
   recorded in the Decision Log.

## Layout (see PLAN.md §4)

- `backend/` — Python 3.12, managed with `uv`; package `vidcleaner`; tests in `backend/tests`.
- `frontend/` — Vite + React + TypeScript.
- `docker/`, `docker-compose.yml`, `unraid/` — deployment.

## Commands

- Backend: `cd backend && uv sync && uv run pytest` ; dev API `uv run uvicorn vidcleaner.main:app --reload`
- Worker: `cd backend && uv run python -m vidcleaner.worker_main`
- Frontend: `cd frontend && npm install && npm run dev` (proxies `/api` to the backend); `npm test`
- Container: `docker compose up --build`

## Conventions

- ffmpeg/ffprobe are invoked via subprocess only from `vidcleaner/pipeline/`; filter graphs always go
  through a file, never inline — via `-/filter_complex <file>` (ffmpeg >= 7.0). Do **not** use
  `-filter_complex_script`: it was removed in ffmpeg 9. Use `pipeline/ffmpeg.py::FFmpegRunner`,
  which picks the right flag for the installed version.
- Every pipeline stage is a pure function of its on-disk inputs in `/work/<job_id>/` and writes a
  `<stage>.done` marker. Markers carry the code version, so an upgrade never resumes onto artifacts
  written by different code. `job.json` is artifact zero: the work dir is self-describing, so resume
  never needs the database.
- **One clock.** Every time value persisted to `subs.json`/`transcript.json`/`detections.json` is in
  *source container time*. `audio.wav` is 0-based; `pipeline/stt.py` is the only place that applies
  the audio stream's `start_time` and `pipeline/render.py` the only place that undoes it. See the
  header of `pipeline/artifacts.py`.
- Stages take the ffmpeg runner by injection and are resolved through `importlib`, so nothing on the
  ordinary import path pulls torch. `tests/unit/test_no_stt_import.py` enforces this — keep
  faster-whisper and whisperX imports inside functions.
- Persistence lives in `pipeline/persist.py` and is called *after* the pipeline, never threaded
  through `StageContext`.
- Library files are only ever changed by `pipeline/swap.py` (rename-based, never unlink).
