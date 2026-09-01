/** Thin fetch wrapper for the VidCleaner API. */

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

export async function apiGet<T>(path: string): Promise<T> {
  const response = await fetch(`/api${path}`);
  if (!response.ok && response.status !== 503) {
    throw new ApiError(`GET ${path} failed`, response.status);
  }
  return (await response.json()) as T;
}

export async function apiPatch<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(`/api${path}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    throw new ApiError(`PATCH ${path} failed`, response.status);
  }
  return (await response.json()) as T;
}

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

export const getHealth = () => apiGet<Health>("/health");
