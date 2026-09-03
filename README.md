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

**All six milestones are complete**: M1 (core clean via CLI), M2 (full-file STT, drift and
evaluation), M3 (job queue, backup/swap, Sonarr/Radarr/Jellyfin), M4 (the web UI), M5
(profiles, the audit pass, retention and the install path below) and M6 (OCR for bitmap
subtitles). The pipeline runs end to
end on a real file:

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

Subtitles are used to narrow which parts of the audio need speech recognition, which is what
makes CPU-only STT practical — about 5% of an episode's runtime on the test media. Before they
are trusted, a **drift check** samples three cues across the file and measures how far they sit
from the audio, so a subtitle track authored for another frame rate or another cut cannot mute
the wrong second. A file whose only subtitles are **bitmap** — the usual case for a Blu-ray
remux — has its PGS track read by OCR, which costs about 13 seconds for a 56-minute episode and
keeps it on the cheap windowed path. OCR is treated as a lead, not a witness: it decides which
audio to transcribe and never mutes a word speech recognition did not also hear. A file with
**no usable subtitles at all** still falls back to transcribing the whole thing (`--stt-mode
full`, or automatically), guarded by `stt_full_max_hours` so a long film cannot occupy the
worker all night.

Detection quality is measured rather than asserted: see [docs/eval.md](docs/eval.md).

Nothing has to be driven by hand. Mark a series or movie **Clean** in the Library page and its
existing files are queued immediately; Sonarr/Radarr webhooks queue new imports and upgrades as
they land; the worker claims one job at a time, swaps the result into place by rename with an
fsynced journal, keeps the original under `/backups`, and tells the arrs and Jellyfin the file
changed. The **Queue** page shows the running stage and log, the **Item** page shows every word
that was removed with a five-second *Original* and *Clean* clip for each, and a false positive can
be whitelisted (this file / this title / everywhere) and the file reprocessed from that page.

Cleaned files are re-checked in the background: when the queue is idle the **audit pass**
transcribes the whole file from the kept original — subtitles narrow the first pass, so they
can also hide a word nobody wrote down — and re-renders only if that finds something new.
Originals are purged on a retention clock you set, the **Words & Profiles** page controls what
gets muted, and a series or movie can override the profile it uses.

## Security

**VidCleaner has no user authentication.** Anyone who can reach the port can browse
your library, queue work and read the webhook token. Put it behind a reverse proxy with
authentication, or keep it on a trusted network — the same posture as an unprotected
Sonarr.

The webhook receiver *is* authenticated: it requires the `X-VidCleaner-Token` header on
every delivery, the token is generated on first boot rather than left empty, and a bad
token gets a 401 with nothing written to the database.

## Install

> **The image is not published to a registry yet**, so building it locally is the only
> supported path today. `unraid/vidcleaner.xml` is a working Community Applications
> template except for its `Repository`/`Support`/`Project`/`Icon` URLs, which are
> placeholders until this repository has a home.

```bash
git clone <this repo> && cd VidCleaner && docker compose up --build -d
```

Then open `http://<host>:8585`.

### Before you start

Three things decide whether this works at all:

| | Why it matters |
|---|---|
| **`/media` must be the same path Sonarr, Radarr and Jellyfin use** | VidCleaner replaces files in place. If the paths differ, configure a mapping in Settings → Path mappings; if they differ *and* you skip that, the arrs will not find the file afterwards. |
| **`PUID`/`PGID` must own the media share** | The swap is a rename inside the library folder. The entrypoint warns on startup if `/media` is not writable, so check the container log first when a job fails at `swapping`. |
| **`/backups` should be on the media share** | Then the swap is a same-filesystem rename rather than a copy. A cross-device backup is refused by default (`allow_cross_device_backup`), because it could not be done without deleting an original. |

`/work` wants an SSD or cache pool and about 1.3x your largest file free; the worker
pauses the queue rather than failing jobs when it drops below `min_free_gib`.

### First run, in order

1. **Settings → Sonarr / Radarr / Jellyfin.** Paste each URL and API key and press
   **Test**. Jellyfin is optional.
2. **Settings → Webhooks.** Press **Add to Sonarr** (and Radarr) to create the
   notification, or copy the URL and `X-VidCleaner-Token` header in by hand. This is
   what makes new downloads clean themselves.
3. **Settings → Path mappings**, only if your containers disagree about paths.
4. **Library → Sync now**, then toggle **Clean** on one series or movie. Its existing
   files are queued immediately.
5. **Queue** shows the running stage. **The first job is much slower than the rest**: it
   downloads a speech model (~1.5 GB) into `/config/models`. A 45-minute episode with
   usable subtitles takes a couple of minutes after that; one without subtitles is
   transcribed whole and takes considerably longer (capped by `stt_full_max_hours`).
6. **Item** page: every word removed, with a five-second *Original* and *Clean* clip.
   Wrong one? Whitelist it (this file / this title / everywhere) and press reprocess.

Nothing is destroyed at any point: the original audio stays in the file as track 2 and
the untouched source file is kept under `/backups` until retention purges it.

### Configuration: env vs. the web UI

Deployment settings are environment variables, read once at start-up. Everything
operational lives in the web UI and takes effect on the next job.

| Env var | Default | Notes |
|---|---|---|
| `PUID` / `PGID` | `99` / `100` | unraid's `nobody:users`. Must own the media share. |
| `UMASK` | `0002` | Keeps the group bit so the arrs can still manage what we write. |
| `TZ` | `Etc/UTC` | |
| `OMP_NUM_THREADS` | `nproc - 2` | **Set it here, not in the UI**: the threading library reads it once at process start. Leave it unset unless you want a different number. |
| `VIDCLEANER_ROLE` | `all` | `api` / `worker` to split across hosts. |
| `VIDCLEANER_LOG_LEVEL` | `INFO` | |
| `VIDCLEANER_PORT` | `8585` | |

In the UI: STT models, CPU threads, beam size, mute padding, codec policy, the audit
pass, backup retention, and the disk floor.

### Tuning

Defaults are `large-v3-turbo` for subtitle-narrowed passes and `medium` for full-file
ones, measured in [docs/eval.md](docs/eval.md). On an 8-core CPU the windowed default
runs at roughly 2.8x real time, so most episodes cost a couple of minutes of
recognition. Worth knowing before changing anything:

- **CPU threads** defaults to cores − 2, leaving room for the API and ffmpeg. More is
  not reliably faster.
- **A bigger full-file model is the expensive choice**, not the windowed one — full
  passes cover the entire runtime instead of ~5% of it.
- **The audit pass** (`idle` by default) re-checks cleaned files against the kept
  original with a full-file pass when the queue is empty, and re-renders only if it
  finds something new. Set it to `off` if you would rather not spend the CPU.

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

Bitmap-subtitle OCR is a second extra: `uv sync --extra ocr` plus the tesseract binary
(`brew install tesseract`; the image installs `tesseract-ocr` and `tesseract-ocr-eng` from
Debian). Without it a PGS-only file simply falls back to a full-file transcription, exactly as
it did before M6.

Both tiers skip themselves when their tool is missing, which is convenient locally
and dangerous in CI — so set the matching flag there and a missing tool fails the
run instead:

```bash
cd backend && VIDCLEANER_TEST_REQUIRE_FFMPEG=1 VIDCLEANER_TEST_REQUIRE_OCR=1 uv run pytest
```

## The image

Four volumes: `/config` (database, settings, STT models, logs), `/media` (the library),
`/backups` (originals) and `/work` (scratch). See **Install** above for what each one
needs. Override the host paths with `MEDIA_DIR`, `BACKUPS_DIR`, `CONFIG_DIR` and
`WORK_DIR` when running compose outside unraid.

About 3 GB (Debian trixie + ffmpeg 7.x + CPU-only torch), built and exercised for both
`linux/amd64` (unraid) and `linux/arm64`. One image runs both processes and exits if
either dies, so Docker's restart policy brings the pair back;
`VIDCLEANER_ROLE=api|worker|all` splits them across hosts. `GET /api/health` backs the
healthcheck and reports the database revision, ffmpeg version and free disk per volume.
