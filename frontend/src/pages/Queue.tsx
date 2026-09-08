/** PLAN.md §9.1 — the running job, what is waiting, what just finished, and health. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import {
  cancelJob,
  getHealth,
  getJob,
  getQueue,
  retryJob,
  setJobPriority,
} from "../api/client";
import type { DiskInfo, Health, JobSummary } from "../api/types";
import { Page } from "../components/Page";
import {
  Badge,
  Button,
  Card,
  Empty,
  ErrorNote,
  Progress,
  StateBadge,
  ago,
  gib,
  tone,
  when,
} from "../components/ui";

/** Fast enough to feel live, slow enough that a busy worker is not the bottleneck. */
const POLL_MS = 3000;

function ItemLink({ job }: { job: JobSummary }) {
  if (!job.item) return <span className="text-slate-400">item {job.media_item_id}</span>;
  return (
    <Link to={`/items/${job.item.id}`} className="text-slate-100 hover:text-sky-300">
      {job.item.label}
    </Link>
  );
}

function RunningJob({ job }: { job: JobSummary }) {
  const [open, setOpen] = useState(false);
  const { data: detail } = useQuery({
    queryKey: ["job", job.id],
    queryFn: () => getJob(job.id),
    refetchInterval: POLL_MS,
    enabled: open,
  });

  return (
    <div className="space-y-2 border-b border-slate-800 py-3 last:border-0 last:pb-0 first:pt-0">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <ItemLink job={job} />
        <span className="text-xs text-slate-500">
          {job.trigger}
          {job.dry_run ? " · dry run" : ""} · started {ago(job.started_at)}
        </span>
      </div>
      <div className="flex items-center gap-2 sm:gap-3">
        <Badge tone="busy">{job.stage ?? job.state}</Badge>
        <Progress value={job.progress_pct} />
        <span className="w-12 shrink-0 text-right text-xs text-slate-400">
          {Math.round(job.progress_pct)}%
        </span>
      </div>
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        className="text-xs text-slate-500 hover:text-slate-300"
      >
        {open ? "hide log" : "show log"}
      </button>
      {open && detail && (
        <pre className="max-h-48 overflow-auto rounded bg-slate-950/70 p-2 text-xs text-slate-400">
          {detail.logs.map((line) => `${when(line.ts)}  ${line.msg}`).join("\n") || "no log yet"}
        </pre>
      )}
    </div>
  );
}

function QueuedRow({ job, onBump, onCancel, busy }: {
  job: JobSummary;
  onBump: (job: JobSummary) => void;
  onCancel: (job: JobSummary) => void;
  busy: boolean;
}) {
  return (
    <tr className="border-b border-slate-800 last:border-0">
      <td className="py-1.5 pr-3">
        <ItemLink job={job} />
      </td>
      <td className="hidden py-1.5 pr-3 text-xs text-slate-500 sm:table-cell">{job.trigger}</td>
      <td className="hidden py-1.5 pr-3 text-xs text-slate-500 sm:table-cell" title="lower runs sooner">
        {job.priority}
      </td>
      <td className="py-1.5 pr-3 text-xs text-slate-500">{ago(job.created_at)}</td>
      <td className="py-1.5 text-right whitespace-nowrap">
        <Button
          onClick={() => onBump(job)}
          disabled={busy}
          className="mr-1"
          aria-label={`Run ${job.item?.label ?? job.id} next`}
        >
          Run next
        </Button>
        <Button
          variant="danger"
          onClick={() => onCancel(job)}
          disabled={busy}
          aria-label={`Cancel ${job.item?.label ?? job.id}`}
        >
          Cancel
        </Button>
      </td>
    </tr>
  );
}

function DiskRow({ name, disk }: { name: string; disk: DiskInfo }) {
  const free = disk.free_bytes ?? 0;
  const total = disk.total_bytes ?? 0;
  const low = total > 0 && free / total < 0.05;
  return (
    <div className="flex flex-wrap items-baseline justify-between gap-x-2 gap-y-0.5 border-b border-slate-800 py-1.5 last:border-0">
      <span className="text-slate-400">
        {name} <span className="break-all text-xs text-slate-600">{disk.path}</span>
      </span>
      {disk.exists ? (
        <span className={low ? "text-amber-300" : "text-slate-200"}>{gib(free)} free</span>
      ) : (
        <span className="text-rose-400">missing</span>
      )}
    </div>
  );
}

function HealthCard() {
  const { data, isPending, isError } = useQuery({
    queryKey: ["health"],
    queryFn: getHealth,
    refetchInterval: 15_000,
  });

  return (
    <Card title="System health">
      {isPending && <Empty>Checking…</Empty>}
      {isError && <p className="text-sm text-rose-400">Could not reach the API.</p>}
      {data && (
        <div className="space-y-3 text-sm">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <Badge tone={tone(data.status)}>{data.status}</Badge>
            <span className="text-xs text-slate-500">
              v{data.version} · role {data.role}
              {data.database.revision ? ` · migration ${data.database.revision}` : ""}
            </span>
          </div>
          {!data.database.ok && (
            <p className="text-rose-400">{data.database.error ?? "database error"}</p>
          )}
          {!data.ffmpeg.present && (
            <p className="text-amber-300">ffmpeg is not installed — no job can render.</p>
          )}
          <div>
            {(Object.keys(data.disk) as Array<keyof Health["disk"]>).map((key) => (
              <DiskRow key={key} name={key} disk={data.disk[key]} />
            ))}
          </div>
        </div>
      )}
    </Card>
  );
}

export function QueuePage() {
  const client = useQueryClient();
  const { data, isPending, isError } = useQuery({
    queryKey: ["queue"],
    queryFn: getQueue,
    refetchInterval: POLL_MS,
  });

  const refresh = () => client.invalidateQueries({ queryKey: ["queue"] });
  const cancel = useMutation({ mutationFn: cancelJob, onSuccess: refresh });
  const retry = useMutation({ mutationFn: retryJob, onSuccess: refresh });
  const bump = useMutation({
    // One below the most urgent trigger (`manual` = 50), so "run next" beats
    // everything already waiting without inventing a new priority tier.
    mutationFn: (job: JobSummary) => setJobPriority(job.id, 10),
    onSuccess: refresh,
  });
  const busy = cancel.isPending || retry.isPending || bump.isPending;

  return (
    <Page title="Queue" subtitle="Running and queued jobs, plus system health.">
      <div className="grid gap-4 sm:gap-6 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        <div className="space-y-6">
          <Card title="Running">
            {isPending && <Empty>Loading…</Empty>}
            {isError && <p className="text-sm text-rose-400">Could not reach the API.</p>}
            {data && data.running.length === 0 && <Empty>Nothing is running.</Empty>}
            {data?.running.map((job) => (
              <RunningJob key={job.id} job={job} />
            ))}
          </Card>

          <Card
            title={`Queued${data ? ` (${data.queued_total})` : ""}`}
            actions={
              data && data.queued_total > data.queued.length ? (
                <span className="text-xs text-slate-500">
                  showing {data.queued.length} of {data.queued_total}
                </span>
              ) : null
            }
          >
            {data && data.queued.length === 0 && <Empty>The queue is empty.</Empty>}
            {data && data.queued.length > 0 && (
              <div className="-mx-1 overflow-x-auto px-1">
                <table className="w-full text-sm">
                  <tbody>
                    {data.queued.map((job) => (
                      <QueuedRow
                        key={job.id}
                        job={job}
                        busy={busy}
                        onBump={(j) => bump.mutate(j)}
                        onCancel={(j) => cancel.mutate(j.id)}
                      />
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <ErrorNote error={cancel.error ?? bump.error} />
          </Card>

          <Card title="Recent">
            {data && data.recent.length === 0 && <Empty>Nothing has finished yet.</Empty>}
            {data && data.recent.length > 0 && (
              <div className="-mx-1 overflow-x-auto px-1">
                <table className="w-full text-sm">
                  <tbody>
                    {data.recent.map((job) => (
                      <tr key={job.id} className="border-b border-slate-800 last:border-0">
                        <td className="py-1.5 pr-3">
                          <ItemLink job={job} />
                          {job.error && (
                            <div className="line-clamp-2 text-xs text-rose-400" title={job.error}>
                              {job.error}
                            </div>
                          )}
                        </td>
                        <td className="py-1.5 pr-3">
                          <StateBadge state={job.state} />
                        </td>
                        <td className="hidden py-1.5 pr-3 text-xs text-slate-500 sm:table-cell">
                          {ago(job.finished_at)}
                        </td>
                        <td className="py-1.5 text-right">
                          {(job.state === "failed" || job.state === "cancelled") && (
                            <Button
                              onClick={() => retry.mutate(job.id)}
                              disabled={busy}
                              aria-label={`Retry ${job.item?.label ?? job.id}`}
                            >
                              Retry
                            </Button>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <ErrorNote error={retry.error} />
          </Card>
        </div>

        <HealthCard />
      </div>
    </Page>
  );
}
