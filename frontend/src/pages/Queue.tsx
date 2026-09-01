import { useQuery } from "@tanstack/react-query";
import { getHealth, type DiskInfo, type Health } from "../api/client";
import { Page, Placeholder } from "../components/Page";

function gib(bytes: number | null): string {
  if (bytes === null) return "—";
  return `${(bytes / 1024 ** 3).toFixed(1)} GiB`;
}

const STATUS_STYLE: Record<Health["status"], string> = {
  ok: "bg-emerald-500/15 text-emerald-300",
  degraded: "bg-amber-500/15 text-amber-300",
  error: "bg-rose-500/15 text-rose-300",
};

function Row({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="flex items-baseline justify-between border-b border-slate-800 py-2 last:border-0">
      <span className="text-slate-400">{label}</span>
      <span className="text-right">
        <span className="text-slate-200">{value}</span>
        {hint && <span className="ml-2 text-xs text-slate-500">{hint}</span>}
      </span>
    </div>
  );
}

function DiskRow({ name, disk }: { name: string; disk: DiskInfo }) {
  return (
    <Row
      label={name}
      value={disk.exists ? `${gib(disk.free_bytes)} free` : "missing"}
      hint={disk.path}
    />
  );
}

export function QueuePage() {
  const { data, isPending, isError } = useQuery({
    queryKey: ["health"],
    queryFn: getHealth,
    refetchInterval: 15_000,
  });

  return (
    <Page title="Queue" subtitle="Running and queued jobs, plus system health.">
      <div className="max-w-2xl space-y-6">
        <div className="rounded border border-slate-800 bg-slate-900/40 p-4">
          <h2 className="mb-3 text-sm font-medium tracking-wide text-slate-300 uppercase">
            System health
          </h2>
          {isPending && <p className="text-sm text-slate-500">Checking…</p>}
          {isError && <p className="text-sm text-rose-400">Could not reach the API.</p>}
          {data && (
            <div className="text-sm">
              <div className="mb-3">
                <span className={`rounded px-2 py-1 text-xs font-medium ${STATUS_STYLE[data.status]}`}>
                  {data.status}
                </span>
              </div>
              <Row label="Version" value={data.version} hint={`role: ${data.role}`} />
              <Row
                label="Database"
                value={data.database.ok ? "connected" : (data.database.error ?? "error")}
                hint={data.database.revision ? `migration ${data.database.revision}` : undefined}
              />
              <Row
                label="ffmpeg"
                value={data.ffmpeg.present ? "available" : "not installed"}
                hint={data.ffmpeg.version ?? undefined}
              />
              {(Object.keys(data.disk) as Array<keyof Health["disk"]>).map((key) => (
                <DiskRow key={key} name={key} disk={data.disk[key]} />
              ))}
            </div>
          )}
        </div>

        <Placeholder milestone="M3/M4">
          The job queue — running stage and progress, queued items with reorder and cancel, and
          recent done/failed with retry — appears once the worker claims jobs.
        </Placeholder>
      </div>
    </Page>
  );
}
