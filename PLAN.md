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
      matching/ compiler.py normalize.py wordlists.py profile.py
                                     # word list → regex; token normalization; censored tokens;
                                     # YAML loading/validation; the DB bridge
      pipeline/ workspace.py artifacts.py stages.py    # job dirs, typed artifacts, resumable driver
                ffmpeg.py graph.py lang.py             # subprocess layer, filter builder, ISO 639
                probe.py extract.py subtitles.py codecs.py
                stt.py whisper_backend.py              # lazy-import boundary for torch
                detect.py render.py verify.py persist.py
                drift.py                               # M2
                swap.py refresh.py                     # M3
                snippets.py                            # M4
      worker/ runner.py claim.py
      data/wordlists/{strong,mild,religious,slurs,sexual}.yaml  data/never_match.yaml
      cli.py                 # `vidcleaner clean <file> [--dry-run]`, `vidcleaner detect <file>`
    scripts/make_fixtures.py   # generates the test media from ffmpeg's own sources
    tests/ unit/ integration/ fixtures/ (hand-written SRT + ffmetadata, committed ffprobe JSON)
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
- Dev on the Mac: `brew install ffmpeg` (present, 9.0.1), `uv run uvicorn --reload`, `uv run vidcleaner-worker`, `npm run dev` with API proxy; fixtures via `scripts/make_fixtures.py`. Real tests on the unraid box via compose.

## 11. Milestones (tick as completed)

- [x] **M0 — Skeleton & plan in repo**: copy this plan to `PLAN.md`, `CLAUDE.md`, `git init`, backend/frontend scaffolds, Dockerfile builds, `/api/health`, SQLite + Alembic baseline, settings load/save, api+worker entrypoint. *Demo: container runs on unraid, UI shell loads.*
- [x] **M1 — Core clean via CLI**: word lists + matcher (tests), probe/extract/subtitles/windowed STT (faster-whisper + whisperX)/detect/render/verify on a local file; `vidcleaner clean <file> --dry-run|--out`; codec policy; subtitle redaction. *Demo: before/after MKV with Clean/Original tracks plays in Infuse; word counts printed.*
- [x] **M2 — Full-file STT + drift + robustness**: full mode (**VAD removed — see the Decision Log; it was inert in windowed mode and cost 5.5x the recall in full mode**), drift check, censored-token handling, suspicious guards, resumable stage markers, eval set with precision/recall in `docs/eval.md`. *Demo: movie with no subs processed overnight; timing error report.*
- [x] **M3 — Worker, swap, integrations**: job queue/claiming, backup/swap/rollback, Sonarr/Radarr clients + sync + backfill, webhook receivers with dedupe/upgrade handling, arr rescan + Jellyfin refresh + mapping check. *Demo: enable a series → existing episodes cleaned; Sonarr imports a new episode → auto-cleaned → Jellyfin shows Clean default.*
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
- 2026-09-01 — **M1 absorbed four items §11 lists under M2**, because §6/§7 make them integral to the
  stages M1 had to build rather than separable robustness work: censored-token handling (§7 defines
  it as part of matching), the suspicious guards (§7 defines them as part of padding), resumable
  stage markers (they *are* the stage contract in CLAUDE.md), and VAD (a faster-whisper parameter,
  already in `AppSettings`). **M2 therefore reduces to**: full-file STT as a selectable mode, the
  drift check (`pipeline/drift.py` — the `SubtitlesResult.offset_s` and `.reliable` fields are
  already reserved and already read by the detector), and the labelled eval set with
  precision/recall in `docs/eval.md`.
- 2026-09-01 — **M1 COMPLETE. Demo recorded.** `vidcleaner clean` run on the real test episode,
  PLURIBUS S01E01 (4.26 GiB, 56:28, eac3 5.1 Atmos, 61 subtitle streams):

  | stage | time |
  |---|---|
  | probe | 0.1 s |
  | extract | 3.9 s |
  | subtitles | 0.4 s |
  | transcribe | 67.4 s (186 s of windows, `large-v3-turbo` + wav2vec2, 14 threads → **2.8× realtime**) |
  | detect | 0.0 s |
  | render | 18.5 s |
  | verify | 4.4 s |
  | **total** | **~95 s for a 56-minute episode** |

  540 English cues → 44 subtitle hits → 28 windows (5.5% of runtime) → 306 transcribed words →
  **49 detections** (34 `both`, 10 subtitle-only, 5 STT-only that the subtitles had omitted),
  **36.1 s muted across 43 ranges**, 10 flagged for review. Counts: fuck 19, god 12, shit 8,
  bullshit 3, goddamn 3, jesus 2, christ 1, god damn 1.

  Output (4.57 GiB): `a:0` eac3 5.1 eng **Clean, default**; `a:1` eac3 5.1 eng **Original**,
  default cleared, **Atmos profile intact**; all 61 subtitle streams preserved with only the English
  one redacted (44 words masked); duration identical to the source (3388.416 s); all five
  `VIDCLEANER*` tags present. **Verify: 26/26 checks pass**, mute windows measured −90.3 dB against
  a −19.1 dB control window.

  **Still owed:** "plays in Infuse" is the one part of the §11 demo only Darick can confirm — the
  file is at `video/PLUR1BUS - S01E01 - We is Us.CLEAN.mkv`.
- 2026-09-02 — **Container verified, on both architectures.** The image builds and the whole M1
  pipeline runs inside it on Debian trixie's **ffmpeg 7.1.5**, which closes the gap left when the
  Docker daemon was unavailable. Tested natively on arm64 and, under emulation, on **amd64 — the
  actual unraid target**:

  * **Image content is 2.4 GB (arm64) / 3.2 GB (amd64)**, so §10's "~3 GB" estimate was accurate.
    Beware the tooling: `docker images` reports 3.34/4.36 GB (snapshotter overhead) while
    `docker image inspect --format '{{.Size}}'` reports a misleading 770 MB; `du` inside the
    container and the sum of layer sizes agree on the figures above. `torch` alone is 652 MB, so
    the CPU-wheel pin is worth roughly 2.5 GB — and it holds on **both** arches:
    `torch 2.14.0+cpu` with `torch.version.cuda is None` and **zero `nvidia-*` directories** in
    `site-packages`.
  * On amd64 the full render path was exercised too (STT replayed from a transcript, since the
    model would be pathologically slow under emulation): **26/26 verify checks**, −91.0 dB in the
    mute windows, and the same `a:0` Clean / `a:1` Original / `a:2` Commentary layout.
  * `get_caps()` detects version 7.1 and selects **`-/filter_complex`**, which works; the pipeline
    ran end to end through it.
  * **The 100-term expression budget is identical on 7.1.5 and 9.0.1** (100 parses, 101 rejected),
    bisected across 17 sizes on both. So `MAX_TERMS_PER_CHUNK = 90` is right for both, and the
    earlier suspicion that 7.x differed was a quoting bug in the throwaway test script, not ffmpeg.
  * `vidcleaner clean` on the generated fixture: **26/26 verify checks**, mute windows at −90.3 dB,
    5 subtitle words masked, `a:0` Clean/default, `a:1` Original, and the third track's `comment`
    disposition preserved — so §3's C4 correction holds on ffmpeg 7 as well.
  * Word lists ship inside the wheel: `vidcleaner words` runs in-image and its false-positive gate
    passes. The no-torch import boundary also holds inside the image, with torch installed.
  * **Entrypoint and packaging (the M0 demo that was never run):** `gosu` with unraid's 99:100,
    `alembic upgrade head`, api and worker both start, 181 word entries and the default profile seed
    on first boot, `spa_built: true`. `/api/health` returns `ok`; `/` and `/library` serve the SPA
    (200) and an unknown `/api/*` path 404s. The Docker healthcheck reports **healthy**.
    `VIDCLEANER_ROLE=worker` seeds on its own, confirming that ownership split. Killing the worker
    exits the container (137), so §4's "if either exits the container exits" holds; `docker stop`
    shuts down gracefully in 0.19 s (143, with `worker.shutdown` logged). `docker compose up` works
    with the documented volume env vars, and a settings PATCH round-trips with the API key
    **`enc:v1:`-encrypted at rest** and masked as `***` on read.

- 2026-09-02 — **M2 step 1 (one mode decision) complete.** The effective STT mode was being
  decided **twice, from different inputs**: `stt.run` read `spec.stt_mode` while `detect.run`
  recomputed `"windowed" if subs.cues else "full"`. So `--stt-mode full` on a file that also
  had subtitles ran the whole-file transcript through the *windowed* matcher, which then
  emitted a 0.3-confidence `source="subtitle"` fallback mute (~1.5 s) for every cue hit the
  full pass had listened to and **not** heard — muting dialogue on evidence the expensive
  pass had just refuted. `stt.resolve_mode()` is now the single rule; its answer is written
  to `Transcript.mode`/`.mode_reason`, so the artifact is the source of truth and a resumed
  job or a `--transcript` replay cannot disagree with the run that produced it.
- 2026-09-02 — **"No candidate windows" does *not* promote to a full pass.** Cues that parsed
  and matched nothing are evidence the file is clean, not missing information; promoting
  there would put a full-file transcription on every clean episode in the library. Only
  `no_subtitles` and `subtitles_unusable` promote. Caught by an existing M1 test rather than
  by review — whose fixture had also never modelled the case its own docstring described (it
  carried no cues at all); the fixture now does.
- 2026-09-02 — **§13's "setting to skip full mode above N hours" is `stt_full_max_hours`,
  default 3.0** (0 = no limit). It is enforced only inside `resolve_mode`, and only against
  *promotion*: an explicit `--stt-mode full|audit` ignores it, because the cap exists to stop
  the pipeline volunteering for a multi-hour pass, not to overrule someone who asked for one.
  A refused promotion records `full_skipped_too_long` in the transcript, which is what makes
  "we declined to look" distinguishable from "this file is clean".
- 2026-09-02 — **`deterministic_job_id` now includes a non-default `stt_mode`.** The profile
  hash deliberately excludes the STT model and mode (§14), so `--stt-mode full` after a
  windowed run landed in the same work dir, found `transcribe.done` and silently resumed onto
  the **windowed** transcript — the flag doing nothing whatsoever. Windowed ids are unchanged
  byte-for-byte, so existing work dirs still resume. `--model` also now overrides
  `stt_drift_model`, which M2 step 4 would otherwise leave pinned to `small`.

- 2026-09-02 — **M2 step 2 (full-pass robustness) complete.** Three defects that are
  invisible on 3 minutes of windows and serious on a 2-hour file, all in
  `whisper_backend.py`:
  * **`_merge_alignment` collapsed the whole transcript into one segment**, concatenating
    every segment's text. A full pass would have produced a single `TranscriptSegment` of
    ~20,000 words — destroying the structure `transcript.json` exists to carry. Aligned words
    are now placed back into the segment containing their midpoint, so segmentation and text
    survive; a word nudged just outside its segment goes to the nearest rather than being
    dropped.
  * **`_align` handed every segment plus the whole audio file to one `whisperx.align` call.**
    Now batched (`ALIGN_CHUNK_S = 300`, `ALIGN_CHUNK_SEGMENTS = 128`), with the audio loaded
    **once** via `whisperx.load_audio` and the array reused — a 2-hour `audio.wav` is ~460 MB
    as float32. M1's "alignment is optional at runtime" is extended to **per batch**: one bad
    stretch of audio no longer costs the other 119 minutes their timing accuracy.
  * **Full-mode progress never moved.** The denominator was `total_window_s`, which is 0 with
    no windows. It is now `total_window_s or duration_s`, and a full pass measures **position
    rather than accumulated duration** — with `vad_filter=True` silence produces no segments,
    so an accumulator stalls partway and never finishes. Recognition also now owns 0→0.80 of
    the bar and alignment 0.80→1.00, because on a full pass alignment costs about as much as
    recognition and previously the job sat at 100% for many minutes.

  Verified on 30 s of real speech from the M1 test episode: **3 segments preserved**
  (one, before), **37/37 words aligned** by real whisperX through the batched path, progress
  reporting `0.32 → 0.63 → 0.80 → 1.00` where full mode previously emitted only `1.0`.

- 2026-09-02 — **M2 step 3: a ONE CLOCK violation in the STT windows, found and fixed.**
  `subs.windows` are in **source container time** like every other persisted value, but
  `whisper_backend` used them directly against `audio.wav`, which is 0-based: as
  `clip_timestamps`, and as the out-of-window filter in `_to_segments`, which runs on raw
  model output *before* the offset is added back. On any file whose audio stream has a
  non-zero `start_time` — the TS-derived rips the ONE CLOCK note exists for — every window
  was displaced by that offset, so STT transcribed the wrong seconds and `dropped_out_of_window`
  silently climbed. Invisible until now because `start_time` is 0 on everything except
  `sample_offset.mkv`, which has no subtitle hits. `_windows_in_audio_time()` now does the
  conversion in one place; `Transcript.windows` are still persisted in container time, which
  `_build_transcript` exists to state and a test to pin. Drift (step 4) makes this urgent
  rather than theoretical: it places ±5 s probes by container time.

- 2026-09-02 — **M2 step 4 (drift measurement core) complete.** `pipeline/drift.py`, pure
  apart from one shell function, so the arithmetic that decides whether a library's
  subtitles can be trusted is testable with no torch, no ffmpeg and no speech — which it has
  to be, since every generated fixture is a sine tone.
- 2026-09-02 — **§6's "text similarity < 0.4" is unimplementable as written, and would have
  discarded every subtitle track in the library.** A probe transcribes its cue plus a ±5 s
  pad, so the window text is several times longer than the cue and any whole-string ratio is
  dominated by the pad. Measured with `rapidfuzz`: a *perfect* match scores
  **`fuzz.ratio` = 28.7** against **22.6** for completely unrelated dialogue — both below
  §6's own 0.4 threshold, and only 6 points apart. Replaced by **alignment coverage**, the
  fraction of the cue's words actually found in the audio: pad-invariant, in [0, 1], and
  free from the alignment already computed (1.00 vs 0.00 on the same pair). A test pins both
  halves, so the correction is demonstrable rather than asserted.
- 2026-09-02 — **§6's "spread > 0.5 s" is defined as the spread of the *per-probe medians*.**
  That is what sampling at 10/50/90% is for: a constant offset is correctable with one
  number, whereas an offset that grows across the file means the subtitles belong to another
  cut and no single correction fixes them.
- 2026-09-02 — **§6 step 3 and step 4 contradict each other on unreliable subtitles** — step 3
  widens the windows to ±6 s (still windowed), step 4 sends them to a full pass. Resolved by
  cause, with **three verdicts instead of two**: `ok` (apply the offset, normal windows),
  `unreliable` (offset measurable — apply it, widen the windows) and `discard` (coverage
  collapsed, so the cues do not describe this audio at all — fall back to a full pass). A
  measurable offset is worth correcting, and widened windows cost a fraction of a full pass.
  `discard` applies to *timing* only: redaction is text-local and correct regardless of sync.
- 2026-09-02 — Pairing is a **monotone sequence alignment** (`Levenshtein.opcodes` over folded
  token lists), not nearest-time or greedy fuzzy matching. Neither of those is
  order-preserving, so both mis-pair a cue that repeats a word — and "Bullshit. Bull. Shit."
  is already in the project's own fixtures. The `equal` blocks of an edit script are exactly
  a monotone one-to-one pairing and the `insert` blocks absorb the pad. Anchors shorter than
  3 characters are excluded from the median: "a", "of", "is" align by luck as often as by
  content and, being the commonest words, would dominate it.
- 2026-09-02 — The drift comparison key is **`fold(strip_wrappers(word))`, not `fold(word)`**.
  `normalize`'s pipeline is `strip_wrappers → normalize → fold`, so `fold` alone leaves outer
  punctuation attached and a cue's `"hurt."` would never pair with a spoken `"hurt"` —
  silently dropping every sentence-final word, which are the ones whose timing matters most.
  Caught by a test, not by review. §6 also lists window building *before* the drift check,
  but the verdict chooses the window padding and offset: the real order is
  cues → hits → drift → windows. Planning never refuses on probe *count*; whether two probes
  is enough to believe is `decide`'s call, which reports `skipped/too_few_probes`.

- 2026-09-02 — **M2 step 5 (drift wired into the subtitles stage) complete.** Drift is a call
  inside `subtitles.run`, **not a new stage**: §6 puts it in step 3, `extract` has already
  produced `audio.wav` by then, and a stage would mean editing `JOB_STAGES` — simultaneously
  the marker whitelist, `clear_from`'s ordering authority and `run_pipeline`'s validation
  list — plus `JOB_STATES` for M3's worker, all for a measurement with no independent resume
  value. `JOB_STAGES` and `JOB_STATES` are **untouched by M2**. The evidence goes to
  `drift.json` as a secondary artifact of the stage, the way extracted `.srt` files already
  do; `subtitles.done` covers both.
- 2026-09-02 — **The drift verdict is decided on *attempted* probes, not timed ones.** First
  implementation counted only probes that produced at least one anchor pair, so a subtitle
  track for the wrong episode — which pairs *nothing* — came back as `skipped`/"not checked"
  and was therefore trusted. That is exactly backwards for the case §6's similarity rule
  exists to catch. Coverage is now evaluated against attempted probes and before any timing
  check, so "paired nothing" reads as the strongest possible evidence of a mismatch. Caught
  by a test, not by review.
- 2026-09-02 — Drift runs with `align=False`. whisperX alignment exists to remove
  faster-whisper's 100–400 ms late bias, but a median over many anchors is already robust to
  a constant bias, and aligning would roughly double the cost of a check that runs on every
  job. New settings: `drift_check` (default on) and `drift_window_pad_s` (6.0, §6's ±6 s).
  The integration tests that are *not* about drift now pass `drift_check=False` — a real
  `small` pass on a sine-tone fixture was costing ~1 s per render test and took the tier from
  33 s to 96 s.
- 2026-09-02 — **Drift verified on the real test episode.** PLURIBUS S01E01, 540 cues: probes
  landed on cues **53 / 271 / 485** (10/50/90%), and the measurement was
  **`ok`, offset −0.164 s, spread 0.211 s, coverage 0.95**, in 17.1 s with `small`. The
  resulting windows are **28 covering 186.1 s — identical to the M1 run**, so the check
  changes nothing on a well-timed file, which is the regression guard that matters. The probe
  transcripts also show the ±5 s pad concretely: each is two to three times the length of its
  cue, which is why a whole-string similarity could never have worked.

- 2026-09-02 — **M2 step 6 (fixtures, integration, CLI reporting) complete.** Two new
  generated fixtures: `sample_nosubs.mkv` (no subtitle stream — the file that forces a full
  pass; deliberately separate from `sample_nolang.mkv`, which lacks subtitles for an
  unrelated reason, so a test that reads `nosubs_mkv` says what it means) and
  `sample_drift.mkv` (`marked.srt` shifted +2 s via `-itsoffset`, so the shift is one number
  in one place and the fixture cannot drift out of step with the tests).
- 2026-09-02 — **A "discard" verdict requires speech we actually heard, and the check counts
  *distinct* words.** Running the CLI on `sample_drift.mkv` exposed the hole: probes that hear
  nothing and probes that hear something unrelated both score zero coverage, and the first
  was being treated as proof the subtitles were wrong. In production three probes landing on
  music or a quiet scene would have thrown away a good subtitle track and pushed a two-hour
  film into a full pass. Then the first fix proved too naive — on the sine-tone fixture with
  `vad_filter=True`, Whisper still hallucinated **"you you you" / "you you" / "you"**: six
  words, **one distinct**. §3 documents this hallucination; here is where it does damage. So
  the floor is 8 *distinct* words (`MIN_PROBE_SPEECH_WORDS`), because non-speech
  hallucination is characteristically repetitive. With it, the fixture keeps its 5 subtitle
  hits instead of losing all of them.
- 2026-09-02 — The CLI report now shows a **Drift** line (verdict, offset, window pad) and the
  STT mode with its reason, and spells out `full_skipped_too_long` in full: that is the one
  case where "0 detections" must not be read as "this file is clean". None of it was visible
  before, which is how the discard bug survived a green test suite — it took running the
  binary to see it.
- 2026-09-02 — Re-verified on the real episode after all of the above: **unchanged** —
  `ok`, offset −0.164 s, spread 0.211 s, coverage 0.95, 44 hits, 28 windows / 186.1 s.

- 2026-09-02 — **M2 steps 7–8 (the eval harness and the labelled set) complete.**
  `backend/scripts/eval.py`, `backend/tests/eval/labels/pluribus_s01e01.yaml` (5 clips,
  18 labels, committed) and **`docs/eval.md`** — a new top-level directory not in §4's layout.
  Media is never committed: the labels name one file and are useless without it, which is the
  intended trade since the timings are the valuable part and they are tiny. Label times are
  **source container time**, per ONE CLOCK, and the loader rejects a label outside its clip —
  the likeliest authoring mistake is writing clip-relative times.
- 2026-09-02 — **§12's ground truth is split in two, because only half of it is knowable
  without listening.** *Presence* is solid: the English subtitles name the words, so precision
  and recall mean what they say today. *Timing* is not — a word boundary has to come from
  someone hearing it, and scoring a model against a boundary seeded by a model is circular.
  Every label carries `verified: false` and the harness prints `unverified` instead of a timing
  error until that changes. §12 also asks for "mean timing error"; the harness reports median
  **and** mean, since M1 already saw a single 4.1 s whisperX span and one such outlier makes a
  mean describe the outlier. Added beyond §12: **mute coverage**, the fraction of a labelled
  word the padded, merged ranges actually silence — the only metric that corresponds to "did
  the viewer hear it", and consistently the lowest number in the table.
- 2026-09-02 — **Cutting eval clips: `-ss` before `-i` with `-c copy` snaps to the preceding
  keyframe**, up to 2.5 s early here. Trusting the requested start put a systematic ~1.9 s
  error into every comparison and made a working detector score 0.21 precision. `-copyts` was
  tried and rejected — it keeps source timestamps but leaves the muxed subtitle timestamps
  rebased and the container duration an absolute end time, so the clip disagrees with itself.
  The working form is a rough input seek followed by an **accurate output seek**, video dropped.
- 2026-09-02 — **Silero VAD is now OFF by default, and the reason is the most surprising finding
  of M2.** faster-whisper documents that *"vad_filter will be ignored if clip_timestamps is
  used"* and gates VAD on `clip_timestamps == "0"` — so VAD has **never applied to this
  project's default windowed mode**, despite §3 leaning on it to cut hallucination from 40% to
  0.2%. M1's test that "a 1 kHz tone with `vad_filter=True` produces zero words" was true, but
  it exercised the *full*-mode path. Where VAD does apply it is actively harmful: measured on
  the eval set, full mode scores **recall 0.11 with VAD against 0.61 without, at identical
  precision (0.67 vs 0.69)**. On clip c2 — sixteen seconds of shouted dialogue — Silero reports
  **zero seconds of speech** at its default threshold and 0.9 s at 0.2; no threshold rescues it.
  It cost 5.5× the recall on exactly the files full mode exists to serve. An integration test
  now asserts faster-whisper's documented contract, so a library change re-opens the question
  instead of silently reversing it.
- 2026-09-02 — **First measured quality numbers** (5 clips, 18 labels, presence only):

  | model | mode | P | R | F1 | mute cov | wall |
  |---|---|---|---|---|---|---|
  | base | windowed | 0.65 | 0.83 | 0.73 | 0.59 | 86 s |
  | base | full | 0.86 | 0.33 | 0.48 | 0.32 | 97 s |
  | large-v3-turbo | windowed | 0.78 | 0.78 | 0.78 | 0.62 | 75 s |
  | **large-v3-turbo** | **full** | **1.00** | **0.72** | **0.84** | **0.69** | 102 s |

  `large-v3-turbo` in full mode is the best configuration measured, and also the slowest
  (~1.4× realtime), which is exactly why windowed stays the default and full is reserved for
  files with no usable subtitles. `base` is not good enough for full mode (recall 0.33) but
  remains fine for the drift probe, which only needs enough words to align against. Caveat
  recorded in `docs/eval.md`: the labels were seeded from a *windowed* run, so they under-count
  what a full pass hears and some "false positives" are probably real words missing from the
  labels.

- 2026-09-02 — **M2 COMPLETE. Demo recorded.** §11 asks for "a movie with no subs processed
  overnight; timing error report". Run on the real test episode, PLURIBUS S01E01, with **every
  subtitle stream stripped**, so the pipeline had nothing but audio:

  | stage | time |
  |---|---|
  | probe | 0.1 s |
  | extract | 4.0 s |
  | subtitles | 0.0 s (none present; drift skipped, `no_cues`) |
  | transcribe | **1173.1 s** — auto-promoted to `full`, `medium`, 14 threads, **2.9× realtime** |
  | detect | 0.0 s |

  `mode=full`, `mode_reason=no_subtitles`: auto-promotion worked on a real 56-minute file.
  **3,236 words in 560 segments** — the segment count matters, because the pre-M2 alignment
  would have collapsed all 3,236 into one. 2.9× realtime is comfortably better than §3's
  1.5–2× estimate for `medium`.

  | | windowed (M1, with subs) | full (M2, subs removed) |
  |---|---|---|
  | detections | 49 | **40** |
  | muted | 36.1 s / 43 ranges | **26.3 s / 37 ranges** |
  | suspicious | 10 | **1** |

  Full mode recovers **82% of the windowed detections with no subtitles at all**, and mutes
  **10 s less** doing it: the windowed run's ten subtitle-only fallbacks each smear 1.2–1.9 s
  across a ~0.3 s word, while every full-mode range is located by STT.

  **Timing error report.** Over the 27 detections both runs found: median **+0.009 s**, mean
  +0.083 s, max 0.74 s. Against windowed `source=both` rows (subtitle and STT agreeing) the
  median is **+0.004 s**; against windowed `source=subtitle` fallbacks it is **+0.551 s** — and
  it is the fallback that is wrong. That quantifies what M1 could only assert.

  Full mode missed 22 windowed detections and found 13 the windowed run missed; the dense
  shouting sequences at 1777–1792 s and 1834–1843 s split cleanly between the two modes. **That
  is the strongest evidence yet for §6's audit pass**: the union beats either mode alone.

  **Still owed:** (a) **no eval label has been verified by ear**, so `docs/eval.md` reports
  presence but withholds timing error — this needs someone who can listen, and it is the single
  most valuable next step; (b) three of the 13 full-only detections score confidence < 0.01,
  which looks like recognition noise, so **a confidence floor for `source="stt"` detections**
  is worth measuring (the harness can now answer it); (c) mute coverage tops out at 0.69, so a
  padding sweep is the obvious follow-up.

- 2026-09-02 — **Label verification tooling added**, closing the "still owed" item from the M2
  demo. `scripts/eval.py` gains three commands: `verify` (plays each unverified label's exact
  span through `afplay`/`ffplay` and takes 50 ms nudges, needing no extra software),
  `export-audacity` and `import-audacity` (a `.wav` plus a three-column Audacity label track per
  clip — boundaries are far easier to place by eye on a waveform than by ear, and Audacity's
  format is three tab-separated fields, so any editor that reads it works). Both routes rewrite
  the YAML in place and set `verified: true`; `run` starts reporting real timing error once the
  set is complete. The Audacity track is in *clip* time and the label file in *episode* time, so
  the importer converts — a test pins that, since getting it wrong would move every verified
  boundary by the clip offset, silently, in the one file that is supposed to be ground truth.
  A round-trip test also asserts the emitter loses no label, category or clip and preserves the
  header, because a verification session must not corrupt the set it is improving.

- 2026-09-02 — **Eval labels are verified per label, not per set, because some clips cannot be
  labelled by hand at all.** Darick verified c1, c3 and c4 in Audacity — moving boundaries,
  deleting three labels that were not really there, and labelling one span "fuck it" — but
  reported **c2 as too difficult**: it is six shouted repetitions of `fuck` running together,
  and no one can place their boundaries on a waveform. The words are certainly there, so c2 is
  still good ground truth for *presence*; it is simply not ground truth for *timing*. Three
  consequences: (a) `score` now measures timing error and mute coverage over **verified labels
  only** while presence still counts every label, with the count reported in an `n` column —
  the previous all-or-nothing rule would have meant reporting timing never; (b)
  `import-audacity` marks a clip verified **only if its boundaries actually moved**, so an
  untouched track is reported as "unchanged, left UNVERIFIED" instead of being silently
  certified — the failure that would have quietly poisoned the one number the harness exists to
  produce; (c) hand-typed labels are mapped to a word-list canonical, since "fuck it" is a real
  thing to have heard but would otherwise have matched no detection and read as a miss.
  Result: **9 of 15 labels verified**, and real timing numbers for the first time.
- 2026-09-02 — **faster-whisper on CPU is not deterministic, and precision is the casualty.**
  Three *identical* runs of `base`/windowed produced **8, 12 and 72 false positives**
  (P = 0.62, 0.54, 0.15). The 72 is `base` entering a **repetition loop** on the c2 shouting
  and emitting `fuck` dozens of times at one timestamp; whether it does turns on tiny numerical
  differences, so it is bistable. Recall and timing are stable across the same runs (TP
  13/14/13, median timing within ~20 ms) — so a single run measures those fine and measures
  precision not at all. `large-v3-turbo` is markedly steadier (4 and 9 false positives, no
  degeneration), which is an argument for the shipped default beyond raw accuracy. Added
  `--repeat N`, which reports the **median run** chosen by F1 plus the observed false-positive
  range, rather than the median of each column separately — so every figure in a row comes from
  one real execution instead of a combination that never happened.

- 2026-09-02 — **M3 step 1 (queue schema and constants) complete.** Migration `0002`, plus
  `STAGE_TO_STATE`, `DEFAULT_PRIORITY`, `TERMINAL_STATES`/`RUNNING_STATES` and a `cancelled`
  state in `db/constants.py`, and `utcnow`/`immediate_connection` in `db/session.py`.
  Verified: 1102 passed.
- 2026-09-02 — **§6's "transient API failures retry with backoff" had nowhere to live.** §5's
  `jobs` has no scheduling column, so a requeued job is claimable immediately and the worker
  hot-loops on it until `attempts` runs out — turning a 60-second backoff into a spin.
  `jobs.retry_at` added, with `ix_jobs_retry_at` and a claim predicate. Also **`jobs.force`**:
  §4 requires "unless forced" and §9.3 offers "reprocess all", and deriving that from `trigger`
  would conflate two independent things.
- 2026-09-02 — **`jobs.priority`'s direction was undefined and is a classic silent inversion.**
  §4 claims with `ORDER BY priority, created_at`, so *lower runs sooner* and §8's "backfill
  priority below webhook jobs" means a **higher** number. Pinned in `DEFAULT_PRIORITY` as
  `manual/reprocess 50, webhook 100, backfill 200, audit 900`, and a test asserts that the
  column's existing default of 100 is the webhook value — so the numbers cannot drift apart
  from the column they configure.
- 2026-09-02 — **`attempts` means "how many times this job has been claimed", not "how many
  times it failed"**, and the docstring now says so. Counting failures loses crashes: a worker
  killed mid-render would never burn an attempt, so a job that reliably kills the worker would
  loop forever. The consequence is that `persist_run`, which increments at the *end* of a run,
  must not double-count on the worker path (step 3).
- 2026-09-02 — **`JOB_STATES` gains `cancelled`.** §6.0 requires an upgrade to "supersede any
  running job" and §9.1 puts a cancel button on the Queue page, but there was no state to put
  such a job in. No migration needed — `db/constants.py` already records that states are TEXT
  precisely so adding a value never requires one.
- 2026-09-02 — **`STAGE_TO_STATE` lives in `db/constants.py`, not the worker.** M4's API needs
  to render it without importing the worker, and the two vocabularies are asymmetric in both
  directions: stage `transcribe` → state `transcribing` and `detect` → `detecting`, while
  `subtitles` and `snippets` are spelled the same. A test asserts it covers `JOB_STAGES` exactly
  and that every value is a real `JOB_STATE`.
- 2026-09-02 — **One live job per media item is enforced by the database, not by a query.**
  The api (webhooks, backfill) and the worker (audit passes) enqueue from separate processes, so
  a `SELECT`-then-`INSERT` guard cannot be atomic across them. `ux_jobs_one_active_per_item` is
  a partial unique index on `media_item_id` where the state is non-terminal; the terminal list
  is written out as a literal in the migration rather than imported from `db.constants`, because
  a migration must describe the schema *as of that revision* and would otherwise change meaning
  the next time someone edits `TERMINAL_STATES`.
- 2026-09-02 — **`BEGIN IMMEDIATE` is required for the claim, and `busy_timeout` cannot
  substitute.** Under WAL, a `SELECT`-then-`UPDATE` upgrade fails with `SQLITE_BUSY_SNAPSHOT`
  *immediately*: SQLite does not invoke the busy handler for snapshot conflicts. `BEGIN
  IMMEDIATE` takes the write lock before the read, and `busy_timeout` **does** cover that —
  measured, a contending transaction blocks for the timeout and then raises "database is
  locked", which `claim_next` will report as "no work". `immediate_connection` is deliberately
  scoped to the two read-then-write operations rather than installed as a global `begin` event:
  emitting it for every transaction would make the api's read-only queries take the write lock,
  which is the one thing WAL exists to prevent.
- 2026-09-02 — **`DateTime(timezone=True)` is a no-op on SQLite**, so an aware datetime
  round-trips as a **naive** one and `datetime.now(UTC) - job.heartbeat` raises `TypeError`.
  Every value the queue compares against a column now comes from `db.session.utcnow()` (naive
  UTC). A test writes one row via `func.now()` (19 characters) and one from Python (26) and
  asserts both compare correctly in SQL *and* in Python. Nothing had hit this because the CLI
  never compared two timestamps — M1 and M2 only ever wrote them.
- 2026-09-02 — `tests/unit/test_health.py` hardcoded revision `"0001"`, so the first migration
  after the baseline would have broken a test about the health endpoint. It now reads the head
  from Alembic's own `ScriptDirectory`. A companion test asserts every model index exists in the
  migrated database, since column drift was already guarded but index drift was not — and a
  half-landed `0002` would show up as a missing unique index rather than as an error.

- 2026-09-02 — **M3 step 2 (the queue) complete.** `worker/claim.py`: `claim_next`,
  `heartbeat`, `should_abort`, `recover_stale`, `enqueue`, `set_state`, `release`, `cancel`,
  `reprioritize`, `log_event`. Verified: 1130 passed.
- 2026-09-02 — **§4's claim sets `state='probing'`, which is briefly wrong for a resumed job,
  and that is the right trade.** A job resuming from a later marker is not probing, and a
  dry-run job never renders — but resolving the state properly means reading the work dir's
  stage markers, and doing filesystem I/O while holding SQLite's write lock is precisely how
  the *other* process's claim starts failing with "database is locked". The runner therefore
  corrects the state from the markers before it runs anything, so the wrong value is never
  observable for longer than one poll iteration.
- 2026-09-02 — **Fencing needs no new column.** A worker whose heartbeat lapsed may still be
  alive (a suspended container, an NFS stall), so stale recovery can hand its job to a second
  worker while the first is still working. Every write carries `AND claimed_by = :me` and
  `heartbeat()` returns `rowcount == 1`; a `False` return means "you no longer own this" and
  the worker stops. Note the deliberate asymmetry: a `heartbeat` that fails with
  `OperationalError` returns **True**, because a momentarily busy database must not be read as
  losing the job — `STALE_AFTER_S` is four missed heartbeats precisely so that is survivable.
- 2026-09-02 — **`recover_stale` never requeues a `swapping` job blind.** Swap is the only
  stage that renames library files, so a crash between `rename(original → backup)` and
  `rename(staged → final)` leaves a state a fresh run can make *worse*. Such a job goes to
  `failed` with an actionable error unless `claim.SWAP_RECONCILER` — the hook M3 step 5 fills
  in from `pipeline/swap.py` — can inspect the intent journal and say `requeue`/`done`.
  Defaulting to `failed` when the hook is unset is the safe direction: refusing to retry costs
  a manual reprocess, whereas retrying over an unknown half-renamed library can cost the file.
- 2026-09-02 — **`enqueue` relies on the database for its central invariant, not on its own
  read.** It checks for a live job first (so it can report `deduped`/`already_active`/
  `superseded` usefully), but the insert runs inside a `begin_nested()` savepoint and an
  `IntegrityError` from `ux_jobs_one_active_per_item` is caught and turned into `reason="raced"`
  with the winner's job id. That is the only correct shape when two processes enqueue: the
  pre-check is an optimisation, the index is the guarantee.
- 2026-09-02 — **§6.0's dedupe window only changes the *reason*, never the outcome.** With one
  live job per item enforced, a second `Download` for the same item always returns the existing
  job; the 60 s window just distinguishes "duplicate delivery" (`deduped`) from "we are already
  working on this" (`already_active`), which is what the webhook's `note` should say. Recorded
  because §6.0 reads as though the window itself prevents the duplicate.
- 2026-09-02 — `MAX_ATTEMPTS = 3` is enforced in *recovery*, not only on failure: a job that
  reliably kills the worker is retired to `failed` after three claims rather than crashing the
  container forever. This is only coherent because `attempts` counts claims (step 1).
- 2026-09-02 — Queue tests must keep using the **file**-backed `migrated` fixture. SQLAlchemy
  passes `check_same_thread=False` and uses `QueuePool` only for file SQLite URLs; an in-memory
  URL flips to `SingletonThreadPool` with `check_same_thread=True`, which would break both the
  8-thread claim test and (in step 3) the heartbeat thread. Stated in the test module header,
  because the failure mode if someone "optimises" it is a confusing threading error far from
  the change.
- 2026-09-02 — The concurrent-claim test asserts **no job was claimed twice** and then drains
  the remainder, rather than asserting that all three claimers won. A claimer that loses the
  write lock past its busy timeout legitimately gets nothing; asserting otherwise would make a
  correct implementation flaky.
- 2026-09-02 — `ruff check` had only ever been run over `vidcleaner/`; over `tests/` it found
  two pre-existing `B905` (`zip()` without `strict=`) in M2's `test_eval_scoring.py`, on loops
  whose lengths the preceding lines already assert equal. Fixed, and `ruff check .` /
  `ruff format --check .` are now clean across the whole backend, which is what future
  milestones should run.

- 2026-09-02 — **M3 step 3 (the worker's supporting machinery) complete.** `worker/spec.py`,
  `heartbeat.py`, `progress.py`, `joblog.py`, `policy.py`; `M3_STAGES`,
  `StaleSourceError`/`SwapBrokenError` and `StageContext.integrations` in `pipeline/stages.py`;
  `JobTarget` plus `JobSpec.in_place`/`.trigger`/`.target` in `artifacts.py`;
  `matching/profile.py::snapshot_for`. Verified: 1283 passed, 88 of them against real media.
- 2026-09-02 — **Worker jobs lose the resume protection the CLI gets for free, and it had to be
  rebuilt explicitly.** `deterministic_job_id` folds the profile hash and a non-default
  `stt_mode` into the work directory's *name*, so the CLI physically cannot resume onto
  artifacts computed under different rules. Worker jobs are named by the `jobs` uuid, and
  `build_context` overwrites `job.json` unconditionally — so a reprocess after a whitelist edit
  would have replaced artifact zero while the old stage markers survived, and `transcribe` and
  `detect` (the two stages that would have noticed) would both have been skipped. This is the
  same defect M2 step 1 found for `--stt-mode`, arriving by a different route.
  `worker/spec.py::plan_job` compares the on-disk spec's `profile_hash`, `stt_mode`,
  `source_path` and `version` against the fresh one and calls `ws.clear_all()` on any
  difference. `force` and `dry_run` are deliberately *not* in that list: they change what we do
  next, not what the existing artifacts mean, and `run_stage` already honours `spec.force`.
- 2026-09-02 — **The settings and profile snapshots are taken at *claim* time, not at enqueue.**
  Enabling a series can queue hundreds of backfill jobs; if the snapshot were frozen at enqueue,
  a word-list edit made while the queue drained would not reach them.
- 2026-09-02 — **`snapshot_for` is extracted from `cli_clean.py` into `matching/profile.py`**
  because the worker needs exactly the same mapping and two copies would eventually produce two
  different `profile_hash` values for one profile — and the hash is what decides whether a file
  is `already_clean`. The worker also goes through `matcher_for`, not the CLI's plain
  `build_matcher`: only `matcher_for` folds the title- and item-scoped whitelist into the hash,
  which is precisely what makes "whitelist a false positive, then reprocess" not short-circuit.
  A test asserts an item whitelist changes the hash.
- 2026-09-02 — **`JobTarget` carries ids, never credentials.** `refresh` needs to know which
  series to rescan and which paths to tell Jellyfin about, but `build_spec` strips
  `SECRET_FIELDS` from the settings snapshot precisely because `/work` ends up in bug reports,
  so a stage cannot rebuild an API client from `job.json`. Splitting it — identity on disk (the
  work dir stays self-describing, per CLAUDE.md), keys injected through
  `StageContext.integrations` — is the only arrangement that satisfies both. A test asserts
  `job.json` contains no `api_key`.
- 2026-09-02 — **`JobSpec.in_place` makes the swap opt-in.** Inferring it from `out_path is
  None` would mean today's `vidcleaner clean file.mkv`, which writes to `/work`, silently
  started rewriting the library. `JobSpec.trigger` exists for a smaller reason: §6.1's stability
  wait costs at least 10 s and only matters for `webhook` jobs, where the import may still be in
  flight, so the runner needs to know which kind of job it is holding.
- 2026-09-02 — **The heartbeat is a thread, and the three reasons are all in the existing
  code.** `verify` emits no progress and runs a full decode of the output — minutes on a feature
  film; §6.1's `wait_for_stable` blocks up to 300 s inside `time.sleep`; and `probe`,
  `subtitles` and `detect` emit nothing at all. So piggybacking §4's 30 s heartbeat on
  `on_progress` would let a perfectly healthy job be declared stale and stolen. Making
  `JobMonitor` the **only** writer of the job row while a job runs pays for itself twice:
  `report()` becomes a lock-free store, so ffmpeg's roughly-per-second callbacks need no
  throttling code; and the tick that writes the heartbeat also reads the state back, which is
  the only cross-process channel available for "someone cancelled this". §4's 30 s is kept as
  the staleness *contract* (`STALE_AFTER_S` is four of them) while the thread ticks at 5 s so
  the UI is smooth — writing more often than promised is always safe.
- 2026-09-02 — A `heartbeat()` that fails with `OperationalError` returns **True**, not False.
  The return value means "do you still own this job", and a momentarily busy database is not
  evidence that you lost it; `STALE_AFTER_S` being four missed heartbeats is what makes that
  safe. Getting this backwards would abandon jobs under exactly the load that causes lock
  contention.
- 2026-09-02 — **`progress_pct` is undefined in PLAN.md**, so `ProgressTracker` defines it:
  fixed per-stage weights taken from the M1/M2 demo timings, normalised over the *planned*
  stage list — a dry run has five stages, not ten, and must still read 100% when it finishes.
  Two weight tables, because M2 measured 1173 s of full-file STT against 67 s windowed on the
  same episode: with one table a full pass would sit at 6% for two hours. `reweight()` exists
  because the promotion to a full pass is decided *inside* the transcribe stage, so the runner
  cannot know at claim time which table applies. The tracker also takes its floor from
  `ws.completed_stages()`, because `run_stage` returns early on a marker hit and deliberately
  does **not** call `ctx.progress` — without that, a job resuming at `render` would report 0%
  until render finished.
- 2026-09-02 — **`job_logs` is the UI's timeline, not a second copy of the ffmpeg log.** Raw
  ffmpeg stderr and argv already go to `/work/<job_id>/ffmpeg.log`; the table gets the few dozen
  lines §9.1's log tail and §9.4's job log would show. Rows are **buffered and flushed at stage
  boundaries**: one transaction per log line would take SQLite's write lock dozens of times per
  job, and step 2 already established that holding it too long makes the other process's claim
  fail. `Timeline.flush` swallows its own errors — bookkeeping must never fail a good render —
  and a test asserts that by flushing a row whose foreign key cannot resolve.
- 2026-09-02 — **§6's "render/verify failures are terminal until reprocess" needed a third
  category, not just two.** Once `swap` has committed, the library file is correct; a failure in
  `refresh` (or M4's `snippets`) after that must leave the job `done` with a warning, because
  marking it failed would invite a retry that re-runs the swap. §6 step 9 already says "warn"
  for the mapping check — `BEST_EFFORT_STAGES` generalises it. `policy.classify` is the whole
  table: `SwapBrokenError` is terminal and never retried; `StaleSourceError` is §6's "path
  vanished" and gets exactly one requeue before `stale`; `render`/`verify`/`swap` are terminal;
  anything earlier retries on a 60/300/900 s backoff until `MAX_ATTEMPTS` claims.
- 2026-09-02 — **`persist_run` gains `count_attempt=False` and an explicit `media_item`.** It
  increments `attempts` at the *end* of a run, which double-counts now that the queue
  increments at claim — every job would retire after 1.5 real attempts. And its
  `ensure_media_item` lookup is by path, which for a worker job would resolve the *post-swap*
  path and create a duplicate row; the worker already knows which row it holds.

- 2026-09-02 — **M3 step 4 (sidecar redaction) complete.** `subtitles.redactable_sidecars`,
  `subtitle_candidates`, `_load_best_candidate`; `render._redact_sidecars`;
  `SubtitlesResult.redactable_sidecars`. Verified: 1294 passed.
- 2026-09-02 — **§6 step 6's sidecar rewriting had no producer at all, so a file whose only
  subtitles are a sidecar was redacted nowhere.** M1's Decision Log deferred *installing*
  redacted sidecars to `swap.py`, which presumed something was making them —
  `RedactedSubtitle.sidecar_source` has been a field with no writer since M1, and
  `SubtitlesResult.sidecars` a list with no consumer. The reason is one line in
  `render._redact_subtitles`: it iterates `subs.redactable`, which `redactable_streams()`
  builds from `probe.text_subtitles`, i.e. **embedded streams only**. `render._redact_sidecars`
  now produces them, and they land in `RenderResult.redacted` (the record swap acts on) but
  never in the render plan — a sidecar is a separate library file, not something ffmpeg muxes.
- 2026-09-02 — **An *untagged* sidecar is redacted, while an untagged embedded stream is
  skipped, and the asymmetry is deliberate.** For embedded streams, guessing is unnecessary and
  avoidable: the M1 episode has 61 text streams, exactly one English, and three untagged
  Chinese ones. A sidecar is the opposite shape — a bare `Movie.srt` with no language in its
  name is the *usual* layout and is nearly always the primary language, so applying the embedded
  rule would mean the commonest sidecar layout never got masked. It is safe because redaction
  only masks what the matcher matches: an English word list over a Spanish subtitle finds
  nothing. A sidecar *tagged* with a language we ship no list for is still excluded — nothing
  could match it, so rewriting it would be pure risk. Both halves are asserted in one test so
  the asymmetry reads as a decision rather than an oversight.
- 2026-09-02 — **A sidecar with zero hits produces nothing to install.** Replacing a library
  file with a byte-different copy of itself is strictly worse than leaving it alone: it changes
  the mtime, invites a Jellyfin rescan, and puts a pointless entry in `/backups`.
- 2026-09-02 — **A defect found by the new test, not by review: one corrupt sidecar failed the
  entire job.** §6 step 3's precedence *prefers* a sidecar over an embedded stream, and nothing
  looked past the one it chose — so a `Movie.srt` that `pysubs2` cannot parse (a truncated
  download, a stray binary file with the wrong extension) raised out of the `subtitles` stage
  on a file with four perfectly good embedded English tracks. `choose_subtitle_source` is now a
  thin wrapper over **`subtitle_candidates`**, which returns every source best-first, and the
  stage walks them until one parses. An unusable candidate is a warning; running out of
  candidates yields `kind="none"`, which merely promotes the job to a full-file pass. Both are
  better outcomes than failing. The refactor is behaviour-preserving for the happy path —
  `choose_subtitle_source` returns `candidates[0]` — and the four overlapping precedence passes
  now de-duplicate, so each text stream appears exactly once in the list.

- 2026-09-02 — **M3 step 5 (the swap transaction) complete.** `pipeline/swap.py`
  (`FsOps`/`RealFs`, `plan_swap`, `preflight`, `execute`, `recover`, `restore_backup`,
  `reconcile`, the stage), `SwapPlan`/`SwapResult`/`SidecarSwap` artifacts,
  `Workspace.swap_plan_json`/`swap_json`/`swap_broken_json`/`refresh_json`,
  `atomic_write_bytes(fsync=)` and `Artifact.write(fsync=)`, and the
  `allow_cross_device_backup` setting. Verified: 1349 passed.
  **The steps after this one are reordered:** integrations clients (was step 8) now come
  before the worker runner (was step 7), because the runner's stage list includes `refresh`,
  which needs the clients. Order from here: backup persistence and restore → integrations
  clients and path mapping → refresh, sync and CLI → the worker runner → webhooks and demo.
- 2026-09-02 — **§6 step 8's rename sequence has no journal, so its own rollback cannot run
  after a crash.** "On failure `rename(backup → original)`" covers an *exception*; a power loss
  between the two renames leaves a library folder with no video file and nothing on disk that
  says why. Two renames cannot be made atomic, so `swap.plan.json` is written and **fsynced**
  (the file *and* its parent directory) before the first rename, and `recover` resolves every
  reachable state from it. `/work` is documented as a writeback-cache SSD, which is exactly why
  the fsync is not optional here and is not used for any other artifact — everything else is
  reconstructible from its inputs.
- 2026-09-02 — **The recovery decision is driven by file *size*, not by existence.** That is
  the only question that works for both shapes: an MKV is swapped in place, so `source_path`
  and `final_path` are one path and "does the source exist" cannot distinguish "not started"
  from "committed"; an MP4 becomes an MKV, so they are two. Comparing against
  `plan.out_size`/`plan.source_size` also catches a short copy, which is the classic ENOSPC
  outcome. The resolved states: nothing committed → redo; backup present with a *good* staged
  file → **roll forward** (those bytes already passed `verify`, so rolling back would discard a
  good render for nothing); backup present with no usable staged file → roll back and
  re-render; final path holds the output → committed (with a warning if the backup has since
  been moved); source *and* backup both present → refuse, since only a rename that behaved as a
  copy produces that and we cannot tell which file is authoritative; nothing anywhere → `stale`.
  Fourteen tests construct each state as plain files. Crash timing cannot be tested by luck —
  you cannot reliably `docker kill` between two renames — but it can be tested exhaustively.
- 2026-09-02 — **The rename order is load-bearing: original → backup happens first.** §3 warns
  that a stray sibling video file can be adopted by Sonarr as *the* file, which is a
  data-loss-shaped outcome, so the folder must never hold two. The cost is a window one rename
  wide where it holds none — much cheaper, because neither arr deletes anything during a scan
  and the episode merely reads as missing until the next moment. A test asserts the exact rename
  order and that only one video file ever exists in the folder.
- 2026-09-02 — **The staged filename must not end in a video extension.** An arr's disk scan
  enumerates by extension, so the temp is `.vidcleaner.<name>.mkv.tmp` — dot-prefixed and
  `.tmp`-suffixed. It is staged in the *destination* directory, so installing it is a rename
  rather than a copy.
- 2026-09-02 — **A failed staging step renames the output back to `out.mkv`.** Without it a
  resumed job finds `render.done` present and `out.mkv` gone, then fails inside `verify` for
  reasons that look nothing like the actual cause. (The first implementation unlinked the staged
  file *before* trying to rename it back, which was found by writing the test.)
- 2026-09-02 — **Same-filesystem detection cannot trust `st_dev`.** unraid's `/mnt/user` is a
  FUSE shfs mount: two paths in one share report the same `st_dev` while the underlying disks
  differ. `st_dev` therefore only *plans* which operation to attempt; the real call is always
  try-`rename`-then-fall-back-on-`EXDEV`, and the fallback is tested.
- 2026-09-02 — **Cross-device backups are refused by default** (`allow_cross_device_backup`).
  A backup across devices cannot be a rename, so it would mean copy + verify + `unlink` the
  original — and CLAUDE.md reserves library writes to this module precisely so that no code
  path deletes one. §10 already *assumed* one filesystem ("so swaps are same-filesystem
  renames"); this enforces it with a message that names the fix.
- 2026-09-02 — **§6 step 8's `copymode` is under-specified and partly impossible.**
  `shutil.copymode` copies permission bits only; ownership needs `os.chown`, which a `gosu`'d
  non-root process can only do for files it already owns — so it is best effort, recorded in
  `SwapResult.owner_applied`, and made right by construction by §10's `PUID/PGID` + `umask
  0002`. Permissions are applied to the staged file *before* the rename, so the file is never
  briefly visible under its real name with the wrong mode. And **mtime is deliberately not
  copied**: Jellyfin's scanner keys on it and the `refresh` stage that runs next depends on the
  file looking new — a test asserts the mtime moves forward.
- 2026-09-02 — **Sidecars are installed last, and a sidecar failure can never cost a good video
  swap.** Each one is backed up and replaced independently, with its own rollback; a missing or
  unwritable redacted subtitle becomes a warning on an otherwise successful `SwapResult`.
- 2026-09-02 — **§9.3's "restore originals" cannot restore to `backups.original_path`.** After a
  `Rename` webhook that path is stale, so restoring there would recreate the old filename *and*
  leave the cleaned file behind — two video files in one folder, §3's adoption hazard. Restore
  targets the item's *current* path and **displaces** the cleaned file to `<name>.cleaned`
  (numbered on collision), never unlinking it. For the MP4 case the `.mkv` must be displaced or
  the arr adopts the wrong file. Backups are verified against the recorded size and fingerprint
  before anything moves.
- 2026-09-02 — **Two guards §6 does not ask for.** The backup path must not already exist (a
  second clean after a restore gets a `vc-<job>` suffix rather than overwriting the earlier
  original, and keeps its real extension so the audit pass can probe it); and `backups_dir` must
  not sit inside a library folder, which would recreate §3's two-files-per-episode hazard with
  real video extensions. A `.ignore` marker is written into `/backups` on first use, because §10
  defaults it to a directory *inside* the media share and Jellyfin honours that file.
- 2026-09-02 — **`swap` and `refresh` are not pure functions of their on-disk inputs, and
  cannot be.** CLAUDE.md's contract exists to make resume safe; `swap` gets that property from
  the intent journal instead, and `refresh` from idempotence (a rescan and a path notification
  are harmless twice). Said in both module docstrings rather than left to rot.
- 2026-09-02 — **Every OS call goes through the `FsOps` protocol.** Not for purity: it is the
  only way to reach EXDEV, ENOSPC, a short copy, EACCES on `chown`, a failed install and a
  failed *rollback* deterministically — and those are precisely the paths that must be correct
  the first time they happen for real. Waiting for a genuine full disk is not a test strategy.

- 2026-09-02 — **M3 step 6 (backup persistence and restore) complete.** `persist_swap`,
  `restore_item`, `reconcile_backups`; `vidcleaner clean --in-place`, `vidcleaner restore`,
  `vidcleaner backups list|reconcile`. Verified: 1371 passed, and a real `clean --in-place`
  on the generated fixture followed by a **byte-identical** restore, subtitle sidecar included.
- 2026-09-02 — **§6 step 8's "record `backups` row" contradicted CLAUDE.md**, which puts
  persistence in `persist.py` after the pipeline. Resolved by making `swap.json` the durable
  record and `persist_swap` the writer, with **one row per file moved** — the video and each
  sidecar. §5's scalar `original_path`/`backup_path` read as one row per job; several rows
  sharing a `job_id` is the natural fit and is what lets a restore put the subtitles back too.
- 2026-09-02 — **`reconcile_backups` closes the one window the swap cannot.** A rename and a
  SQLite commit cannot be made atomic, so a crash between them leaves a file in `/backups` that
  nothing knows about (and a human deleting a backup leaves the reverse). A `pending` backup
  state was considered and rejected: DB-first ordering just moves the window rather than
  closing it. The intent journal is the truth *during* a swap and the reconciler is the backstop
  *afterwards*. An adopted file hangs from the same sentinel title the CLI's own runs use,
  because `backups.media_item_id` is NOT NULL and a lost original belongs to nothing we can
  identify.
- 2026-09-02 — **Restore targets the current name with the *original's* extension.** Two
  separate corrections in one expression: not `backups.original_path` (a `Rename` webhook makes
  it stale, and restoring there recreates the old filename while leaving the cleaned file
  behind — §3's two-video-files hazard), and not the current suffix either (an MP4 that became
  an MKV has to go back as an MP4). So `Path(item.path).with_suffix(original_suffix)`.
- 2026-09-02 — **The displaced cleaned file goes to `/backups`, not beside itself.** The first
  implementation renamed it in place as `<name>.cleaned`, which is *safe* — an arr's disk scan
  enumerates by video extension and `.cleaned` is not one — but leaves a source-sized file in
  the media share that nothing will ever tidy up. `RestorePlan.displace_to` now names the
  destination and `restore_item` points it at `/backups` beside the original it replaced, where
  the retention clock can reach it. Caught by an integration test asserting the library folder
  holds exactly one file afterwards.
- 2026-09-02 — A restore that cannot put a *sidecar* back only warns; the video is already
  restored and failing the whole operation over a subtitle would be the wrong trade. Symmetric
  with the swap, where sidecars are installed last for the same reason.
- 2026-09-02 — `vidcleaner clean --in-place` refuses to combine with `--dry-run` or `--out`,
  and appends `swap` to the stage list. It exists so the swap transaction is demonstrable
  before the worker (and long before §9's UI): the M3 integration tier now runs a real swap on
  generated media, asserts the backup is **byte-identical** to the original, restores it, and
  asserts the restored file is byte-identical again — plus that a second run reports
  `already_clean`, which closes §4's idempotency loop across a real library swap for the first
  time.

- 2026-09-02 — **M3 step 7 (integration clients and path mapping) complete.**
  `integrations/{__init__,base,models,pathmap,sonarr,radarr,jellyfin}.py`,
  `tests/support/fake_arr.py`, `tests/fixtures/arr/*.json`, and a new **`tests/contract/`
  tier** with its own `contract` marker. `base.py`, `models.py` and the `Integrations` bundle
  are beyond §4's file list; Sonarr and Radarr share ~90% of their surface, so it lives in
  `ArrClient` and the two named modules stay thin. Verified: 1429 passed, 42 of them contract.
- 2026-09-02 — **§12's `respx` is replaced by `httpx.MockTransport`.** It does the same job
  with no new dependency and nothing pinned to httpx internals: the handler is a plain
  function, so a test routes by `(method, path)`, returns a committed JSON fixture, **and**
  asserts on the recorded call sequence — which is how "a 401 is not retried" and "a rescan
  posts exactly this body" get pinned. It also matches the injection idiom already in the
  project (`FFmpegRunner`, `ScriptedTranscriber`, `FsOps`). `respx` would only earn its place
  if the clients were async, which they are not.
- 2026-09-02 — **The clients are synchronous, deliberately.** The worker is a sync process and
  `refresh` is a sync stage; every existing FastAPI handler is a sync `def` over a sync
  `Session`, which FastAPI runs in a threadpool; and the webhook receivers make no outbound
  calls at all. Async would buy nothing anywhere while costing either an event loop inside the
  worker or a split session layer in the api. `httpx.Client` is documented thread-safe.
- 2026-09-02 — **§8's path mapping has no defined direction, and now does: `from_prefix` is
  the *app's* path, `to_prefix` is *ours*.** The arrs hand us paths (`to_local`), we hand
  Jellyfin paths (`to_remote`). Plus two constraints §5 omits — **longest prefix wins**, and
  **component-aware matching**, because a naive `startswith` makes a rule for `/media/tv`
  rewrite `/media/tvshows`, a silent wrong-path bug whose *best* case is a refused swap. Also:
  a duplicate `to_prefix` is rejected as well as a duplicate `from_prefix`, since it would make
  `to_remote` ambiguous; and everything is string manipulation, never `pathlib`, because a
  Sonarr on Windows reports `C:\media\TV\...` and `Path` on Linux would collapse it to one
  component. A broken mapping table degrades to identity with an error log rather than taking
  the integration down.
- 2026-09-02 — **The invariant that makes mapping checkable: the database stores local paths
  exclusively.** Mapping happens only at the boundary, and never on a `/work` or `/backups`
  path. Stated in `integrations/__init__.py` because it is the kind of rule that decays the
  first time someone maps a path "just here".
- 2026-09-02 — **§3 names a Test endpoint only for Jellyfin; the arr equivalent is
  `GET /api/v3/system/status`.** It validates the key *and* returns `version`/`appName`, so
  §9.6 can show "Sonarr 4.0.10" rather than a bare green tick. For Jellyfin the authenticated
  `/System/Info` is used rather than `/System/Info/Public`, which answers without a key and
  would therefore report success for a wrong one. `test()` catches `IntegrationError`
  (including auth) and returns `TestResult(ok=False, detail=...)` at HTTP 200 — a wrong key is
  the likeliest reason anyone clicks Test, so it must be a red row with a reason rather than a
  stack trace. Every *other* call still raises on 401, because a refresh that silently did
  nothing would be worse.
- 2026-09-02 — **Retries are scoped to what is safe to repeat.** All GETs, plus the two POSTs
  that are idempotent by construction (`/api/v3/command` for a rescan, `/Library/...` for a
  path notification): three attempts on a timeout, a connection error or a 429/5xx, with a
  0.5/1/2 s backoff plus jitter. 401/403 is never retried (the key is wrong and will stay
  wrong) and neither is any other 4xx. **`POST /notification` is never retried either** — a
  duplicate webhook would double every future event, which is worse than a visible failure the
  user can retry deliberately. `sleep` is injected, so the retry tests take zero wall time and
  assert the actual backoff sequence.
- 2026-09-02 — **The API key must never appear in a log record or an exception message**, and
  a test asserts it over `caplog` at DEBUG plus `str()`/`repr()` of the raised error. It
  travels through every single request, so it is the one secret with that exposure.
- 2026-09-02 — A `204 No Content` is decoded as success, not an error: Jellyfin answers 204 to
  `/Library/Media/Updated`. And `MediaUpdate` is aliased to Jellyfin's PascalCase body with a
  test pinning the exact JSON, because getting that shape wrong fails **silently** — the call
  still returns 204 and nothing refreshes.
- 2026-09-02 — A base URL keeps any sub-path (`http://host/sonarr`, common behind a reverse
  proxy), gains `http://` when the scheme is missing, and is refused outright for a non-HTTP
  scheme. An unset URL or key raises `IntegrationNotConfigured` **before any socket is
  opened**, so "not configured" is distinguishable from "not reachable" — `refresh` skips on
  the former and warns on the latter.

- 2026-09-02 — **M3 step 8 (refresh, sync, backfill and the CLI) complete.**
  `pipeline/refresh.py`, `integrations/sync.py`, `api/integrations.py`, `vidcleaner/cli_arrs.py`
  (`integrations test`, `sync`, `titles list|enable|disable`, `queue list|show|cancel|retry`).
  Verified: 1475 passed, 89 of them contract.
- 2026-09-02 — **§6 step 9's "after 90 s, confirm the arr's path" cannot be a sleep in a
  stage.** It idles the single worker 90 s per job — half an hour for a twenty-episode season
  pack — and it is unresumable in either marker order: write the marker before the sleep and a
  restart skips the check, write it after and a restart re-does the whole refresh. Folded into
  the sync pass instead, which already fetches exactly this data, gated on `media_items.cleaned_at`
  being older than `mapping_check_delay_s` (default 90 — §6's number, now a scheduling
  parameter) and younger than a day. **No new column**, and unlike a sleep the window survives
  a restart. `vidcleaner sync --confirm` forces it for the demo.
- 2026-09-02 — **§3/§6's Jellyfin update type is wrong for the new name.** §6 remembers the
  `Deleted` for an old `.mp4` but leaves the new name as `Modified` — and after an MP4→MKV swap
  the `.mkv` is a path Jellyfin has never seen, so it is **`Created`**. Both updates go in one
  `/Library/Media/Updated` call, which is also cheaper given §3's ~60 s debounce.
- 2026-09-02 — **`refresh` is non-fatal by design.** By the time it runs the swap has committed
  and the library file is correct, so a failed rescan costs a stale Jellyfin entry until the
  hourly sync and nothing more. Every client error is a `warnings` entry on `refresh.json`,
  never a `StageError` — which is also what makes re-running the stage safe, the only way a
  resumed job can reach it. "Not configured" is recorded in `skipped`, deliberately distinct
  from a warning: a library with no Jellyfin is a perfectly good deployment.
- 2026-09-02 — **Adoption, the reconciliation M1's log said M3 owes, is implemented and
  tested.** Items resolve by natural key `(title_id, season, episode)` and then **by path**; a
  path hit on a row under the CLI sentinel is *re-parented*, keeping `status`, `last_job_id`,
  `cleaned_at` and `source_fingerprint`. Preserving `last_job_id` is the whole point:
  `detections.media_item_id` points at that row, so inserting a duplicate would silently orphan
  the M1 run's 49 detections from the episode M4 is going to show. When both lookups hit
  *different* rows the unique constraint forces a **merge**: the natural-key row wins and
  `detections`, `backups` and `jobs` are all re-pointed at it.
- 2026-09-02 — **SQLite reuses a deleted rowid, which can fool a test.** The merge test first
  asserted "the loser's id is gone" and failed — because episode 2's brand-new row was assigned
  the id the merge had just freed. Rewritten to assert observable facts (exactly one row owns
  the path, the natural-key row won, the history moved). Worth recording because the same trap
  applies to any code that identifies a row by id across a delete.
- 2026-09-02 — **§5's `uq_media_items_title_s_e` does not constrain movies**: SQLite treats
  NULLs as distinct, so `(title_id, NULL, NULL)` repeats indefinitely and repeated syncs would
  accumulate duplicate movie rows. Guarded in code (a movie resolves by `title_id` alone), with
  a test that syncs three times and asserts one row. A partial unique index would need a
  migration and is an M5 option.
- 2026-09-02 — **§5 cannot represent a multi-episode file.** One `episodeFile` maps to several
  `episodes[]` under scalar `season`/`episode`. M3 keys on the **lowest** `(season, episode)`
  sharing the file — stable across syncs — joins nothing, and treats `arr_file_id` as the real
  identity. A `media_item_episodes` join table is an M5 item.
- 2026-09-02 — **A title that vanishes from an arr's listing is counted, never deleted.** An
  arr that is restarting or half-migrated can return a short list, and deleting on that basis
  would erase every selection the user has made. Only a `SeriesDelete`/`MovieDelete` webhook
  disables a title. `sync_titles` also never writes `enabled` or `profile_id`, with a test that
  both survive a re-sync.
- 2026-09-02 — **`arr_path` is stored mapped to a local path, in `sync_titles` itself.** The
  first implementation mapped it in a second pass over the table, which double-applies on the
  next sync because the stored value is already local by then. Caught by writing the test.
- 2026-09-02 — **The M1 sentinel title is excluded from every sync and backfill query**
  (`arr_id >= 0`). Nothing in §8 says so, and without it the CLI's own scratch files appear as a
  Radarr movie called "Local files (CLI)" — and become eligible for backfill.
- 2026-09-02 — **§6.0's "`*FileDelete` marks item `pending`" is the wrong status**, and the
  same applies to a file the arr stops listing. `pending` means "we intend to clean it"; the
  file is gone. `stale` is the word §5/§6 already use for a vanished path, and its `kept`
  backups become `orphaned` at the same time (§13's retention path).
- 2026-09-02 — **§8's backfill gate is evaluated with no file I/O**, cheap→expensive: status
  first, then the last job's recorded `profile_hash` against the item's *current* one from
  `matcher_for`. Over-enqueueing is deliberately fine — `probe` re-reads the
  `VIDCLEANER_PROFILE_HASH` tag and returns `already_clean` in about 0.1 s (measured in the M1
  demo), so the queue is the cheap filter and the tag is the definitive one. A test asserts that
  adding an *item* whitelist puts a cleaned episode back in the queue, which is the entire
  reason the hash is per item.
- 2026-09-02 — **`sync_all` opens its own short transactions rather than taking a `Session`.**
  A single transaction spanning a large library's HTTP calls would hold SQLite's write lock for
  far longer than the worker's 5 s busy timeout allows, and the worker's claim would start
  failing with "database is locked" — the coupling step 2 recorded, now respected in the one
  place that could trip it. One arr failing does not stop the other, and one title failing does
  not stop the rest.
- 2026-09-02 — **§8's hourly sync is owned by the api, and queue/disk maintenance by the
  worker.** Split by resource, one owner each: sync and the mapping check are HTTP-and-database
  only, and the worker is blocked inside ffmpeg or STT for minutes at a time so a timer there
  fires late by however long the current stage takes; stale recovery, `/work` GC and the
  idle-gated audit enqueue need queue idleness and the volumes, which only the worker has. The
  recorded cost is that a `role=worker`-only deployment never syncs and a `role=api`-only one
  never processes — both are half a system by construction, and `role=all` is the shipped
  default. This follows the precedent already in the log for word-list seeding.
- 2026-09-02 — **M3 ships only the REST endpoints whose contract is already fully
  determined**: `POST /api/integrations/{app}/test`, `GET`/`PUT /api/path-mappings`, and
  `POST /api/library/sync`. `library`, `items` and `jobs` are listing/filter/pagination
  surfaces and belong to M4, designed around the actual screens rather than inherited from
  whatever was convenient now. Everything else the milestone demo needs is a CLI subcommand.
  `PUT /api/path-mappings` validates every app's rules *before* deleting anything, so a
  duplicate prefix cannot leave half a mapping table behind.
- 2026-09-02 — `POST /api/integrations/{app}/test` reads the **stored** key when the body's is
  `***`, mirroring `save_settings`: the API masks secrets, so the form may never have seen the
  real value and posting it back must not be read as "test with the literal `***`".
- 2026-09-02 — **The CLI now migrates before touching the database.** The api and worker both
  do it at startup, so only the CLI could meet an old schema — and it did: after migration 0002
  every `queue`/`titles` command on a dev checkout failed with "no such column: jobs.retry_at".
  `Settings.auto_migrate` already existed for exactly this ("mainly smooths dev runs"); a
  failure warns rather than showing a traceback.

- 2026-09-02 — **M3 step 9 (the worker run loop) complete.** `worker/runner.py`,
  `worker/scheduler.py`, `worker/gc.py`, and `tests/support/stages/` -- a complete fake stage
  registry. Verified: 1503 passed, including a real worker-driven clean-and-swap of the
  generated fixture through real ffmpeg, and a kill/resume test.
- 2026-09-02 — **The runner walks the stages itself rather than calling `run_pipeline`.** It
  needs to write `jobs.state` per stage, check for a cancellation between them, and re-weight
  the progress bar when a job is promoted to a full-file pass; `run_pipeline` stays the CLI's
  path and the one the tests use. §4's "resume from the last completed stage marker" therefore
  works exactly as before, because `run_stage` is still the only thing that reads or writes a
  marker.
- 2026-09-02 — **`tests/support/stages/` is the payoff of `StageContext.stage_registry` being
  injectable** (M1 step 2 made it per-context "so the worker need not touch
  `_STAGE_MODULES`"). Nine tiny modules that write the *real* artifact models mean the whole
  run loop -- state transitions, progress, the timeline, retry classification, resume,
  persistence, work-dir pruning -- is exercised in under a second with no ffmpeg and no torch.
  `swap` and `refresh` are **not** faked there: both already have their own injection seams
  (`FsOps`, `httpx.MockTransport`), so faking them again would only test the fake.
- 2026-09-02 — **Three real bugs found by that test rather than by review.** (a) `persist_run`
  never cleared `claimed_by`/`heartbeat`, so a finished job stayed marked as owned by a worker
  and §9.1's Queue page would have shown it as running forever; it now applies the same rule
  `claim.set_state` does. (b) `plan_swap` stat'ed the source *before* `preflight` could look at
  it, so a file that vanished between render and swap surfaced as a bare `OSError` — which
  `classify` reads as a terminal swap failure — instead of `StaleSourceError`, which §6 says
  should re-resolve and requeue once. (c) **The worst one:** `run_job` seeded its outcome with
  `"done"` and recorded it in `finally`, so anything escaping the stage machine that
  `poll_once` does not catch — a `BaseException` such as `KeyboardInterrupt` — marked an
  unfinished job **done** and its item **clean**, with no swap having happened. The outcome is
  now `None` until `_drive` returns, and an escape records nothing and leaves the claim, which
  is precisely what stale recovery expects from a killed process.
- 2026-09-02 — **A stage transition is published to the database immediately, not on the next
  heartbeat tick.** There are at most ten per job — nothing beside ffmpeg's
  roughly-per-second progress callbacks — and they are the moments §9.1's Queue page actually
  needs. Without it a job that finishes inside one 5 s tick never records a stage at all: the
  row still said `probing` when it was done, and a crashed job's `state` was no guide to where
  it stopped. Found by the kill/resume test.
- 2026-09-02 — **§6.1's stability wait and free-space check are now wired in, and both were
  dead.** `probe.wait_for_stable` had zero callers; it runs for `trigger == "webhook"` only,
  because a `Download` can fire while Sonarr is still hardlinking and everything else would pay
  at least 10 s for nothing. `check_free_space` only logged a warning; for a non-dry-run job it
  is now a real precondition that fails the job before any work.
- 2026-09-02 — **§4's `already_clean` short-circuit now exists outside the CLI.** `parse_probe`
  has computed the flag since M1 and nothing acted on it; the runner stops after `probe`, and
  `force` suppresses it. An integration test drives it through a real swap: cleaning the same
  file twice reports `already_clean` the second time, which closes §4's idempotency loop for
  the worker.
- 2026-09-02 — **§4's `render_parallel` is not achievable in the current stage driver and now
  says so.** `run_pipeline` is strictly sequential over one work dir; overlapping render N with
  STT N+1 would need two claim lanes plus semaphores. M3 runs serially and writes a job-timeline
  warning when the setting is above 1 — the same treatment M2 gave `vad_filter` rather than
  leaving a setting that silently does nothing.
- 2026-09-02 — **Nothing reclaimed `/work`, and PLAN.md never mentions it.** This is the
  failure that would actually take the box down: `out.mkv` is source-sized (4.57 GiB in the M1
  demo) plus roughly 110 MB per hour of `audio.wav`, and §10 puts `/work` on a cache SSD.
  `worker/gc.py` *prunes* a finished job — `out.mkv`, `audio.wav`, `graph.txt`, `subs/`,
  `redacted/` — and keeps `job.json`, the JSON artifacts, `ffmpeg.log`, the markers and
  `snippets/`, because §6 step 10 puts the UI's snippet audio there and M4 reads
  `detections.json` back. Whole directories are collected only once the job has been terminal
  for a week, and an *unknown* directory (a CLI run's) only when it is that old too, so a run in
  progress is never touched.
- 2026-09-02 — **Periodic work is split by resource, one owner each.** The worker owns what
  needs queue idleness or the volumes — stale recovery, `/work` collection, §6's audit pass —
  and `Scheduler.tick()` is called only when `poll_once` found nothing, so every task inherits
  "runs when the queue is idle" for free, which is exactly what §6 requires of the audit pass.
  The api owns the hourly arr sync, because a timer in the worker fires however late the current
  ffmpeg or STT stage happens to be. Last-run times are in memory: a restart re-running one of
  these is harmless and much cheaper than a table to persist them.
- 2026-09-02 — The audit pass enqueues **one item per tick** and only for items whose last job
  actually ran in `windowed` mode — a full pass over a file that already had one would find the
  same thing at the same cost — and never twice for the same item. §6 says it "runs only when no
  normal jobs are queued", which the scheduler's idle gating gives it directly.
- 2026-09-02 — `Worker` gained a `transcriber` parameter, the same seam `build_context` already
  exposes. It is what lets the ffmpeg integration tier drive a real job end to end (claim →
  stages → real swap → backups row) without downloading a 2 GB model, using the
  `ScriptedTranscriber` that already ships in production for `--transcript`.
- 2026-09-02 — `claim.SWAP_RECONCILER` is process-wide state that `Worker.__init__` sets (so the
  queue, which the api imports for webhooks, never has to import the pipeline). That leaks
  between tests, so `tests/unit/test_claim.py` resets it around each test — the *unset*
  behaviour is itself under test there, since defaulting an interrupted swap to `failed` is the
  safe direction.

- 2026-09-02 — **M3 step 10 (webhook receivers) complete.** `integrations/payloads.py`,
  `api/webhooks.py` (`/sonarr`, `/radarr`, `/setup`, `/install`), `ensure_webhook_token`, and
  the api-owned hourly sync task in `main.lifespan`.
- 2026-09-02 — **`webhook_token` is generated at seed time**, so the receiver can *always*
  require it. The alternative — accept deliveries while it is unset — is an unauthenticated
  job-enqueue endpoint on first boot. A bad or missing token gets `401` and **nothing is
  stored**: writing unauthenticated bodies to the database is a denial-of-service vector on an
  endpoint anyone can reach.
- 2026-09-02 — **The application has no user authentication of any kind, and PLAN.md never says
  so**, which makes the webhook secret the entire security perimeter and `GET
  /api/webhooks/setup` (which hands the token out in plaintext) only acceptable behind a reverse
  proxy. Recorded here because it is a property of the whole design, not of this endpoint; M5's
  README work should say it out loud.
- 2026-09-02 — **`Test` is dispatched before any title resolution**, and an unknown `eventType`
  before it too. Test payloads carry dummy ids (`series.id = 1`), so resolving first would let
  a Test click enqueue a real job against a fake path on any install whose series 1 exists — a
  test asserts exactly that, with a real series 1 present. Answering an unrecognised event
  early also makes the recorded note say `unhandled:<type>` rather than the less useful
  `unknown_title`.
- 2026-09-02 — **Webhook dispatch is pure database — no outbound HTTP at all.** That is what
  makes §6.0's "respond 200 immediately" true with no BackgroundTask: an unknown title records
  `unknown_title` and the hourly sync adopts it, rather than blocking the receiver on a
  possibly-hung Sonarr. Receivers always answer `200` except `401`/`400`, because Sonarr
  disables a notification after repeated failures — and a dispatch exception is caught, recorded
  on the event row with `handled=False`, and still answered `200`.
- 2026-09-02 — **A `Download` goes through the same `resolve_item` the sync uses**, so a webhook
  can never create a row the sync would then have to merge. A disabled title still gets its
  `media_items` row upserted (§12's "recorded but not queued") so §9.2's "12/24 clean" is right
  the moment the user enables it.
- 2026-09-02 — **§6.0's dedupe window is pinned by two tests that show what it does and does
  not do.** A ten-event season pack produces **ten** jobs and ten distinct paths — every event
  names a different file, so the window collapses nothing there. A multi-episode file (several
  `Download` events sharing one `episodeFile`) produces **one**, which is what the window
  actually protects against, along with a duplicate delivery.
- 2026-09-02 — `Rename` is keyed on `arr_file_id`, which is authoritative, with `previousPath`
  as the fallback — and it never enqueues. This is the same fact that forces a restore to
  target the item's *current* path rather than `backups.original_path`.
- 2026-09-02 — **`event=` cannot be passed as a keyword to structlog**, which reserves it for
  the message itself; doing so raises `TypeError` from inside the logger. It cost twenty test
  failures whose tracebacks pointed at an unrelated `except ValueError`. Renamed to
  `event_type=`; worth remembering for every future log call about arr events.
- 2026-09-02 — **§8's hourly sync runs in the api's `lifespan`** as an asyncio task that calls
  the blocking sync through `asyncio.to_thread`, waits its interval *before* the first run (a
  container restart should not stampede the arrs), and never dies on an exception. The interval
  is `sync_interval_minutes` in deployment config, since it is read once at process start.

- 2026-09-02 — **M3 COMPLETE. Demo recorded.** §11 asks for "enable a series → existing episodes
  cleaned; Sonarr imports a new episode → auto-cleaned". Run against a **real HTTP server**
  standing in for Sonarr (Darick has the real one on unraid), so every layer below that is
  genuine: the CLI, sync, the queue, the worker, real ffmpeg 9.0, the real rename transaction
  and the webhook receiver. Three generated fixture episodes in a `/media`-shaped tree, with
  `/tv → <media>/tv` path mapping configured:

  | step | result |
  |---|---|
  | `vidcleaner integrations test` | `sonarr ok 4.0.10.2544`, radarr/jellyfin `not configured` |
  | `vidcleaner sync` | 1 title added, **disabled**, `arr_path` stored mapped to local |
  | `vidcleaner titles enable --arr-id 42` | 3 items adopted, **3 backfill jobs queued at priority 200** |
  | worker | 3 jobs `done`; per-job timings probe 0.03 / render 0.08 / verify 0.23 / swap 0.002 s |
  | library | 3 files replaced, `a:0 ac3 Clean default=1 eng`, `a:1 Original`, `a:2 Commentary` intact, `VIDCLEANER=1` |
  | backups | 3 rows `kept`, each byte-identical to its original |
  | webhook `Download` | `{"ok": true, "note": "created", "job_id": ...}` → worker → S01E04 `clean`, trigger `webhook` |
  | duplicate delivery | `{"note": "deduped"}`, same job id |
  | wrong token | **HTTP 401**, no `webhook_events` row |
  | `vidcleaner sync --confirm` | **4 mapping checks, 0 mismatched** — §6 step 9 without a sleep |
  | `vidcleaner restore --item 1` | 1785323 → 980401 bytes, backup `restored`, cleaned copy moved to `/backups` as `.cleaned`, **exactly 4 files in the folder** |

  What the fake Sonarr actually received, in order: `system/status`, `series`, `episodefile`,
  `episode`, **four `RescanSeries` commands** (one per cleaned file), then on the confirm pass
  `series`/`episodefile`/`episode` and `episodefile/{501..504}`. `/work` held 4 pruned
  directories totalling **144 KiB** — the `out.mkv` files (1.7 MiB each) were reclaimed.

  The job timeline for one episode reads end to end: `queued (backfill, priority 200)` →
  `claimed` → each stage with its elapsed time → `subtitles source=… mode=full
  (subtitles_unusable)` → `transcribed` → `detected detections=3 muted=3` → `swapped final=… backup=…`
  → `refreshed arr=sonarr command=9 skipped=['jellyfin_not_configured']`.

  **Still owed** (per the session's agreement, the same way "plays in Infuse" is): the live half
  of the demo on the unraid box — `vidcleaner integrations test` against the real Sonarr, Radarr
  and Jellyfin, a real import, and confirming **Jellyfin shows Clean as the default track**.
  Everything up to the HTTP boundary is proven here; only the arrs themselves are simulated.
  The commands are `vidcleaner integrations test`, `vidcleaner sync`,
  `vidcleaner titles enable --name "<series>"`, `vidcleaner queue list`, and
  `vidcleaner restore --path <file>`.

- 2026-09-02 — **M4 step 1 (the `snippets` stage) complete.** `pipeline/snippets.py` cuts two
  5 s AAC clips per detection from `audio.wav` — `orig.m4a` and `clean.m4a`, the latter with
  only *that* detection's mute applied — plus a `wave.png` waveform with the muted span
  highlighted, in one ffmpeg invocation per detection. `M4_STAGES = M3_STAGES + snippets` and the
  worker runs it last. Verified by `tests/integration/test_snippets.py`: `volumedetect` inside the
  mute window of `clean.m4a` is inaudible while the same window of `orig.m4a` is not, and a control
  window outside it survives — the same tripwire §12 uses for the render.
- 2026-09-02 — **Snippets live in `/config/snippets/<job id>/`, not `/work`.** `gc.py` deletes a
  finished job's whole work dir after 7 days, and the detections those clips illustrate live in the
  database forever; the review UI is the entire point of M4, so a play button that dies after a week
  is not acceptable. Cost is a few MB per movie. `detections.snippet_path` therefore stores a path
  *relative to `Settings.snippets_dir`* (`<job id>/<nnnn>`), so moving the root does not invalidate
  the rows. Consequences: the stage is **not** a pure function of its `/work` inputs — like `swap`
  and `refresh` it is idempotent instead — and `Workspace.snippets_dir` is gone.
- 2026-09-02 — **`snippets` is the third module allowed to cross clocks**, because it cuts from
  0-based `audio.wav` using container-time detections. `artifacts.py`'s ONE CLOCK note,
  `pipeline/__init__.py` and CLAUDE.md now say so; the list is `stt` (applies), `render` and
  `snippets` (undo).
- 2026-09-02 — **A snippet failure is a warning, never a job failure.** The stage runs after `swap`
  has committed, so the library file is already correct and refusing the job would strand a good
  clean. A pruned work dir (no `audio.wav`) records `skipped=["no_audio"]` for the same reason.

## 15. Working agreement for future sessions

1. Read `PLAN.md` §2 (locked decisions) and §11 (next unchecked milestone) before coding.
2. Work one milestone at a time; update checkboxes and the Decision Log in the same commit as the code.
3. Any new external dependency, schema change, or change to the output-file layout gets a Decision Log line.
4. Run `uv run pytest` and `npm test` before ticking a milestone; record the milestone demo result in the Decision Log.
