/**
 * The handful of primitives every M4 screen needs.
 *
 * Deliberately small and un-abstracted: four pages do not justify a component
 * library, and a status badge whose colours live in one map is the difference
 * between "failed" being red everywhere and red in three of four places.
 */

import type { ButtonHTMLAttributes, ReactNode } from "react";

export function gib(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return "—";
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(0)} KiB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(0)} MiB`;
  return `${(bytes / 1024 ** 3).toFixed(1)} GiB`;
}

/** `93.4` -> `1:33.400`. Detection times are read against a player, so keep the ms. */
export function timecode(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "—";
  const whole = Math.floor(seconds);
  const hours = Math.floor(whole / 3600);
  const minutes = Math.floor((whole % 3600) / 60);
  const rest = (seconds - hours * 3600 - minutes * 60).toFixed(1).padStart(4, "0");
  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, "0")}:${rest}`
    : `${minutes}:${rest}`;
}

export function duration(seconds: number | null | undefined): string {
  if (!seconds) return "—";
  const minutes = Math.round(seconds / 60);
  return minutes >= 60 ? `${Math.floor(minutes / 60)}h ${minutes % 60}m` : `${minutes}m`;
}

/** Times arrive tagged UTC; render them in the viewer's zone. */
export function when(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString();
}

export function ago(value: string | null | undefined): string {
  if (!value) return "—";
  const then = new Date(value).getTime();
  if (Number.isNaN(then)) return "—";
  const seconds = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86_400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86_400)}d ago`;
}

/** The separators `mask_text` preserves (backend/vidcleaner/matching/compiler.py:400). */
const NOT_SEPARATOR = /[^ \t\xa0'’-]+/gu;

/**
 * `fuck` -> `f**k`, `son of a bitch` -> `s*n o* a b***h`.
 *
 * First and last character survive so the word stays identifiable on a review screen; the
 * middle is starred. Separators split tokens and pass through untouched, matching the
 * backend's `mask_text`, so a phrase still reads as a phrase. A two-letter token keeps only
 * its first character -- both of its characters are "first and last", so the rule as written
 * would render it whole, and nothing may reach the DOM unmasked.
 */
export function maskWord(word: string): string {
  return word.replace(NOT_SEPARATOR, (token) => {
    const chars = Array.from(token);
    if (chars.length < 2) return token;
    const last = chars.length > 2 ? chars[chars.length - 1] : "*";
    return chars[0] + "*".repeat(chars.length - 2) + last;
  });
}

/**
 * Best-effort masking inside prose: a `note` or a `context_text` is free text, but the row's
 * own words are known, so at least those are covered. Longest term first, so `son of a bitch`
 * wins over `bitch`; the match itself is masked, so its case and length survive.
 */
export function maskIn(text: string, terms: ReadonlyArray<string | null | undefined>): string {
  const unique = [
    ...new Set(terms.map((t) => t?.trim()).filter((t): t is string => !!t)),
  ].sort((a, b) => b.length - a.length);
  if (unique.length === 0) return text;
  const body = unique
    .map((t) =>
      t
        .replace(/[.*+?^${}()|[\]\\]/g, "\\$&")
        .replace(/[ \t\xa0'’-]+/g, "[ \\t\\xa0'\\u2019-]+"),
    )
    .join("|");
  // The leading separator is captured rather than looked behind: same effect, no lookbehind,
  // and re-emitting it means two adjacent occurrences both match.
  const boundary = new RegExp(`(^|[^\\p{L}\\p{N}])(${body})(?![\\p{L}\\p{N}])`, "giu");
  return text.replace(boundary, (_all, before: string, hit: string) => before + maskWord(hit));
}

const TONE: Record<string, string> = {
  ok: "bg-emerald-500/15 text-emerald-300 ring-emerald-500/30",
  busy: "bg-sky-500/15 text-sky-300 ring-sky-500/30",
  warn: "bg-amber-500/15 text-amber-300 ring-amber-500/30",
  bad: "bg-rose-500/15 text-rose-300 ring-rose-500/30",
  idle: "bg-slate-500/15 text-slate-300 ring-slate-500/30",
};

/** One place decides what colour a state is, so it cannot drift between screens. */
export const STATE_TONE: Record<string, keyof typeof TONE> = {
  ok: "ok",
  clean: "ok",
  done: "ok",
  already_clean: "ok",
  degraded: "warn",
  pending: "idle",
  untracked: "idle",
  restored: "idle",
  cancelled: "idle",
  queued: "idle",
  stale: "warn",
  error: "bad",
  failed: "bad",
};

export function tone(state: string): keyof typeof TONE {
  return STATE_TONE[state] ?? "busy";
}

export function Badge({
  children,
  tone: name = "idle",
  title,
}: {
  children: ReactNode;
  tone?: keyof typeof TONE;
  title?: string;
}) {
  return (
    <span
      title={title}
      className={`inline-flex items-center rounded px-1.5 py-0.5 text-xs font-medium whitespace-nowrap ring-1 ring-inset ${TONE[name]}`}
    >
      {children}
    </span>
  );
}

export function StateBadge({ state }: { state: string }) {
  return <Badge tone={tone(state)}>{state.replace(/_/g, " ")}</Badge>;
}

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "default" | "danger";
};

export function Button({ variant = "default", className = "", ...props }: ButtonProps) {
  const styles = {
    primary: "bg-sky-600 text-white hover:bg-sky-500",
    default: "bg-slate-800 text-slate-200 hover:bg-slate-700",
    danger: "bg-rose-900/70 text-rose-100 hover:bg-rose-800",
  }[variant];
  return (
    <button
      {...props}
      className={`rounded px-2.5 py-1.5 text-sm font-medium transition disabled:cursor-not-allowed disabled:opacity-40 pointer-coarse:px-3.5 pointer-coarse:py-3 ${styles} ${className}`}
    />
  );
}

export function Card({
  title,
  actions,
  children,
  className = "",
}: {
  title?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`rounded border border-slate-800 bg-slate-900/40 ${className}`}>
      {(title || actions) && (
        <header className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2 border-b border-slate-800 px-3 py-2 sm:px-4">
          <h2 className="text-sm font-medium tracking-wide text-slate-300 uppercase">{title}</h2>
          {actions}
        </header>
      )}
      <div className="p-3 sm:p-4">{children}</div>
    </section>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="py-2 text-sm text-slate-500">{children}</p>;
}

export function ErrorNote({ error }: { error: unknown }) {
  if (!error) return null;
  const message = error instanceof Error ? error.message : String(error);
  return (
    <p role="alert" className="mt-2 text-sm text-rose-400">
      {message}
    </p>
  );
}

export function Progress({ value }: { value: number }) {
  const pct = Math.max(0, Math.min(100, value));
  return (
    <div
      role="progressbar"
      aria-valuenow={Math.round(pct)}
      aria-valuemin={0}
      aria-valuemax={100}
      className="h-1.5 w-full overflow-hidden rounded bg-slate-800"
    >
      <div className="h-full bg-sky-500 transition-all" style={{ width: `${pct}%` }} />
    </div>
  );
}
