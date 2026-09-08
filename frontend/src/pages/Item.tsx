/**
 * PLAN.md §9.4 — what was removed from one file, and the evidence.
 *
 * This is the screen the milestone's demo turns on: mark a false positive, reprocess,
 * hear the word again. So every detection carries both clips (original and cleaned)
 * and its waveform, and the whitelist control sits on the row it belongs to rather
 * than in a separate form the user has to re-enter the word into.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { addWhitelist, deleteWhitelist, getItem, itemAction } from "../api/client";
import type { ActionName, DetectionRow, ItemDetail } from "../api/types";
import { Page } from "../components/Page";
import {
  Badge,
  Button,
  Card,
  Empty,
  ErrorNote,
  StateBadge,
  ago,
  duration,
  gib,
  maskWord,
  timecode,
  when,
} from "../components/ui";

type Scope = "item" | "title" | "global";

const SCOPE_LABEL: Record<Scope, string> = {
  item: "this file",
  title: "this title",
  global: "everywhere",
};

function Clip({ src, label }: { src: string; label: string }) {
  return (
    <div className="flex min-w-0 items-center gap-2">
      <span className="w-12 shrink-0 text-xs text-slate-500 sm:w-16">{label}</span>
      {/* eslint-disable-next-line jsx-a11y/media-has-caption -- 5 s of audio, no speech track */}
      <audio controls preload="none" src={src} aria-label={label} className="h-8 w-full min-w-0 max-w-64" />
    </div>
  );
}

function DetectionCard({
  detection,
  onWhitelist,
  busy,
}: {
  detection: DetectionRow;
  onWhitelist: (word: string, scope: Scope) => void;
  busy: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [scope, setScope] = useState<Scope>("item");
  const shown = maskWord(detection.word_raw);
  const shownCanonical = maskWord(detection.word_canonical);

  return (
    <div className="border-b border-slate-800 py-2 last:border-0">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1.5 text-sm sm:gap-x-3">
        <span className="w-20 shrink-0 font-mono text-xs text-slate-400">
          {timecode(detection.start_s)}
        </span>
        <span className="text-slate-100">{shown}</span>
        <Badge tone="idle">{detection.category}</Badge>
        <Badge tone={detection.source === "both" ? "ok" : "warn"} title="how it was found">
          {detection.source}
        </Badge>
        {detection.confidence !== null && (
          <span className="text-xs text-slate-500">{Math.round(detection.confidence * 100)}%</span>
        )}
        {detection.suspicious && (
          <Badge tone="warn" title="the mute range failed a sanity guard">
            suspicious
          </Badge>
        )}
        {detection.whitelisted && <Badge tone="idle">whitelisted</Badge>}
        {!detection.muted && !detection.whitelisted && <Badge tone="idle">not muted</Badge>}
        <span className="hidden text-xs text-slate-600 sm:inline">
          muted {(detection.mute_end_s - detection.mute_start_s).toFixed(2)}s
        </span>
        <button
          type="button"
          onClick={() => setOpen((value) => !value)}
          className="ml-auto text-xs text-slate-500 hover:text-slate-300"
          aria-label={`${open ? "Hide" : "Review"} ${shown} at ${timecode(detection.start_s)}`}
        >
          {open ? "close" : "review"}
        </button>
      </div>

      {open && (
        <div className="mt-2 space-y-2 rounded bg-slate-950/60 p-3">
          {detection.snippet ? (
            <>
              <img
                src={`${detection.snippet}/wave.png`}
                alt={`Waveform around ${shown}`}
                className="w-full max-w-xl rounded"
              />
              <Clip src={`${detection.snippet}/orig.m4a`} label="Original" />
              <Clip src={`${detection.snippet}/clean.m4a`} label="Clean" />
            </>
          ) : (
            <Empty>No review clips for this detection.</Empty>
          )}
          <div className="flex flex-wrap items-center gap-2 pt-1">
            <span className="text-xs text-slate-500">False positive? Allow it in</span>
            <select
              value={scope}
              onChange={(event) => setScope(event.target.value as Scope)}
              aria-label={`Whitelist scope for ${shownCanonical}`}
              className="rounded border border-slate-800 bg-slate-900 px-2 py-1 text-xs"
            >
              {(Object.keys(SCOPE_LABEL) as Scope[]).map((value) => (
                <option key={value} value={value}>
                  {SCOPE_LABEL[value]}
                </option>
              ))}
            </select>
            <Button
              disabled={busy}
              onClick={() => onWhitelist(detection.word_canonical, scope)}
              aria-label={`Whitelist ${shownCanonical}`}
            >
              Whitelist &amp; reprocess
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

function Summary({ data }: { data: ItemDetail }) {
  const { item, job } = data;
  const rows: Array<[string, string]> = [
    ["Status", item.status],
    ["Path", item.path],
    ["Size", gib(item.size)],
    ["Duration", duration(item.duration)],
    ["Cleaned", item.cleaned_at ? when(item.cleaned_at) : "—"],
    ["Mode", job?.stt_mode ?? "—"],
    ["Model", job?.model_used ?? "—"],
    ["Subtitles", job?.subtitle_source ?? "—"],
    ["Job", job ? `${job.trigger}${job.dry_run ? " (dry run)" : ""}` : "—"],
  ];
  return (
    <dl className="grid grid-cols-[5.5rem_minmax(0,1fr)] gap-x-3 gap-y-1 text-sm sm:grid-cols-[7rem_minmax(0,1fr)] sm:gap-x-4">
      {rows.map(([label, value]) => (
        <div key={label} className="contents">
          <dt className="text-slate-500">{label}</dt>
          <dd
            className={`text-slate-200 ${label === "Path" ? "break-all sm:truncate" : "truncate"}`}
            title={value}
          >
            {value}
          </dd>
        </div>
      ))}
    </dl>
  );
}

export function ItemPage() {
  const { itemId } = useParams();
  const id = Number(itemId);
  const [params, setParams] = useSearchParams();
  const jobId = params.get("job") ?? undefined;
  const client = useQueryClient();
  const [note, setNote] = useState<string | null>(null);

  const { data, isPending, isError, error } = useQuery({
    queryKey: ["item", id, jobId ?? null],
    queryFn: () => getItem(id, jobId),
    enabled: Number.isFinite(id),
  });

  const refresh = () => {
    client.invalidateQueries({ queryKey: ["item", id] });
    client.invalidateQueries({ queryKey: ["queue"] });
  };

  const act = useMutation({
    mutationFn: (action: ActionName) => itemAction(id, action),
    onSuccess: (result) => {
      setNote(
        result.queued.length
          ? `Queued (${result.action.replace("_", " ")}).`
          : result.restored.length
            ? "Original restored."
            : `Nothing to do: ${Object.keys(result.skipped).join(", ") || "no change"}.`,
      );
      if (result.warnings.length) setNote((current) => `${current} ${result.warnings.join("; ")}`);
      refresh();
    },
  });

  const whitelist = useMutation({
    mutationFn: ({ word, scope }: { word: string; scope: Scope }) =>
      addWhitelist(id, { canonical_word: word, scope, reprocess: true }),
    onSuccess: (result) => {
      setNote(
        `“${maskWord(result.canonical_word)}” allowed in ${SCOPE_LABEL[result.scope as Scope]}` +
          (result.job_id ? " — reprocessing." : "."),
      );
      refresh();
    },
  });

  const forget = useMutation({
    mutationFn: deleteWhitelist,
    onSuccess: refresh,
  });

  if (isError) {
    return (
      <Page title="Item">
        <ErrorNote error={error} />
      </Page>
    );
  }
  if (isPending || !data) return <Page title="Item">Loading…</Page>;

  const busy = act.isPending || whitelist.isPending;
  const muted = data.detections.filter((d) => d.muted && !d.whitelisted).length;

  return (
    <Page
      title={data.item.label}
      subtitle={`${muted} muted word${muted === 1 ? "" : "s"} in this run`}
    >
      <div className="space-y-6">
        <div className="flex flex-wrap items-center gap-2">
          <Button disabled={busy} onClick={() => act.mutate("process")} className="grow sm:grow-0">
            Process now
          </Button>
          <Button variant="primary" disabled={busy} onClick={() => act.mutate("reprocess")} className="grow sm:grow-0">
            Reprocess
          </Button>
          <Button disabled={busy} onClick={() => act.mutate("dry_run")} className="grow sm:grow-0">
            Dry run
          </Button>
          <Button
            variant="danger"
            className="grow sm:grow-0"
            disabled={busy || !data.restorable}
            title={data.restorable ? undefined : "no backup is kept for this file"}
            onClick={() => {
              if (window.confirm("Put the original file back?")) act.mutate("restore");
            }}
          >
            Restore original
          </Button>
          {data.title && (
            <Link
              to={`/titles/${data.title.id}`}
              className="w-full min-w-0 truncate text-sm text-slate-500 hover:text-slate-300 sm:ml-auto sm:w-auto"
            >
              ← {data.title.title}
            </Link>
          )}
        </div>

        {note && <p className="text-sm text-sky-300">{note}</p>}
        <ErrorNote error={act.error ?? whitelist.error ?? forget.error} />

        <div className="grid gap-4 sm:gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
          <Card
            title="Summary"
            actions={data.job ? <StateBadge state={data.job.state} /> : null}
          >
            <Summary data={data} />
            {data.job?.error && <p className="mt-2 text-sm text-rose-400">{data.job.error}</p>}
            {data.jobs.length > 1 && (
              <div className="mt-3 flex flex-wrap items-center gap-2 text-xs">
                <span className="text-slate-500">Runs:</span>
                {data.jobs.map((run) => (
                  <button
                    key={run.id}
                    type="button"
                    onClick={() => setParams(run.id === data.jobs[0].id ? {} : { job: run.id })}
                    className={`rounded px-2 py-0.5 ${
                      run.id === data.job?.id
                        ? "bg-slate-700 text-slate-100"
                        : "bg-slate-800/60 text-slate-400 hover:text-slate-200"
                    }`}
                  >
                    {run.trigger} · {ago(run.finished_at ?? run.created_at)}
                  </button>
                ))}
              </div>
            )}
          </Card>

          <Card title="Counts">
            {data.counts.length === 0 && <Empty>Nothing was detected in this run.</Empty>}
            <ul className="space-y-1 text-sm">
              {data.counts.map((count) => (
                <li key={`${count.word_canonical}:${count.category}`} className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                  <span className="w-10 shrink-0 text-right text-slate-400">{count.total}×</span>
                  <span className="text-slate-200">{maskWord(count.word_canonical)}</span>
                  <Badge tone="idle">{count.category}</Badge>
                  {count.suspicious ? <Badge tone="warn">{count.suspicious} suspicious</Badge> : null}
                </li>
              ))}
            </ul>
          </Card>
        </div>

        <Card title={`Detections (${data.detections.length})`}>
          {data.detections.length === 0 && <Empty>Nothing to review.</Empty>}
          {data.detections.map((detection) => (
            <DetectionCard
              key={detection.id}
              detection={detection}
              busy={busy}
              onWhitelist={(word, scope) => whitelist.mutate({ word, scope })}
            />
          ))}
        </Card>

        {data.whitelist.length > 0 && (
          <Card title="Whitelist in scope">
            <ul className="space-y-1 text-sm">
              {data.whitelist.map((entry) => (
                <li key={entry.id} className="flex flex-wrap items-center gap-x-2 gap-y-1">
                  <span className="text-slate-200">{maskWord(entry.canonical_word)}</span>
                  <Badge tone="idle">{SCOPE_LABEL[entry.scope as Scope] ?? entry.scope}</Badge>
                  <button
                    type="button"
                    onClick={() => forget.mutate(entry.id)}
                    className="text-xs text-slate-600 hover:text-rose-300"
                    aria-label={`Remove ${maskWord(entry.canonical_word)} from the whitelist`}
                  >
                    remove
                  </button>
                </li>
              ))}
            </ul>
          </Card>
        )}
      </div>
    </Page>
  );
}
