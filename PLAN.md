# VidCleaner — Project Plan

> **This file is the source of truth for the project.** Every work session must: read this file
> first → work the next unchecked item in §11 *Milestones* → tick it off → record any deviation in
> §14 *Decision Log*. Do not silently diverge from a locked decision (§2); add a Decision Log entry
> instead.

## 1. Context

Darick runs Jellyfin, Sonarr and Radarr in Docker on unraid (HP EliteDesk 800 G5: Intel 9th-gen
6–8 core CPU + UHD 630 iGPU, no discrete GPU) and watches on Apple TV via Infuse. Goal: automatically
remove profanity from the *audio* of movies and TV episodes, with a web UI to (a) mark series/movies
for cleaning, (b) see exactly what was removed per episode/movie (per-word counts + timestamps) and
(c) fix mistakes. Output must be a self-contained video file that plays cleanly in Infuse (Infuse
does not support EDL files), be as non-destructive as possible, use AI speech-to-text to locate the
words, mute them with ffmpeg, run in Docker, and integrate with Sonarr/Radarr/Jellyfin so new
downloads are processed without manual action.

No existing open-source project does this end-to-end (prior art: `cleanvid` = subtitles-only muting,
`swears-begone`/`monkeyplug`/`adeel-raza/profanity-filter` = Whisper CLIs; none integrate with
Sonarr/Radarr/Jellyfin or have a review UI). We build our own, borrowing their ffmpeg patterns.

## 2. Locked decisions (planning Q&A, 2026-09-01)

| Topic | Decision |
|---|---|
| Output mode | **Add a "Clean" audio track.** Video/subs/chapters/attachments stream-copied. Muted audio becomes the new *first + default* track (title `Clean`, same `language` tag as source), original audio kept as track 2 (title `Original`, default flag cleared). Written to a temp file, verified, then atomically swapped into the library; the untouched original is moved to a backup dir until purged. Fully reversible. |
| STT compute | **Local CPU only.** faster-whisper (CTranslate2 int8) + whisperX forced alignment for word boundaries. Use existing text subtitles, when present, to narrow which windows need STT. |
| Tech stack | **Python 3.12 backend (FastAPI + SQLAlchemy/SQLite + separate worker process) + React/TypeScript/Vite UI**, one Docker image; FastAPI serves the built SPA. |
| Selection | **Per-title toggle + automation.** UI lists Sonarr series and Radarr movies; user marks titles "clean". Marking a title enqueues its **existing files (backfill)**; Sonarr/Radarr webhooks enqueue new imports/upgrades. Manual "process now"/"reprocess"/"restore original" always available. |
| Word list | **Built-in categorized tiers + custom edits.** Categories (strong, mild, religious, slurs, sexual) toggled per *profile*; custom words/phrases; per-title profile override; whitelist for false positives (global/title/item). |
| Subtitles | **Redact text subtitles too** (SRT/ASS/SSA/WebVTT/mov_text embedded or sidecar → `****`). Bitmap subs (PGS/VobSub) untouched. |
| Approval | **Auto-apply, review after.** Every detection visible with a playable snippet; false positives → whitelist → reprocess. |
| Paths | **Same paths in every container.** Implement an optional prefix-mapping table (identity default). |
| Container format | Output is always **MKV** (MP4 inputs are remuxed; MP4 can't carry SRT/ASS/FLAC cleanly). |

## 3. Research findings that drive the design

**Sonarr v4 / Radarr v5**
- Webhook `eventType` values: Sonarr `Test, Grab, Download, Rename, SeriesAdd, SeriesDelete, EpisodeFileDelete, Health, ApplicationUpdate, HealthRestored, ManualInteractionRequired`; Radarr analogous with `MovieAdded, MovieDelete, MovieFileDelete`. **Import and upgrade are both `Download`**; upgrade ⇒ `isUpgrade: true` + `deletedFiles[]`. Season-pack imports fire one `Download` per episode file (burst).
- `Download` payload: Sonarr `series{id,title,tvdbId,path}`, `episodes[]{id,seasonNumber,episodeNumber,title}`, `episodeFile{id,path,relativePath,size,mediaInfo}`; Radarr `movie{id,title,year,tmdbId,folderPath}`, `movieFile{id,path,relativePath,size}`. Webhook config supports custom headers (we use one for a shared secret).
- API v3, header `X-Api-Key`. `GET /api/v3/series`, `/episode?seriesId=`, `/episodefile?seriesId=`, `/episodefile/{id}`; `GET /api/v3/movie`, `/moviefile?movieId=`, `/moviefile/{id}`. Rescan after modifying a file: `POST /api/v3/command {"name":"RescanSeries","seriesId":N}` / `{"name":"RescanMovie","movieId":N}`.
- Disk-scan semantics: a same-path replacement is re-analyzed **only if byte size changed** (adding a track guarantees this; user must have "Analyse video files" on). Extra sibling video files become "unmapped" and may be adopted as *the* file ⇒ **never leave a second video file in the folder**. Neither app deletes files during scans. `Download` fires only on import, so our own rescan does not re-trigger us (guard anyway via the idempotency tag).

**Jellyfin 10.10**
- Auth header `Authorization: MediaBrowser Token="<key>"`. Cheapest refresh: `POST /Library/Media/Updated {"Updates":[{"Path": "...", "UpdateType":"Modified|Created|Deleted"}]}` (~60 s debounce). Alternatives: `/Library/Movies/Updated?tmdbId=`, `/Library/Series/Updated?tvdbId=`, `POST /Items/{id}/Refresh`. Test: `GET /System/Info`.
- Cannot query items by provider id; filter `GET /Items?recursive=true&includeItemTypes=Movie,Episode&fields=Path,ProviderIds` client-side (only for optional deep links).
- Default audio: first `IsDefault` stream when user pref "play default track regardless of language" is set, else language score then default flag ⇒ clean track must be **first, default, and carry the original language tag**. Infuse "Auto" honors the default tag. Jellyfin has no TV multi-version grouping ⇒ another reason for single-file output.

**STT on CPU**
- faster-whisper native word timestamps: typically 100–400 ms error, biased late. **whisperX** (faster-whisper + wav2vec2 CTC alignment) runs CPU-only: ~80% of words within 50 ms on read speech, ~67% conversational, rare huge outliers ⇒ pad mutes and sanity-check against subtitle cue time.
- Speed estimates, 8-core AVX2 CPU (extrapolated, unverified): `small` int8 ≈ 4–6× real-time, `medium` ≈ 1.5–2×, `large-v3` ≈ 0.3–0.6× (2 h movie ≈ 4–6 h), `large-v3-turbo` ≈ 1.5–3×. `distil-large-v3` may lack word timestamps. ⇒ defaults: **`large-v3-turbo` for windowed passes, `medium` for full-file passes** (settings). **Subtitle windowing is essential.**
- Hallucination on music/silence (40% of non-speech clips with large-v3); Silero VAD cuts it to 0.2% ⇒ `vad_filter=True`, `condition_on_previous_text=False`, `beam_size` 1–3. faster-whisper `clip_timestamps="s1,e1,..."` transcribes only chosen windows.
- **Whisper self-censors** (`f***`, `s___`) sometimes; no reliable decoder fix ⇒ detector treats starred/underscored tokens as hits; with a subtitle word known we only need Whisper's *timing*. `initial_prompt` listing profanities is a cheap, possibly helpful hint (setting, default on).
- Correct STT times by the audio stream's `start_time` from ffprobe.
- UHD 630 via OpenVINO/Vulkan: encoder-only, unverified for Gen9 ⇒ CPU-only for v1.

**Subtitles**
- Cues are sentence-level (1–6 s) and can be offset or drift (fps mismatch ⇒ minutes). Extract: `ffmpeg -i in.mkv -map 0:s:m:language:eng out.srt`; `codec_name` `subrip/ass/mov_text/webvtt` = text; `hdmv_pgs_subtitle/dvd_subtitle` = bitmap (skip; future OCR via `pgsrip`).

**ffmpeg**
- Mute: `volume=0:enable='between(t,s,e)+...'`; `enable` gates whole frames (AAC 21 ms, AC3 32 ms) ⇒ prepend `asetnsamples=n=240` (5 ms). 80 ms lead padding makes clicks inaudible; optional 10 ms `afade` edges (cleanvid pattern) as a setting.
- Linux single-argument cap is 128 KB ⇒ always `-filter_complex_script graph.txt`.
- Filtering forces re-encode of the clean track only; `-c:v copy -c:s copy -c:a:1 copy`. `-disposition:a:0 default -disposition:a:1 0`, `-metadata:s:a:0 title=Clean language=<src>`, `-map_metadata 0 -map_chapters 0 -map 0:t?`.
- Codec policy for the clean track: AAC→`aac` at max(source bitrate, 160 k/ch-pair); AC3→`ac3 640k`; EAC3 ≤5.1→`eac3`; **DTS/DTS-HD/TrueHD(+Atmos, Atmos dropped)/any 7.1 → `flac`** (ffmpeg `dca`/`truehd` encoders experimental/limited; `eac3` can't do 7.1). Setting `clean_track_lossless=true` forces FLAC for everything. Optional extra EAC3 5.1 downmix track (setting, default off).
- QSV/VAAPI irrelevant to muting (video copied).
- QA: `volumedetect` inside mute windows ≈ −91 dB; full decode `ffmpeg -v error -i out.mkv -map 0:a:0 -f null -`; `showwavespic` for waveform PNGs.

## 4. Architecture

```
 Sonarr ──webhook(Download)──┐                          ┌── Sonarr API (series/files, RescanSeries)
 Radarr ──webhook(Download)──┤                          ├── Radarr API (movies/files, RescanMovie)
                             ▼                          ├── Jellyfin API (/Library/Media/Updated)
                    ┌──────────────────┐                │
  Browser ──HTTP──▶ │ api process      │──integrations──┘
                    │ uvicorn+FastAPI  │
                    │ serves SPA       │
                    └───────┬──────────┘
                            │ SQLite (WAL) `jobs` table = queue
                    ┌───────▼──────────┐   subprocess: ffmpeg/ffprobe
                    │ worker process   │   in-process: faster-whisper / whisperX (int8, N threads)
                    │ claim→stages     │
                    └──────────────────┘
   volumes: /config (db, settings, models cache, logs) · /media (library, same path as arrs/Jellyfin)
            /backups (originals; default inside the media share) · /work (scratch; SSD/cache)
```

- **One image, two processes** started by the entrypoint (`api` and `worker`; if either exits the container exits so Docker restarts it). Separate processes because CTranslate2/numpy hold the GIL and pin threads, which would starve the API event loop. `VIDCLEANER_ROLE=api|worker|all` allows split deployment later.
- **Queue** = `jobs` table. Worker claims with `BEGIN IMMEDIATE … UPDATE jobs SET state='probing', claimed_by=?, heartbeat=now WHERE id=(SELECT id … WHERE state='queued' ORDER BY priority, created_at LIMIT 1)`. Heartbeat every 30 s; on startup, jobs with stale heartbeat resume from the last completed stage marker. SQLite `journal_mode=WAL`, `busy_timeout=5000`.
- **Concurrency**: 1 STT job at a time; ffmpeg render of job N may overlap STT of job N+1 (setting `render_parallel`, default 1). Threads: `cpu_threads = cores − 2` for CTranslate2, `OMP_NUM_THREADS` same.
- **Resumable stages**: each stage writes its artifact + `<stage>.done` marker in `/work/<job_id>/` (`probe.json`, `audio.wav`, `subs.json`, `transcript.json`, `detections.json`, `graph.txt`, `out.mkv`). Stages are pure functions of on-disk inputs.
- **Idempotency**: output MKV global tags `VIDCLEANER=1`, `VIDCLEANER_VERSION`, `VIDCLEANER_PROFILE_HASH`, `VIDCLEANER_JOB`, `VIDCLEANER_SRC_FP` (fingerprint = sha1(first 8 MB + last 8 MB + size) of the source). Probe skips files whose tag matches the current profile hash (→ `already_clean`) unless forced. `media_items.source_fingerprint` is a fast pre-check.

### Repo layout
```
VidCleaner/
  PLAN.md  CLAUDE.md  README.md
  docker/Dockerfile  docker/entrypoint.sh  docker-compose.yml  unraid/vidcleaner.xml
  backend/
    pyproject.toml (uv)      # fastapi uvicorn sqlalchemy alembic httpx pydantic-settings structlog
                             # faster-whisper whisperx (torch cpu) pysubs2 rapidfuzz pyyaml
    vidcleaner/
      main.py                # app factory
      worker_main.py         # worker entry
      config.py              # pydantic-settings: env + /config/settings.json
      db/ models.py session.py alembic/
      api/ webhooks.py library.py items.py jobs.py wordlists.py settings.py media.py health.py
      integrations/ sonarr.py radarr.py jellyfin.py pathmap.py
      matching/ compiler.py normalize.py   # word list → regex; token normalization; censored tokens
      pipeline/ stages.py probe.py subtitles.py drift.py stt.py detect.py render.py verify.py swap.py refresh.py snippets.py codecs.py
      worker/ runner.py claim.py
      data/wordlists/{strong,mild,religious,slurs,sexual}.yaml  data/never_match.yaml
      cli.py                 # `vidcleaner clean <file> [--dry-run]`, `vidcleaner detect <file>`
    tests/ unit/ integration/ fixtures/ (CC0 speech wav + SRT, generated tone MKVs)
  frontend/ (Vite + React + TS + TanStack Query + Tailwind)  src/pages/{Queue,Library,Title,Item,Words,Settings}
```

## 5. Data model (SQLite, SQLAlchemy 2.x, Alembic)

- `settings` (key, value_json) — integration URLs/keys (secrets encrypted with a key file in /config), STT models, threads, padding, codec policy, retention, audit-pass mode.
- `path_mappings` (id, app `sonarr|radarr|jellyfin`, from_prefix, to_prefix) — empty = identity.
- `titles` (id, kind `series|movie`, arr_id, tvdb_id, tmdb_id, imdb_id, title, year, poster_url, arr_path, **enabled** bool, profile_id nullable, last_synced_at). Unique (kind, arr_id).
- `media_items` (id, title_id, kind `movie|episode`, arr_file_id, season, episode, episode_title, path, size, duration, source_fingerprint, status `untracked|pending|queued|processing|clean|already_clean|failed|stale|restored`, last_job_id, cleaned_at, updated_at). Unique (title_id, season, episode); index path.
- `jobs` (id uuid, media_item_id, trigger `webhook|backfill|manual|reprocess|audit`, priority int, state, stage, progress_pct, claimed_by, heartbeat, attempts, work_dir, source_fingerprint, stt_mode `windowed|full|audit`, model_used, subtitle_source, profile_snapshot_json, timings_json, dry_run bool, error, created_at, started_at, finished_at). Index (state, priority, created_at), media_item_id.
- `job_logs` (id, job_id, ts, level, msg) — plus a file log per job for ffmpeg stderr.
- `detections` (id, job_id, media_item_id, word_raw, word_canonical, category, start_s, end_s, mute_start_s, mute_end_s, source `subtitle|stt|both`, confidence, muted bool, whitelisted bool, suspicious bool, subtitle_cue_idx nullable, snippet_path). Index (media_item_id), (job_id, word_canonical).
- `word_entries` (id, canonical, category, forms_json, is_phrase, is_builtin, enabled); `profiles` (id, name, categories_json, extra_word_ids_json, pad_pre_ms=80, pad_post_ms=120, is_default); `whitelist` (id, scope `global|title|item`, scope_id, canonical_word, context_text nullable).
- `backups` (id, job_id, media_item_id, original_path, backup_path, size, sha1_prefix, state `kept|purged|restored|orphaned`, created_at, purge_after).
- `webhook_events` (id, source, event_type, payload_json, received_at, handled bool, job_id nullable, note).

"Count of each word" for an item: `SELECT word_canonical, category, COUNT(*), SUM(muted) FROM detections WHERE media_item_id=? AND job_id=(last_job_id) AND whitelisted=0 GROUP BY 1,2 ORDER BY 3 DESC`. Title rollups aggregate across its items' last jobs.

## 6. Job pipeline (state machine)

`queued → probing → extracting → subtitles → transcribing → detecting → rendering → verifying → swapping → refreshing → snippets → done` | `failed` | `already_clean` | `stale`. `dry_run` jobs stop after `detecting`.

0. **Ingest** (API side): store raw webhook, respond 200 immediately. Only `Download` creates work (`Test` marks integration verified; `Rename` updates paths; `*FileDelete` marks item `pending` and backup `orphaned`; `*Delete` disables title). Resolve `titles` by arr id; if not `enabled`, record only. **Dedupe** by path within 60 s (season packs). Upgrades (`isUpgrade`) supersede any running job for the item.
1. **probing** — wait for size+mtime stability (poll 5 s, 2 stable polls, cap 5 min); `ffprobe -show_format -show_streams -show_chapters -of json`; compute fingerprint; skip if tag matches (→ `already_clean`); record duration, audio streams (codec/channels/bitrate/language/start_time/default), subtitle streams; choose source audio (default stream, else first in preferred language, else first); choose clean codec (§3 policy); free-space check: `/work` ≥ 1.3× size and backup volume ≥ 1.0× size.
2. **extracting** — `ffmpeg -i in -map 0:a:<src> -ac 1 -ar 16000 -f wav audio.wav` (reused by STT, drift check, snippets).
3. **subtitles** — choose text sub: sidecar `*.<lang>.srt|*.srt|*.ass` → embedded text stream in preferred language → any embedded text stream → none. Parse with `pysubs2` (strips ASS tags). Run matcher over cue text → candidate windows `[start−1.5, end+1.5]`, merge gaps < 2 s. **Drift check**: choose 3 cues with ≥ 6 words at 10/50/90 %; transcribe each ±5 s with `small`; offset = median(STT word time − sub word time). If text similarity < 0.4 → discard subs (wrong language/episode). If |offset| > 0.7 s or spread > 0.5 s → subs unreliable for timing: widen windows to ±6 s and use the median offset for redaction alignment.
4. **transcribing** — *Windowed* (subs present): faster-whisper `clip_timestamps` over merged windows, `word_timestamps=True`, `vad_filter=True`, `condition_on_previous_text=False`, `initial_prompt` with profanity hint; whisperX align. *Full* (no/unreliable subs, or `audit`): same over the whole file with `medium`. Persist `transcript.json` (words with start/end/prob).
5. **detecting** — §7. Emit `detections.json` + DB rows. Dry-run ends here.
6. **rendering** — build `graph.txt`, run ffmpeg with `-progress pipe:1` to `out.mkv`. Redacted subtitles: extract each text stream, redact, mux back in place of the original stream preserving language/title/default; rewrite sidecar `.srt/.ass` (back up originals to `/backups`).
7. **verifying** — ffprobe `out.mkv`: audio count = original+1, video/subtitle streams identical codec/count, duration within 0.5 s, `a:0` title Clean default=1 language=src, `a:1` default=0; full decode of clean track `-f null`; `volumedetect` on up to 3 mute windows ≈ −91 dB; size ≥ 0.9× original. Re-check source path exists with same inode/size. Fail → `failed`, library untouched.
8. **swapping** — copy `out.mkv` to `<dir>/.vidcleaner.<name>.tmp` (or `rename` if /work is the same filesystem); `rename(original → backup_path)`; `rename(tmp → final_path)` (MP4 inputs get a `.mkv` name); on failure `rename(backup → original)`. Never `unlink`. `copymode` from original. Record `backups` row.
9. **refreshing** — Sonarr `RescanSeries` / Radarr `RescanMovie`; Jellyfin `/Library/Media/Updated` (`Modified`, plus `Deleted` for an old `.mp4` name). After 90 s, confirm the arr's `episodefile/moviefile` path equals ours; warn if not.
10. **snippets** — per detection: 5 s audio clips from `audio.wav` centred on the mute (`orig.m4a`) and the same with the mute applied (`clean.m4a`), plus a `showwavespic` PNG with the range highlighted. Cheap (no video). Video previews are a later option.

**Audit pass** (setting `audit_pass = off|idle|always`, default `idle`): after a windowed job completes, enqueue a `priority=low` `audit` job that runs a full-file pass with `medium` and adds any detections the subtitles missed (background/crowd lines, subs for a different cut); if new hits appear, it re-renders from the **backup original** (never re-encodes the clean track twice). Runs only when no normal jobs are queued.

**Path vanished / stale**: if the source path disappears mid-job, re-resolve via the arr API (`/episodefile/{id}`), requeue once, else `stale`. Transient API failures retry with backoff; render/verify failures are terminal until reprocess.

## 7. Detection & matching logic

- **Word list format** (`data/wordlists/*.yaml`): `- {canonical: fuck, category: strong, forms: [fuck, fucks, fucked, fucker, fuckers, fucking, fuckin, "fuckin'"], compounds: [motherfucker, motherfuckers, motherfucking, clusterfuck]}`. **Explicit inflection tables, not generic suffix rules** (generic rules yield junk and real false positives like `shitake`). Phrases: `{canonical: god damn, forms: ["god damn", goddamn, goddamned, goddammit, "god dammit"], is_phrase: true}`.
- **Never-match list** (`data/never_match.yaml`): `hello, class, classic, assassin, bass, cassette, shell, cocktail, Scunthorpe, …` — system whitelist evaluated first.
- **Compilation**: per profile, one alternation regex `\b(?:…)\b`, forms sorted longest-first, `re.IGNORECASE`; phrases allow `[\s\-']+` between words. Compounds are explicit entries, so `\b` stays on both sides always (this *is* how the Scunthorpe problem is avoided). Whitelist (global → title → item) removes canonicals or specific `context_text` matches.
- **Normalization** of STT tokens: lowercase, strip punctuation/apostrophes, collapse whitespace. Tokens with `*`/`_`/`-` runs (`f***`, `s___`, `f-ing`) are *censored*: match an enabled word by first letter + length ±1 when a subtitle hit in the window names it; without subtitle evidence mark `strong`, `confidence 0.6`, muted (setting `mute_censored_tokens`, default on).
- **Windowed matching**: for each subtitle hit, choose the STT token in the window with `rapidfuzz.fuzz.ratio ≥ 85` (or censored rule) → mute range = token `[start, end]`, `source=both`. Fuzzy matching is used only to pick the timing token, never to expand the word list. No STT token found → mute the drift-corrected proportional word span within the cue (±0.4 s), `source=subtitle`, `confidence 0.3`, `suspicious=1`.
- **Full/audit matching**: regex over the normalized transcript joined with spaces, mapping char offsets back to token indices; phrases span tokens.
- **Padding & merging**: `mute_start = start − pad_pre (80 ms)`, `mute_end = end + pad_post (120 ms)` (per profile; Whisper starts run late). Merge ranges overlapping or within 250 ms. Guards: any range > 3 s or > 2.5 s from its subtitle cue midpoint → `suspicious`, fall back to subtitle span.
- **Subtitle redaction**: same regex; replace match with `*` × len; preserve ASS override tags and timing.

## 8. Integrations

- **Webhook receivers**: `POST /api/webhooks/sonarr`, `/api/webhooks/radarr`; shared secret header `X-VidCleaner-Token` (configured in the arr's Webhook "Headers"). Setup page shows the URL + header to paste, and can create the notification via `POST /api/v3/notification` on user click.
- **Sync + backfill**: hourly and on demand: pull all series/movies into `titles`; for enabled titles pull files and enqueue any item not `clean` for the current profile hash (catch-up for missed webhooks). Enabling a title triggers immediate backfill of its files (priority below webhook jobs).
- **Post-swap**: `RescanSeries`/`RescanMovie`, then Jellyfin path refresh. Mapping verification 90 s later.
- **Path mapping** applied to every arr path → local, and local → Jellyfin path.

## 9. UI (React, minimal v1)

1. **Queue** (home) — running job (stage, %, ETA, log tail), queued list with reorder/cancel, recent done/failed with retry, integration health, disk space.
2. **Library** — tabs Series / Movies: poster, Clean toggle, profile dropdown, status (e.g. 12/24 clean), search/filter, "Sync now". Row → Title page.
3. **Title** — episode/movie list with status badge, per-word rollup, buttons: process now, reprocess all, restore originals, dry-run.
4. **Item** — summary (model, mode, subtitle source, time, codec), **counts per word/category**, detections table (time, word, category, source, confidence, suspicious) with ▶ Original / ▶ Clean snippet players + waveform, "false positive → whitelist (item/title/global) + reprocess", job log.
5. **Words & Profiles** — categories with word chips, custom words/phrases, whitelist, profile editor, default profile, padding.
6. **Settings** — Sonarr/Radarr/Jellyfin URL + key + Test, webhook URL/token, path mappings, STT models/threads, codec policy, audit pass, backup retention + purge, log level.

## 10. Docker / unraid

- `Dockerfile`: multi-stage; `node:22` builds frontend → `/app/static`; `python:3.12-slim` + static ffmpeg ≥ 7.0 (jellyfin-ffmpeg or johnvansickle build) + `uv sync --frozen`; torch CPU wheels (`--index-url https://download.pytorch.org/whl/cpu`); image ≈ 3 GB. Models download on first run to `/config/models` (`HF_HOME`).
- Entrypoint: `gosu $PUID:$PGID`, `umask 0002`, run migrations, start `api` and `worker` (exit if either dies). Env: `PUID PGID TZ VIDCLEANER_PORT=8585 VIDCLEANER_ROLE=all OMP_NUM_THREADS`.
- Volumes: `/config`, `/media` (host `/mnt/user/media`, same as arrs/Jellyfin), `/backups` (default `/mnt/user/media/.vidcleaner-backups` so swaps are same-filesystem renames), `/work` (host cache/SSD).
- `docker-compose.yml` + `unraid/vidcleaner.xml` CA-style template; healthcheck `GET /api/health`.
- Dev on the Mac: `brew install ffmpeg` (not installed yet), `uv run uvicorn --reload`, `uv run vidcleaner-worker`, `npm run dev` with API proxy; fixtures via `scripts/make_fixtures.py`. Real tests on the unraid box via compose.

## 11. Milestones (tick as completed)

- [x] **M0 — Skeleton & plan in repo**: copy this plan to `PLAN.md`, `CLAUDE.md`, `git init`, backend/frontend scaffolds, Dockerfile builds, `/api/health`, SQLite + Alembic baseline, settings load/save, api+worker entrypoint. *Demo: container runs on unraid, UI shell loads.*
- [ ] **M1 — Core clean via CLI**: word lists + matcher (tests), probe/extract/subtitles/windowed STT (faster-whisper + whisperX)/detect/render/verify on a local file; `vidcleaner clean <file> --dry-run|--out`; codec policy; subtitle redaction. *Demo: before/after MKV with Clean/Original tracks plays in Infuse; word counts printed.*
- [ ] **M2 — Full-file STT + drift + robustness**: full mode with VAD, drift check, censored-token handling, suspicious guards, resumable stage markers, eval set with precision/recall in `docs/eval.md`. *Demo: movie with no subs processed overnight; timing error report.*
- [ ] **M3 — Worker, swap, integrations**: job queue/claiming, backup/swap/rollback, Sonarr/Radarr clients + sync + backfill, webhook receivers with dedupe/upgrade handling, arr rescan + Jellyfin refresh + mapping check. *Demo: enable a series → existing episodes cleaned; Sonarr imports a new episode → auto-cleaned → Jellyfin shows Clean default.*
- [ ] **M4 — UI**: Queue, Library (toggle/profile), Title, Item (counts, detections, snippet players, whitelist + reprocess, restore original), Settings with Test buttons and webhook setup. *Demo: mark a false positive, reprocess, word audible again.*
- [ ] **M5 — Profiles, audit pass, retention, hardening**: Words & Profiles page, per-title override, audit jobs, backup retention/purge, disk guards, stale-path handling, PUID/PGID, unraid template, README, thread/model tuning. *Demo: fresh unraid install from template to first cleaned episode in < 15 min of setup.*

Later / optional: PGS OCR (`pgsrip`), video preview snippets, OpenVINO iGPU encoder, extra EAC3 downmix track, Bazarr integration to fetch subs before STT, notifications (Discord/Pushover), multi-language word lists.

## 12. Verification strategy

- **Unit**: matcher (boundaries, inflections, compounds, phrases, censored tokens, never-match, whitelist scopes), padding/merging/guards, ffmpeg graph builder (golden files), codec policy table, path mapping, webhook payload parsing (fixtures shaped per §3), job claiming/resume.
- **Integration (ffmpeg required)**: fixture MKV = CC0 speech WAV + hand-written SRT with target words + tone track + chapters; run CLI; assert `volumedetect` ≈ −91 dB inside ranges and unchanged outside; assert stream layout/titles/dispositions/language/chapters via ffprobe JSON; assert redacted subtitle text; assert MP4→MKV remux path.
- **Contract**: `respx`-mocked Sonarr/Radarr/Jellyfin; `Test`/`Download`/upgrade/season-pack flows; rescan/refresh calls with correct ids/paths; disabled-title events recorded but not queued.
- **End-to-end on unraid**: manual import of a test episode into an enabled series; confirm auto-clean, Sonarr still maps the file (size updated), Jellyfin shows two audio tracks, Infuse plays Clean by default and can switch to Original; restore original and confirm reversal; kill the container mid-render and confirm resume.
- **Quality loop**: labelled set of 5 clips with known swear timestamps; report precision/recall and mean timing error per model in `docs/eval.md` when tuning models/padding.

## 13. Risks & mitigations

| Risk | Mitigation |
|---|---|
| CPU too slow for full-file STT on movies without subs | Windowed mode default; `turbo`/`medium` models; audit passes only when idle; setting to skip full mode above N hours; future Bazarr fetch |
| Whisper misses or censors words | Subtitle candidates + censored-token rule + padding; audit pass; UI review |
| Misalignment mutes the wrong word | whisperX alignment; guards (≤ 3 s range, ≤ 2.5 s from cue); snippets for review |
| Sonarr/Radarr confused by the modified file | Same path, size changes ⇒ re-analyze; explicit Rescan; mapping check; never leave sibling video files |
| Upgrade replaces the cleaned file | `Download`+`isUpgrade` re-enqueues; old backup marked orphaned and purged per retention |
| Backups double storage | Retention (default 30 days), purge UI, backups on the same share |
| Lossless/7.1 sources | FLAC clean track; optional EAC3 downmix |
| Infuse ignores default flag on some MKVs | Clean track is also first and language-tagged; document Infuse audio "Auto" |
| Season-pack webhook bursts | Path dedupe + single-STT concurrency |

## 14. Decision log

- 2026-09-01 — Initial plan from user Q&A + research (§2, §3). Architecture review adopted: separate worker process, backfill-first, audit pass, explicit inflection tables, audio-only snippets for v1, milestone order pipeline → integrations → UI.
- 2026-09-01 — PLAN.md and CLAUDE.md written into the repo (M0 step 0). Rest of M0 (scaffolds, Dockerfile, health endpoint, DB baseline) still open.
- 2026-09-01 — **M0 complete.** `git init`; backend (FastAPI api + separate worker process, SQLAlchemy
  2.x + Alembic, settings store, CLI stub); frontend shell (Vite + React + TS + Tailwind v4 +
  TanStack Query + react-router) with the six §9 routes; `docker/Dockerfile`, `docker/entrypoint.sh`,
  `docker-compose.yml`, `unraid/vidcleaner.xml`, `README.md`. Verified: `uv run pytest` 29 passed,
  `uv run ruff check` clean, `npm test` 4 passed, `npm run build` OK; `/api/health` reports database
  revision `0001` and per-volume free disk; a settings round-trip persists with the API key
  encrypted at rest and masked on read; the worker starts, idles and exits 0 on SIGTERM; FastAPI
  serves the built SPA including deep links. **Not verified here:** the image was authored but not
  built — this Mac is arm64 and unraid is amd64. The M0 demo (`docker compose up --build` on unraid,
  UI shell loads) is still owed and its result gets appended below.
- 2026-09-01 — STT dependencies (`faster-whisper`, `whisperx`, and the torch they pull) moved to an
  `stt` optional-dependency group, excluded from the default `uv sync` and from the M0 image. §10's
  torch-CPU wheel install moves to M1, when the first code actually imports them.
- 2026-09-01 — The `0001` Alembic baseline creates the **whole §5 schema** at once rather than
  growing it milestone by milestone: it is fully specified, so this avoids migration churn through
  M1–M3. `media_items.last_job_id` and `backups.job_id` are plain columns, not foreign keys, because
  they would form a cycle with `jobs.media_item_id` under SQLite.
- 2026-09-01 — Dependencies beyond §4's list: `cryptography` (Fernet key file for secret settings,
  required by §5), `pytest-asyncio` and `ruff` (dev); frontend `react-router-dom`, `vitest` +
  `@testing-library/react`, and Tailwind v4 via `@tailwindcss/vite`. Vitest is pinned to v3 —
  v2 bundles Vite 5 and conflicts with the project's Vite 6.
- 2026-09-01 — Settings deliberately split in two: **deployment config** (paths, role, port, log
  level) resolved by `vidcleaner/config.py` as env > `<config_dir>/settings.json` > defaults, and
  **operational settings** (integration URLs/keys, STT models, padding, codec policy, retention) in
  the `settings` table via `vidcleaner/settings_store.py`. Secrets are Fernet-encrypted with
  `<config_dir>/secret.key` (0600) and the API returns `***`; writing `***` back is a no-op, so
  saving the Settings form cannot wipe a key the user never saw.
- 2026-09-01 — Added `VIDCLEANER_STATIC_DIR` (default `/app/static` in the image, `frontend/dist` in
  dev) so one FastAPI app serves the SPA in both. With no container mounts present, config/work/
  backups fall back to `.local/` in the repo root so a dev checkout runs without creating `/config`.
- 2026-09-01 — Runtime image is `python:3.12-slim-trixie` with apt's ffmpeg (7.x) rather than a
  downloaded static build; the Dockerfile asserts ffmpeg ≥ 7.0 at build time so a base-image change
  cannot silently ship an older one. `/api/health` reports `degraded` (HTTP 200) when ffmpeg is
  missing and `error` (HTTP 503) only when the database is unreachable.
- 2026-09-01 — The `vidcleaner` CLI ships only `health` in M0, on stdlib `argparse` (no typer). The
  `clean`/`detect` subcommands from §11 arrive with the M1 pipeline.
- 2026-09-01 — **M1 step 1 (word lists + matcher) complete.** Adds `vidcleaner/matching/`
  (`normalize.py`, `wordlists.py`, `compiler.py`, `profile.py`) and
  `vidcleaner/data/{wordlists/*.yaml,never_match.yaml}`. §4 named only `compiler.py` and
  `normalize.py`; `wordlists.py` (YAML loading/validation) and `profile.py` (the DB bridge) are
  additions. 180 entries / 472 forms; 120 entries active in the default profile. Verified:
  `uv run pytest` 469 passed, `uv run ruff check` and `ruff format --check` clean,
  `uv run vidcleaner words` reports the false-positive gate green.
- 2026-09-01 — **§7's `\b(?:…)\b` is wrong and is replaced by `(?<!\w)(?:…)(?!\w)`.** `\b` after an
  apostrophe requires a following word character, so `re.compile(r"\b(?:fuckin')\b")` does **not**
  match `"he was fuckin' tired"` — and the M1 test media (PLURIBUS S01E01) contains `friggin'`.
  The asymmetric lookarounds still reject every classic substring case.
  `test_pattern_uses_no_word_boundary_escape` guards against reintroducing `\b`.
- 2026-09-01 — **§7's phrase separator `[\s\-']+` is replaced by `[ \t\xa0\-'’]{1,3}`.** `\s`
  includes `\n`, so `god damn` matched across a subtitle line break in
  `"oh my god\ndamn that hurt"`. No newline, and bounded length.
- 2026-09-01 — **`never_match.yaml` re-scoped.** §7 attributes it to the Scunthorpe problem, but §7's
  own compound argument is sufficient and verified: with the corrected boundaries plus explicit
  `compounds` entries, an 84-word innocent corpus (Scunthorpe, assassin, class, bass, cassette,
  shell, cocktail, hello, Uranus, shiitake, …) produces zero matches against the widest possible
  matcher with **no** help from the file. Its actual jobs are (a) the censored-token hyphen branch,
  where `x-ray`/`e-mail`/`t-shirt` have exactly the shape of `f-ing` and no regex can separate them,
  (b) the rapidfuzz timing selector, where `ratio("shit","shirt")=88.9` clears the ≥85 bar, and
  (c) that corpus, as a CI build gate.
- 2026-09-01 — **`.` is not treated as a mask character** in censored-token detection. Doing so
  classified the ubiquitous subtitle ellipsis ("That...", "I...", both in the test episode) as
  censored. Masking is recognised only from `* _ # @ $` and runs of two or more hyphens.
- 2026-09-01 — **Whitelisted canonicals stay compiled into the pattern**; hits are recorded with
  `whitelisted=1, muted=0` rather than being omitted. §5's own rollup query filters
  `WHERE whitelisted=0`, which only makes sense if such rows exist, and §9.4's review UI needs them
  to offer un-whitelisting. Whitelist scopes are a **union**, not the override chain §7's
  "global → title → item" implies: the schema has no negative form, so a narrower scope can only add
  suppression. A `mode` column in M5 would be needed for true override semantics.
- 2026-09-01 — **`compounds:` in the YAML is authoring sugar**; the loader flattens each into its own
  top-level entry (same category, `parent` kept in memory for the Words UI). Required so
  `motherfucker` rolls up separately from `fuck` in §5's per-word counts and can be whitelisted
  independently. Also a new optional phrase field **`focus:`** — `son of a bitch` matches as a
  phrase but mutes only the `bitch` token instead of ~1.2 s of dialogue.
- 2026-09-01 — **Precision lives in per-entry `enabled: false` + a mandatory `note`, not in category
  toggles** (validation rejects a disabled entry with no note). 50 of 180 entries ship off,
  including `cock`, `queer`, `nip`, `cracker`, `balls`, `snatch`, `hoe`, `bloody` and the clinical
  anatomy terms. The default profile is **`strong` + `slurs` + `sexual` + `religious`; `mild` is
  off** (damn/hell/crap roughly triple the cuts for words most viewers accept). Bare `god`, `jesus`
  and `christ` ship **enabled** — user decision, because the episode's standalone "Jesus." and
  "Christ, no." are the reason to enable `religious` at all, and leaving them off made the category
  nearly inert. The cost is reverent false positives ("thank God"), to be handled by a per-title
  whitelist in M4.
- 2026-09-01 — **`VIDCLEANER_PROFILE_HASH` is per media item, not per profile.** Item- and
  title-scoped whitelist rules feed it, so that adding an item whitelist and hitting Reprocess is
  not short-circuited to `already_clean`. Format `v<algo>:<sha1[:16]>`; the STT model and codec
  policy are deliberately excluded (including the model would invalidate every cleaned file the
  first time someone compared `medium` against `turbo`, and §4 calls this a *profile* hash).
  `ALGO_VERSION` is the escape hatch for "the matcher changed but the data did not".
- 2026-09-01 — Word-list seeding runs at startup after `upgrade_to_head`, not from an Alembic
  migration (data seeding inside migrations ages badly). `sync_builtin_word_entries` never writes
  `enabled` back to an existing row, so a user's choice survives upgrades; the api owns seeding
  whenever it runs and a worker-only role does it instead, which keeps the two processes from racing.
- 2026-09-01 — **M1 step 2 (work dir, artifacts, stage driver) complete.** `pipeline/` gains
  `workspace.py` and `artifacts.py` beyond §4's file list, plus `stages.py`. Three conventions the
  §4/§6 text implies but does not state: (a) **`job.json` is artifact zero** — CLAUDE.md's "pure
  function of its on-disk inputs" requires the job's own parameters to be on disk too, so resume
  never needs the database; (b) **stage markers carry `vidcleaner.__version__`** and `is_done()`
  returns False on a mismatch, so a code upgrade cannot resume onto artifacts written by different
  code; (c) artifacts are written atomically (temp file + `os.replace`), because a half-written
  artifact beside a completed marker would be read back as if it were whole. Secrets are stripped
  from the settings snapshot in `job.json` — `/work` ends up in bug reports.
- 2026-09-01 — Artifacts are **pydantic models**, not dataclasses: they are read back after a crash,
  possibly by code of a different version, so validation at the deserialization boundary is the
  point, and they become M4 FastAPI `response_model`s with no adapter. Plain dataclasses are kept
  for values that are never serialized (`StageContext`, `Workspace`). `artifacts.Detection` mirrors
  `db.models.Detection` field-for-field minus `job_id`/`media_item_id`, asserted by
  `test_detection_mirrors_the_database_columns`, so M3's `persist.py` is a mechanical copy.
- 2026-09-01 — **The "one clock" invariant**, documented at the top of `artifacts.py`: every time in
  `probe.json`/`subs.json`/`transcript.json`/`detections.json` is in *source container time*;
  `audio.wav` is 0-based; `stt` is the only module that applies the audio stream's `start_time`
  (recording it in `Transcript.audio_start_offset_s`) and `render` the only one that converts back.
  §3 says only "correct STT times by the audio stream's `start_time`"; naming the invariant matters
  because a sign error here mutes the wrong second of every file and no unit test catches it — the
  runtime tripwire is verification's mute-window and control-window `volumedetect` checks.
- 2026-09-01 — `stages.py` resolves stage modules through `importlib` and takes the ffmpeg runner by
  injection rather than importing `pipeline.ffmpeg`/`pipeline.stt` at module scope, so the package
  (and M4's API through it) imports on a checkout with no torch. `tests/unit/test_no_stt_import.py`
  asserts that boundary for `pipeline`, `stages`, `artifacts`, `matching.compiler`, `main` and `cli`.
- 2026-09-01 — **M1 step 3 (ffmpeg layer + fixtures) complete.** Adds `pipeline/ffmpeg.py` (beyond
  §4's list), `scripts/make_fixtures.py`, `tests/integration/`, and an `ffmpeg` pytest marker
  auto-applied by directory. `VIDCLEANER_TEST_REQUIRE_FFMPEG=1` turns the skips into failures so CI
  cannot go green by skipping the whole tier. Verified: 600 passed, 26 of them against real media.
- 2026-09-01 — **§3's "always `-filter_complex_script graph.txt`" is wrong: that option no longer
  exists.** Measured on the installed ffmpeg 9.0.1: `Unrecognized option 'filter_complex_script'`.
  The replacement is the generic read-option-from-file syntax **`-/filter_complex graph.txt`**,
  added in 7.0 and working on 7.x and 9.x; `get_caps()` version-gates it. **CLAUDE.md's conventions
  section has been updated accordingly** — it named the removed flag. Filter graphs still always go
  through a file, and for the stated reason: a graph measured 143 KB at ~1600 mute ranges, past the
  128 KB single-argv cap.
- 2026-09-01 — **§3's unbounded `enable='between(...)+between(...)'` cannot work for a feature
  film.** `av_expr_parse` has a hard depth budget of 100 `+` terms; measured, 100 parses and **101
  fails** with "Error when evaluating the expression". Mute ranges are therefore chunked across
  chained `volume` filters. Two integration tests pin the boundary (≤100 parse, 101 raises) so a
  future ffmpeg that changes the budget is caught rather than silently mis-rendering.
- 2026-09-01 — **§3's "optional 10 ms `afade` edges" silences the rest of the file if implemented as
  written.** `afade=t=out` holds its output at zero after its window, so a chain of out/in pairs
  mutes everything from the first fade onward — measured `silence_start: 0, silence_duration: 6` on
  a 6 s clip, and every structural check still passes. Each `afade` must carry its own timeline
  `enable=`. Both the broken and the fixed behaviour are asserted, so the gate is demonstrable
  rather than superstition.
- 2026-09-01 — `asetnsamples` needs **`:p=0`**; `pad` defaults to true and zero-pads the final frame
  with up to 239 samples. Measured on a real AC-3 bitstream, the reframing is what §3 claims: a
  nominal `[2.000, 3.000]` mute lands at `[1.0098, 2.0197]`-style offsets without it and within
  ~2 ms with it. Also confirmed `volume=0` measures exactly −91.0 dB — but assertions use
  `max_volume ≤ −80 dB`, since −91 is the s16 quantization floor and shifts under FLAC and lossy
  ringing.
- 2026-09-01 — `volumedetect` prints its statistics **twice**: once at graph-configuration time with
  `n_samples: 0`, then for real. The parser takes the last block with a non-zero sample count. Also:
  `-af` cannot be combined with a `-filter_complex` output label, so measurement filters go inside
  the graph when one is in use and via `-af` only when it is not.
- 2026-09-01 — Test media is generated by `scripts/make_fixtures.py` from ffmpeg's own sources (no
  network, no committed binaries); only three small text inputs are committed. Two cases are covered
  by unit tests over ffprobe JSON instead of generated media: DTS/TrueHD sources (ffmpeg's `dca` and
  `truehd` encoders are experimental, so generating them would test ffmpeg) and bitmap subtitles
  (ffmpeg refuses text-to-bitmap subtitle transcoding, so PGS/VobSub cannot be synthesized without
  committing binary media). Audio is a steady 1 kHz sine so that "silent inside the range, unchanged
  outside" is unambiguous.
- 2026-09-01 — **M1 step 4 (codec policy + graph builder) complete.** Adds `pipeline/codecs.py` and
  `pipeline/graph.py` (the latter beyond §4's list), both pure functions with golden tests and no
  ffmpeg dependency, plus an integration tier that feeds the generated graphs to a real ffmpeg.
  Verified: 781 passed.
- 2026-09-01 — **AC-3 stereo deviates from §3's flat 640k.** §3 says "AC3→`ac3 640k`", which for a
  two-hour *stereo* track is 576 MB against 173 MB at 192k for no audible gain — and this file is
  added to every episode in the library. Stereo AC-3 now tracks the source bitrate with a 192k floor
  and a 640k ceiling; **3+ channels keep 640k exactly as §3 specifies.**
- 2026-09-01 — Mute ranges are chunked across chained `volume` filters at 90 terms each, with an
  outer `if(between(t,chunk_start,chunk_end),…,0)` guard so that outside a chunk's own span ffmpeg
  evaluates one `between` rather than ninety. Chaining was chosen over parenthesised grouping inside
  a single expression: no depth arithmetic, and each filter simply multiplies the gain by 0 or 1.
  Verified against real ffmpeg at 0/1/89/90/91/200/1000/1999 ranges (1999 ranges = 23 chunks, 50 KB
  of graph text).
- 2026-09-01 — Graph times are fixed-point milliseconds, **rounded outward** (start down, end up), so
  quantization can only lengthen a mute and never leak a syllable; and never scientific notation,
  which ffmpeg's expression parser would not accept. A `MAX_RANGES = 2000` guard raises rather than
  emitting a pathological graph — that many ranges is a detector fault, not a very profane film.
- 2026-09-01 — Two bugs found by the tests rather than by review, both in `pipeline/ffmpeg.py`:
  `FFmpegError` assigned `self.args`, which is a `BaseException` slot, replacing the tuple `str(exc)`
  is derived from and discarding the formatted message (renamed to `argv`); and `run()` drained
  stdout inline, which blocks until EOF and made `process.wait(timeout=...)` unreachable, so a hung
  ffmpeg would never have been killed (stdout now has its own pump thread).
- 2026-09-01 — **M1 step 5 (probe, extract, subtitles) complete.** Adds `pipeline/probe.py`,
  `extract.py`, `subtitles.py` and `lang.py`. Verified: 852 unit + 59 integration tests pass, and
  the three stages run end to end on the real 4.26 GiB test episode.
- 2026-09-01 — **§3's "subtitle windowing is essential" is confirmed with numbers.** On PLURIBUS
  S01E01 (56:28, 540 English cues) the matcher finds 44 hits, which become **28 windows covering
  186 s of 3388 s — 5.5% of the runtime**. Windowed STT therefore transcribes about 3 minutes of
  audio instead of 56, an ~18x reduction, which is what makes CPU-only STT viable at all.
  Detected words: fuck 18, god 10, shit 8, goddamn 3, bullshit 2, jesus 2, christ 1.
- 2026-09-01 — **Only subtitle streams in a language we ship a word list for are redacted.** The
  test episode carries **61** text subtitle streams and exactly **one** is English; extracting and
  redacting the other 60 would be pure waste, since no word list can match them. Streams with no
  `language` tag are skipped rather than guessed at — the same file has three (Chinese, titled but
  untagged). Multi-language word lists remain a §11 "later" item.
- 2026-09-01 — Subtitle windows pad the **cue** bounds by ±1.5 s rather than the hit's proportional
  span: the proportional estimate can be wrong within a long cue, and a window that is too narrow
  loses the word entirely. Hit *time spans* are still derived from character offsets, which keeps a
  match near the end of a long cue near the end of its time range.
- 2026-09-01 — Redaction splices into `SSAEvent.text` through a visible-character offset map, because
  `pysubs2`'s `plaintext` **setter strips ASS override tags**. Markup outside a match is preserved
  exactly; a word split across a tag boundary (`f{\i1}uck`) is still matched and masked, which
  necessarily destroys the override block inside the span — that is the right trade for a profanity
  filter, and it is counted in `tags_dropped` rather than happening silently. `\N` is not a phrase
  separator here either, so `god\Ndamn` redacts to `***\Ndamn`, consistent with detection.
- 2026-09-01 — Confirmed the ONE CLOCK premise empirically: extracting a stream whose `start_time`
  is 0.5 s yields a WAV of exactly 10.000 s, not 10.5 s — ffmpeg drops the offset rather than
  padding. So `wav_time = container_time − start_time`, and `stt` adds it back exactly once.
- 2026-09-01 — **M1 step 6 (detector) complete.** `pipeline/detect.py` is a pure function of
  already-loaded values, so the whole detector is unit-testable with no ffmpeg, no torch and no
  database. Verified: 912 passed.
- 2026-09-01 — **§7's fuzzy timing selector must score against the entry's whole form table, not the
  matched surface string.** `fuzz.ratio("fuck", "fucking")` is 72.7, below the ≥85 bar, so a
  subtitle hit on `fuck` would silently lose its timing to Whisper's `fucking` and fall back to the
  0.3-confidence proportional span. Every comparison target is already an authored form, so this
  widens *timing* recall without widening the word list.
- 2026-09-01 — Two orderings inside the detector are load-bearing and each has a regression test:
  **censored tokens resolve before fuzzy matching** (`fuzz.ratio("f***","fuck")` is ~50, so a
  fuzzy-first implementation loses the pairing), and **guards run before padding** (otherwise 200 ms
  of padding tips a legitimate 2.9 s span over the 3 s limit and flags it for nothing).
- 2026-09-01 — Extension to §7, at no cost: within the windows already transcribed, the matcher is
  also run over the STT tokens themselves, emitting `source="stt"` detections. Whisper regularly
  hears a word the subtitles sanitised (fan subs, or subs cut for TV) and the audio is already
  transcribed. Deduplication is by time overlap **plus a related canonical** — overlap alone would
  let a wide subtitle fallback span swallow a different word spoken beside it, while canonical-only
  matching let a partially located `god damn` phrase double-count as a bare `god`.
- 2026-09-01 — **A punctuation-only token now emits a hard break in the joined transcript.** Words
  join with a single space, but `join_tokens` puts `"\n"` where punctuation was, because the phrase
  separator excludes it. Without that, `"Oh my God. Damn."` joined to `"god damn"` and matched the
  phrase across a sentence boundary — the same defect as §7's `[\s\-']+`, arriving via punctuation
  rather than a line break.
- 2026-09-01 — **One STT token supplies timing for at most one subtitle hit.** Two hits in the same
  cue ("Bull. Shit.") otherwise both claimed the nearest token, leaving the second word unmuted and
  producing a spurious STT-only duplicate.
- 2026-09-01 — Whitelisted words are marked `whitelisted=1, muted=0` by the detector, since
  `find_hits` deliberately does not consult the whitelist (§5's rollup needs the rows to exist).
  `Matcher.is_suppressed(canonical, context)` exists for callers holding a serialized
  `SubtitleHit` rather than a live `Match`.
- 2026-09-01 — A masked token is only named when the revealed letters pin it down: `f***` stays
  `<censored>` (it fits fuck, fag and faggot) while `f**k` and `b*tch` resolve uniquely. §7 mutes
  either way at 0.6 confidence — the mask itself is unambiguous evidence that something was censored.
- 2026-09-01 — **M1 step 7 (render + verify) complete.** Adds `pipeline/render.py` and
  `pipeline/verify.py`. `build_render_command` and `structural_checks` are pure and golden-tested,
  because output stream-index arithmetic is the likeliest bug in the milestone. Verified: 1006
  passed, 88 of them against real media, including a full render of the generated fixture with the
  requested ranges measured silent and the rest measured unchanged.
- 2026-09-01 — **§3's `-disposition:a:1 0` is destructive and is replaced by the subtractive
  `-disposition:a:1 -default`.** A literal `0` zeroes the whole disposition bitmask, wiping
  `comment`, `original`, `hearing_impaired` and `dub` from the user's original track. Also, `0` is
  the wrong ordinal in general: `-map 0:a` preserves source order, so the cleaned track lands at
  output `a:(1+K)` for source ordinal `K`, and every source track that carried `default` must have
  it cleared — not just `a:1`.
- 2026-09-01 — **§3's `-c:s copy` fails outright for MP4 sources**: `mov_text` cannot be muxed into
  Matroska ("Could not write header"), so it is transcoded to `srt` per stream position. Verified by
  rendering the MP4 fixture through to a passing verify.
- 2026-09-01 — A `-map [label]` stream inherits **no** metadata or dispositions, and `-map_metadata 0`
  copies global tags only, so the clean track's `title` and `language` are set explicitly. The clean
  track mirrors the source language including its **absence** — asserting a language we do not know
  is worse for Jellyfin and Infuse than leaving it unset. Redacted subtitles arrive as file inputs
  and therefore also need language, title and dispositions restored.
- 2026-09-01 — **Sidecar subtitles are written to `/work` only, never the library.** §6 step 6 says
  rendering rewrites them, but step 7 promises "Fail → `failed`, library untouched", and CLAUDE.md
  reserves library writes for `swap.py`. M3's `swap.py` installs them in the same rename-only
  transaction as the video. Redaction itself happens in Python *before* ffmpeg runs, so a subtitle
  failure downgrades to "copy the original stream and warn" and can never fail a render.
- 2026-09-01 — Verification asserts `max_volume ≤ −80 dB` inside mute windows rather than
  `mean ≈ −91 dB`: −91 is the s16 quantization floor and shifts under FLAC (s32) and lossy ringing.
  Windows are inset **40 ms**, because a nominal `[3.000, 3.200]` mute measures as
  `[3.0056, 3.2101]` after a real AC-3 round trip — the MDCT window smears the edges by about one
  frame, so asserting at the nominal boundary would flake.
- 2026-09-01 — **The control-window check is the most important assertion in the suite.** Every other
  check passes on a file whose audio has been silenced end to end, which is exactly what an ungated
  `afade` chain, a mis-signed time offset, or an `enable` expression that evaluates true everywhere
  all produce. `verify` therefore also measures ≥1 s of audio clear of every mute and requires
  ≥ −50 dB. A test deliberately silences a whole render and asserts that this check — and only
  really this check — catches it.
- 2026-09-01 — The §4 idempotency loop is closed and tested: re-probing our own output with the same
  profile hash reports `already_clean`.
- 2026-09-01 — **M1 step 8 (STT) complete.** Adds `pipeline/stt.py` (protocol, `ScriptedTranscriber`,
  the stage) and `pipeline/whisper_backend.py` (real faster-whisper + whisperX, every heavy import
  inside a function). Verified: 1041 passed, and a real windowed transcription ran end to end on a
  60 s clip cut from the M1 test episode.
- 2026-09-01 — **§10's CPU-only torch is now actually enforced, and it needed more than an index
  pin.** `[tool.uv.sources]` only applies to *direct* dependencies, but torch arrives transitively
  via whisperX, so `torch`/`torchaudio` are named explicitly in the `stt` extra and pinned to
  `https://download.pytorch.org/whl/cpu` with `marker = "sys_platform == 'linux'"` (that index
  carries no macOS wheels, and macOS has no CUDA build to avoid). Result: the lock went from **34
  `nvidia-*-cu12` packages to zero**, and `torchvision`/`torchcodec` dropped out too. Linux resolves
  `torch 2.14.0+cpu`; macOS resolves from PyPI. The relock also moved whisperX 3.8.6 → 3.7.2 to
  satisfy the constraint. The Dockerfile now passes `--extra stt` on both `uv sync` lines.
- 2026-09-01 — `ScriptedTranscriber` ships in production code, not in `tests/`: it backs
  `--transcript foo.json` (re-run detect and render without repeating a 40-minute STT pass) and it
  is what lets the whole integration tier exercise the pipeline with no torch installed. It filters
  by window rather than ignoring windows, so the windowing logic stays exercised.
- 2026-09-01 — whisperX alignment is **optional at runtime**. If the import fails or its API has
  moved, the faster-whisper timings are kept with `aligned=False` and the detector's guards absorb
  the extra slop — losing timing accuracy is much better than failing the job. A test simulates the
  missing import and asserts the degradation.
- 2026-09-01 — §3's VAD claim is confirmed by test: with `vad_filter=True`, a pure 1 kHz tone
  produces **zero** words, so Silero VAD does suppress the hallucination Whisper otherwise emits on
  non-speech. `Transcript.dropped_out_of_window` guards `clip_timestamps`, whose semantics have
  shifted between faster-whisper releases; it is asserted to be 0.
- 2026-09-01 — **Real-media STT results (60 s clip, 8 subtitle hits, 2 windows / 29 s).**
  `base`: 20 words, 5 exact pairings, 4 suspicious. `large-v3-turbo` (the shipped default):
  27 words, 6 exact pairings, only 2 suspicious, and it additionally caught a `fuck` the subtitles
  had omitted. Both runs exercised the guards for real — whisperX returned one 4.1 s span for a
  single word, which the >3 s guard rejected in favour of the subtitle span exactly as §7 intends.
  This is the concrete evidence that the guards are load-bearing, not decorative.
- 2026-09-01 — **M1 step 9 (CLI + persistence) complete.** Adds `vidcleaner/cli_clean.py` and
  `pipeline/persist.py`, plus `vidcleaner clean` / `detect`. Verified: 1088 passed, and the full
  `clean` ran on a real 60 s clip from the test episode — 26/26 verify checks, mute windows measured
  at −90.3 dB and the control window at −9.7 dB, 8 subtitle words masked, output layout `a:0` Clean
  eac3 5.1 eng default / `a:1` Original, all five `VIDCLEANER*` tags present.
- 2026-09-01 — **M1's CLI does write `jobs` and `detections` rows** (user decision), but persistence
  lives in `pipeline/persist.py` and is called by the CLI *after* the pipeline, never threaded
  through `StageContext` — CLAUDE.md requires stages to be pure functions of their on-disk inputs,
  and a `Session` in the context would make every stage test need a migrated database. The
  non-null FK chain (`detections → jobs → media_items → titles`) is satisfied by a **sentinel
  title**: `kind="movie"`, `arr_id=-1` (negative, so it can never collide with a real Sonarr id),
  `enabled=False` (so M3's backfill ignores it). `media_items` rows are keyed on `path` with
  `season`/`episode` NULL, which SQLite treats as distinct under `uq_media_items_title_s_e`.
  **M3 owes a reconciliation step**: when a real arr file matches a local row's path it must adopt
  that row rather than inserting a duplicate.
- 2026-09-01 — **§5's rollup query needs a cast.** As written, `SUM(muted)` returns `True` rather
  than a count: `muted` is a Boolean column, so SQLAlchemy applies the Boolean result processor to
  the aggregate. M4's per-word rollup must use `func.sum(cast(Detection.muted, Integer))`. Asserted
  in `test_the_rollup_query_from_plan_section_five_works`.
- 2026-09-01 — Two bugs the CLI work exposed. First, `configure_logging` bound `sys.stderr` at
  configure time and `cache_logger_on_first_use` kept it forever, so reconfiguring while anything
  had replaced the stream (pytest's `capsys`, or the CLI's own call) left the cached logger writing
  to a closed file — surfacing much later as `ValueError: I/O operation on closed file` in eight
  unrelated tests. The factory now resolves `sys.stderr` per call. Second, the CLI never called
  `configure_logging` at all, so structlog's default factory wrote to **stdout** and corrupted
  `--json`.
- 2026-09-01 — `--detections FILE` enters the pipeline at `render` but still runs `probe` and
  `subtitles`: render needs `probe.json` for the plan and `subs.json` for the redaction list, and
  neither stage needs STT. `render` also tolerates a missing `subs.json` by skipping redaction
  rather than failing an otherwise good render. This flag is the mechanism behind M4's "reprocess
  after a whitelist edit".

## 15. Working agreement for future sessions

1. Read `PLAN.md` §2 (locked decisions) and §11 (next unchecked milestone) before coding.
2. Work one milestone at a time; update checkboxes and the Decision Log in the same commit as the code.
3. Any new external dependency, schema change, or change to the output-file layout gets a Decision Log line.
4. Run `uv run pytest` and `npm test` before ticking a milestone; record the milestone demo result in the Decision Log.
