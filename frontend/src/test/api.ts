/**
 * A fetch stub keyed by path, plus the fixtures the pages expect.
 *
 * Keyed rather than "always return this": every M4 page makes several calls, and a
 * single-payload stub makes a page appear to work while it is really reading the
 * wrong response.
 */

import { vi } from "vitest";

export type Route = unknown | ((init: RequestInit | undefined) => unknown);

export interface MockOptions {
  /** Paths (without the `/api` prefix) mapped to a body, or to a function of the
   * request. A key ending in `*` matches by prefix. */
  routes: Record<string, Route>;
  status?: number;
}

export interface MockedApi {
  calls: Array<{ method: string; path: string; body: unknown }>;
}

export function mockApi({ routes, status = 200 }: MockOptions): MockedApi {
  const calls: MockedApi["calls"] = [];

  const match = (path: string): Route | undefined => {
    if (path in routes) return routes[path];
    const prefix = Object.keys(routes)
      .filter((key) => key.endsWith("*"))
      .sort((a, b) => b.length - a.length)
      .find((key) => path.startsWith(key.slice(0, -1)));
    return prefix === undefined ? undefined : routes[prefix];
  };

  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, init?: RequestInit) => {
      const path = String(input).replace(/^\/api/, "");
      calls.push({
        method: init?.method ?? "GET",
        path,
        body: init?.body ? JSON.parse(String(init.body)) : undefined,
      });
      const route = match(path);
      if (route === undefined) {
        return new Response(JSON.stringify({ detail: `no stub for ${path}` }), {
          status: 404,
        });
      }
      const body =
        typeof route === "function"
          ? (route as (i?: RequestInit) => unknown)(init)
          : route;
      return new Response(JSON.stringify(body), { status });
    }),
  );
  return { calls };
}

// ------------------------------------------------------------------ fixtures

export const HEALTH = {
  status: "degraded",
  version: "0.1.0",
  role: "all",
  database: { ok: true, error: null, revision: "0001" },
  ffmpeg: { present: false, version: null, path: null },
  disk: {
    config: {
      path: "/config",
      exists: true,
      free_bytes: 2 ** 30,
      total_bytes: 2 ** 40,
    },
    media: {
      path: "/media",
      exists: true,
      free_bytes: 2 ** 30,
      total_bytes: 2 ** 40,
    },
    backups: {
      path: "/backups",
      exists: true,
      free_bytes: 2 ** 30,
      total_bytes: 2 ** 40,
    },
    work: {
      path: "/work",
      exists: true,
      free_bytes: 2 ** 30,
      total_bytes: 2 ** 40,
    },
  },
};

export const EMPTY_QUEUE = {
  running: [],
  queued: [],
  recent: [],
  queued_total: 0,
};

export function item(overrides: Record<string, unknown> = {}) {
  return {
    id: 7,
    title_id: 3,
    title: "The Wire",
    kind: "episode",
    label: "The Wire S01E01 — The Target",
    season: 1,
    episode: 1,
    episode_title: "The Target",
    path: "/media/tv/The Wire/S01E01.mkv",
    size: 2 ** 30,
    duration: 3600,
    status: "clean",
    last_job_id: "job-1",
    cleaned_at: "2026-09-01T12:00:00Z",
    ...overrides,
  };
}

export function job(overrides: Record<string, unknown> = {}) {
  return {
    id: "job-1",
    media_item_id: 7,
    item: item(),
    trigger: "webhook",
    state: "queued",
    stage: null,
    progress_pct: 0,
    priority: 100,
    attempts: 0,
    dry_run: false,
    force: false,
    stt_mode: "windowed",
    model_used: null,
    subtitle_source: null,
    claimed_by: null,
    error: null,
    detections: null,
    created_at: "2026-09-01T11:59:00Z",
    started_at: null,
    finished_at: null,
    heartbeat: null,
    retry_at: null,
    ...overrides,
  };
}

export function title(overrides: Record<string, unknown> = {}) {
  return {
    id: 3,
    kind: "series",
    title: "The Wire",
    year: 2002,
    poster_url: null,
    enabled: true,
    profile_id: null,
    arr_id: 12,
    arr_path: "/media/tv/The Wire",
    tvdb_id: 1000,
    tmdb_id: null,
    item_count: 2,
    clean_count: 1,
    failed_count: 0,
    pending_count: 1,
    last_synced_at: "2026-09-01T10:00:00Z",
    ...overrides,
  };
}

export function detection(overrides: Record<string, unknown> = {}) {
  return {
    id: 1,
    word_raw: "shit",
    word_canonical: "shit",
    category: "strong",
    start_s: 93.4,
    end_s: 93.8,
    mute_start_s: 93.32,
    mute_end_s: 93.92,
    source: "both",
    confidence: 0.94,
    muted: true,
    whitelisted: false,
    suspicious: false,
    subtitle_cue_idx: 12,
    snippet: "/api/media/snippets/job-1/0000",
    ...overrides,
  };
}

/** §9.6's backups panel. Overrides spread last, as everywhere in this module. */
export const backups = (overrides: Record<string, unknown> = {}) => ({
  summary: {
    total: 3,
    total_bytes: 6 * 1024 ** 3,
    by_state: { kept: 3 },
    bytes_by_state: { kept: 6 * 1024 ** 3 },
    expired: 1,
    expired_bytes: 2 * 1024 ** 3,
    orphaned: 0,
    orphaned_bytes: 0,
    retention_days: 30,
    keeps_forever: false,
    backups_dir: "/backups",
    ...((overrides.summary as Record<string, unknown>) ?? {}),
  },
  backups: [],
  ...overrides,
});

/** §9.5's Words page. One builtin, one disabled builtin, one compound, one phrase. */
export const wordList = (overrides: Record<string, unknown> = {}) => ({
  categories: ["strong", "mild", "religious", "slurs", "sexual"],
  default_categories: ["religious", "sexual", "slurs", "strong"],
  words: [
    wordEntry({ id: 1, canonical: "fuck", category: "strong" }),
    wordEntry({
      id: 2,
      canonical: "motherfucker",
      category: "strong",
      parent: "fuck",
      forms: ["motherfuckers", "motherfucker"],
    }),
    wordEntry({
      id: 3,
      canonical: "bloody",
      category: "mild",
      enabled: false,
      note: "far too common in British English to mute by default",
    }),
    wordEntry({
      id: 4,
      canonical: "son of a bitch",
      category: "strong",
      is_phrase: true,
      focus: ["bitch"],
    }),
  ],
  counts: { strong: 3, mild: 1, religious: 0, slurs: 0, sexual: 0 },
  enabled_counts: { strong: 3, mild: 0, religious: 0, slurs: 0, sexual: 0 },
  ...overrides,
});

export const wordEntry = (overrides: Record<string, unknown> = {}) => ({
  id: 1,
  canonical: "fuck",
  category: "strong",
  forms: ["fuck"],
  is_phrase: false,
  is_builtin: true,
  enabled: true,
  focus: [],
  parent: null,
  note: null,
  ...overrides,
});

export const profile = (overrides: Record<string, unknown> = {}) => ({
  id: 1,
  name: "Default",
  categories: ["strong", "slurs", "sexual", "religious"],
  extra_word_ids: [],
  pad_pre_ms: 80,
  pad_post_ms: 120,
  is_default: true,
  titles: 0,
  ...overrides,
});

export const whitelistEntry = (overrides: Record<string, unknown> = {}) => ({
  id: 1,
  scope: "global",
  scope_id: null,
  canonical_word: "god",
  context_text: null,
  mode: "suppress",
  label: null,
  ...overrides,
});
