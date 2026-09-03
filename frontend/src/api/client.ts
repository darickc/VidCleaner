/**
 * Thin fetch wrapper plus one function per endpoint.
 *
 * The functions exist so pages never build URLs: a route the backend renamed should
 * break the build here, once, rather than in four `useQuery` calls.
 */

import type {
  ActionName,
  ActionResult,
  AppSettings,
  Health,
  ItemDetail,
  JobDetail,
  PathMapping,
  QueueView,
  TestResponse,
  TitleDetail,
  TitleList,
  TitlePatchResult,
  WebhookSetup,
  WhitelistResult,
} from "./types";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly detail?: unknown,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, init);
  if (response.status === 204) return undefined as T;
  const body = await response.text();
  let parsed: unknown = undefined;
  try {
    parsed = body ? JSON.parse(body) : undefined;
  } catch {
    parsed = body;
  }
  // 503 is the health endpoint reporting a broken database: the body still explains
  // why, and showing it beats showing "could not reach the API".
  if (!response.ok && response.status !== 503) {
    throw new ApiError(detailOf(parsed) ?? `${path} failed`, response.status, parsed);
  }
  return parsed as T;
}

function detailOf(parsed: unknown): string | undefined {
  if (parsed && typeof parsed === "object" && "detail" in parsed) {
    const detail = (parsed as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail) && detail.length > 0) {
      const first = detail[0] as { msg?: string };
      if (first?.msg) return first.msg;
    }
  }
  return undefined;
}

export const apiGet = <T>(path: string) => request<T>(path);

const send = <T>(method: string, path: string, body?: unknown) =>
  request<T>(path, {
    method,
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });

export const apiPost = <T>(path: string, body?: unknown) => send<T>("POST", path, body);
export const apiPatch = <T>(path: string, body: unknown) => send<T>("PATCH", path, body);
export const apiPut = <T>(path: string, body: unknown) => send<T>("PUT", path, body);
export const apiDelete = <T>(path: string) => send<T>("DELETE", path);

// ------------------------------------------------------------------ endpoints

export const getHealth = () => apiGet<Health>("/health");

export const getQueue = () => apiGet<QueueView>("/jobs");
export const getJob = (jobId: string) => apiGet<JobDetail>(`/jobs/${jobId}`);
export const cancelJob = (jobId: string) => apiPost<{ cancelled: boolean }>(`/jobs/${jobId}/cancel`);
export const retryJob = (jobId: string) => apiPost<ActionResult>(`/jobs/${jobId}/retry`);
export const setJobPriority = (jobId: string, priority: number) =>
  apiPatch<{ updated: boolean }>(`/jobs/${jobId}`, { priority });

export interface TitleQuery {
  kind?: "series" | "movie";
  q?: string;
  enabled?: boolean;
}

export const getTitles = ({ kind, q, enabled }: TitleQuery = {}) => {
  const params = new URLSearchParams();
  if (kind) params.set("kind", kind);
  if (q) params.set("q", q);
  if (enabled !== undefined) params.set("enabled", String(enabled));
  const query = params.toString();
  return apiGet<TitleList>(`/library/titles${query ? `?${query}` : ""}`);
};

export const getTitle = (titleId: number) => apiGet<TitleDetail>(`/library/titles/${titleId}`);

export const patchTitle = (
  titleId: number,
  patch: { enabled?: boolean; profile_id?: number; clear_profile?: boolean },
) => apiPatch<TitlePatchResult>(`/library/titles/${titleId}`, patch);

export const titleAction = (titleId: number, action: ActionName) =>
  apiPost<ActionResult>(`/library/titles/${titleId}/actions`, { action });

export const syncLibrary = () => apiPost<Record<string, unknown>>("/library/sync");

export const getItem = (itemId: number, jobId?: string) =>
  apiGet<ItemDetail>(`/items/${itemId}${jobId ? `?job_id=${encodeURIComponent(jobId)}` : ""}`);

export const itemAction = (itemId: number, action: ActionName) =>
  apiPost<ActionResult>(`/items/${itemId}/actions`, { action });

export const addWhitelist = (
  itemId: number,
  body: { canonical_word: string; scope: string; reprocess: boolean },
) => apiPost<WhitelistResult>(`/items/${itemId}/whitelist`, body);

export const deleteWhitelist = (entryId: number) => apiDelete<void>(`/whitelist/${entryId}`);

export const getSettings = () => apiGet<AppSettings>("/settings");
export const patchSettings = (patch: Partial<AppSettings>) =>
  apiPatch<AppSettings>("/settings", patch);

export const testIntegration = (app: string, body?: { url?: string; api_key?: string }) =>
  apiPost<TestResponse>(`/integrations/${app}/test`, body ?? {});

export const getWebhookSetup = (app: string) =>
  apiGet<WebhookSetup>(`/webhooks/setup?app=${app}`);
export const installWebhook = (app: string) =>
  apiPost<{ created: boolean; id: number; url: string }>(`/webhooks/install?app=${app}`);

export const getPathMappings = () => apiGet<PathMapping[]>("/path-mappings");
export const putPathMappings = (mappings: PathMapping[]) =>
  apiPut<PathMapping[]>("/path-mappings", mappings);

export type { Health, DiskInfo } from "./types";
