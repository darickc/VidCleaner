/**
 * The shapes `/api` returns, mirroring the pydantic models in `vidcleaner/api/`.
 *
 * Hand-written rather than generated: the surface is small, and a generator would
 * pull a build step into a project whose whole frontend is four screens. When a
 * response model changes, change it here — the pages are typed against these.
 */

export interface DiskInfo {
  path: string;
  exists: boolean;
  free_bytes: number | null;
  total_bytes: number | null;
}

export interface Health {
  status: "ok" | "degraded" | "error";
  version: string;
  role: string;
  database: { ok: boolean; error: string | null; revision: string | null };
  ffmpeg: { present: boolean; version: string | null; path: string | null };
  disk: Record<"config" | "media" | "backups" | "work", DiskInfo>;
}

export interface ItemRef {
  id: number;
  title_id: number;
  title: string;
  kind: string;
  label: string;
  season: number | null;
  episode: number | null;
  episode_title: string | null;
  path: string;
  size: number | null;
  duration: number | null;
  status: string;
  last_job_id: string | null;
  cleaned_at: string | null;
}

export interface JobSummary {
  id: string;
  media_item_id: number;
  item: ItemRef | null;
  trigger: string;
  state: string;
  stage: string | null;
  progress_pct: number;
  priority: number;
  attempts: number;
  dry_run: boolean;
  force: boolean;
  stt_mode: string | null;
  model_used: string | null;
  subtitle_source: string | null;
  claimed_by: string | null;
  error: string | null;
  detections: number | null;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  heartbeat: string | null;
  retry_at: string | null;
}

export interface JobLogLine {
  id: number;
  ts: string | null;
  level: string;
  msg: string;
}

export interface JobDetail extends JobSummary {
  work_dir: string | null;
  source_fingerprint: string | null;
  timings: Record<string, number>;
  profile: Record<string, unknown>;
  logs: JobLogLine[];
}

export interface QueueView {
  running: JobSummary[];
  queued: JobSummary[];
  recent: JobSummary[];
  queued_total: number;
}

export interface TitleRow {
  id: number;
  kind: string;
  title: string;
  year: number | null;
  poster_url: string | null;
  enabled: boolean;
  profile_id: number | null;
  arr_id: number;
  arr_path: string | null;
  tvdb_id: number | null;
  tmdb_id: number | null;
  item_count: number;
  clean_count: number;
  failed_count: number;
  pending_count: number;
  last_synced_at: string | null;
}

export interface TitleList {
  titles: TitleRow[];
  total: number;
}

export interface WordCount {
  word_canonical: string;
  category: string;
  total: number;
  muted: number;
  suspicious?: number;
}

export interface ItemRow extends ItemRef {
  detection_count: number;
}

export interface TitleDetail {
  title: TitleRow;
  profile_name: string | null;
  items: ItemRow[];
  counts: WordCount[];
}

export interface DetectionRow {
  id: number;
  word_raw: string;
  word_canonical: string;
  category: string;
  start_s: number;
  end_s: number;
  mute_start_s: number;
  mute_end_s: number;
  source: string;
  confidence: number | null;
  muted: boolean;
  whitelisted: boolean;
  suspicious: boolean;
  subtitle_cue_idx: number | null;
  /** URL prefix of the review clips, or null when there is nothing to play. */
  snippet: string | null;
}

export interface WhitelistRow {
  id: number;
  scope: string;
  scope_id: number | null;
  canonical_word: string;
  context_text: string | null;
}

export interface BackupRow {
  id: number;
  backup_path: string;
  original_path: string;
  size: number | null;
  state: string;
  created_at: string | null;
  purge_after: string | null;
}

export interface ItemDetail {
  item: ItemRef;
  title: TitleRow | null;
  job: JobSummary | null;
  jobs: JobSummary[];
  counts: WordCount[];
  detections: DetectionRow[];
  whitelist: WhitelistRow[];
  backups: BackupRow[];
  restorable: boolean;
}

export type ActionName = "process" | "reprocess" | "dry_run" | "restore";

export interface ActionResult {
  action: string;
  queued: string[];
  skipped: Record<string, number>;
  restored: number[];
  considered: number;
  warnings: string[];
}

export interface TitlePatchResult {
  id: number;
  enabled: boolean;
  profile_id: number | null;
  queued: string[];
}

export interface WhitelistResult extends WhitelistRow {
  created: boolean;
  job_id: string | null;
}

export interface TestResponse {
  app: string;
  ok: boolean;
  version: string | null;
  detail: string;
  latency_ms: number;
}

export interface WebhookSetup {
  url: string;
  header_name: string;
  token: string;
  note: string;
}

export interface PathMapping {
  app: string;
  from_prefix: string;
  to_prefix: string;
}

/** `/api/settings` is a flat bag of scalars; the form knows the field names. */
/** §9.6's "backup retention + purge". */
export interface BackupSummary {
  total: number;
  total_bytes: number;
  by_state: Record<string, number>;
  bytes_by_state: Record<string, number>;
  expired: number;
  expired_bytes: number;
  orphaned: number;
  orphaned_bytes: number;
  retention_days: number;
  keeps_forever: boolean;
  backups_dir: string;
}

export interface BackupRow {
  id: number;
  media_item_id: number;
  label: string;
  original_path: string;
  backup_path: string;
  size: number | null;
  state: string;
  exists: boolean;
  created_at: string | null;
  purge_after: string | null;
  expired: boolean;
}

export interface BackupList {
  summary: BackupSummary;
  backups: BackupRow[];
}

export interface PurgeResult {
  scope: string;
  purged: number;
  freed_bytes: number;
  missing: number;
  warnings: string[];
}

export type AppSettings = Record<string, string | number | boolean>;
