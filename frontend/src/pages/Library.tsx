/** PLAN.md §9.2 — Series / Movies, the Clean toggle, and "Sync now". */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { getTitles, patchTitle, syncLibrary, syncTitle } from "../api/client";
import type { TitleRow } from "../api/types";
import { Page } from "../components/Page";
import { Badge, Button, Card, Empty, ErrorNote, ago } from "../components/ui";

type Kind = "series" | "movie";

function Progress({ row }: { row: TitleRow }) {
  if (row.item_count === 0) return <span className="text-xs text-slate-600">no files</span>;
  return (
    <span className="text-xs text-slate-400">
      {row.clean_count}/{row.item_count} clean
      {row.failed_count > 0 && <span className="ml-2 text-rose-400">{row.failed_count} failed</span>}
    </span>
  );
}

function Toggle({
  row,
  onChange,
  disabled,
}: {
  row: TitleRow;
  onChange: (enabled: boolean) => void;
  disabled: boolean;
}) {
  return (
    <label className="flex cursor-pointer items-center gap-2 text-sm">
      <input
        type="checkbox"
        checked={row.enabled}
        disabled={disabled}
        onChange={(event) => onChange(event.target.checked)}
        aria-label={`Clean ${row.title}`}
        className="h-4 w-4 accent-sky-500"
      />
      <span className={row.enabled ? "text-slate-200" : "text-slate-500"}>Clean</span>
    </label>
  );
}

export function LibraryPage() {
  const client = useQueryClient();
  const [kind, setKind] = useState<Kind>("series");
  const [search, setSearch] = useState("");
  const [onlyEnabled, setOnlyEnabled] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  const query = { kind, q: search, ...(onlyEnabled ? { enabled: true } : {}) };
  const { data, isPending, isError } = useQuery({
    queryKey: ["titles", query],
    queryFn: () => getTitles(query),
  });

  const refresh = () => client.invalidateQueries({ queryKey: ["titles"] });

  const toggle = useMutation({
    mutationFn: async ({ row, enabled }: { row: TitleRow; enabled: boolean }) => {
      const result = await patchTitle(row.id, { enabled });
      // Pull the files now rather than at the next hourly pass, so the Title page's
      // picker has something to show the moment the user opens it.
      const synced = enabled ? await syncTitle(row.id).catch(() => null) : null;
      return { result, synced };
    },
    onSuccess: ({ result, synced }, { row, enabled }) => {
      // §2's backfill is the whole point of the toggle for a movie; for a series it
      // now defers instead, and saying so is the difference between "did that work?"
      // and a user waiting for a queue that is never going to fill.
      const deferred = result.deferred + (synced?.deferred ?? 0);
      setNote(
        !enabled
          ? `${row.title}: cleaning off`
          : result.queued.length
            ? `${row.title}: queued ${result.queued.length} file${result.queued.length === 1 ? "" : "s"}`
            : `${row.title}: cleaning on — new downloads process automatically${
                deferred ? `; ${deferred} existing file${deferred === 1 ? "" : "s"} left for you to pick` : ""
              }`,
      );
      refresh();
    },
  });

  const sync = useMutation({
    mutationFn: syncLibrary,
    onSuccess: (report) => {
      setNote(
        `Sync: ${report.titles_seen ?? 0} titles, ${report.items_seen ?? 0} files, ` +
          `${(report.enqueued as unknown[] | undefined)?.length ?? 0} queued`,
      );
      refresh();
    },
  });

  return (
    <Page title="Library" subtitle="Mark series and movies for cleaning.">
      <div className="space-y-4">
        <div className="flex flex-wrap items-center gap-3">
          <div className="flex rounded border border-slate-800">
            {(["series", "movie"] as Kind[]).map((value) => (
              <button
                key={value}
                type="button"
                onClick={() => setKind(value)}
                className={`px-3 py-1.5 text-sm capitalize ${
                  kind === value ? "bg-slate-800 text-slate-100" : "text-slate-400"
                }`}
              >
                {value === "series" ? "Series" : "Movies"}
              </button>
            ))}
          </div>
          <input
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="Search"
            aria-label="Search titles"
            className="rounded border border-slate-800 bg-slate-900/60 px-3 py-1.5 text-sm outline-none focus:border-slate-600"
          />
          <label className="flex items-center gap-2 text-sm text-slate-400">
            <input
              type="checkbox"
              checked={onlyEnabled}
              onChange={(event) => setOnlyEnabled(event.target.checked)}
              className="h-4 w-4 accent-sky-500"
            />
            Only enabled
          </label>
          <Button
            variant="primary"
            className="ml-auto"
            onClick={() => sync.mutate()}
            disabled={sync.isPending}
          >
            {sync.isPending ? "Syncing…" : "Sync now"}
          </Button>
        </div>

        {note && <p className="text-sm text-sky-300">{note}</p>}
        <ErrorNote error={toggle.error ?? sync.error} />

        <Card title={data ? `${data.titles.length} of ${data.total}` : "Titles"}>
          {isPending && <Empty>Loading…</Empty>}
          {isError && <p className="text-sm text-rose-400">Could not reach the API.</p>}
          {data && data.titles.length === 0 && (
            <Empty>
              Nothing here yet. Configure Sonarr or Radarr in Settings, then press “Sync now”.
            </Empty>
          )}
          {data && data.titles.length > 0 && (
            <table className="w-full text-sm">
              <tbody>
                {data.titles.map((row) => (
                  <tr key={row.id} className="border-b border-slate-800 last:border-0">
                    <td className="py-2 pr-3">
                      <Link
                        to={`/titles/${row.id}`}
                        className="text-slate-100 hover:text-sky-300"
                      >
                        {row.title}
                      </Link>
                      {row.year && <span className="ml-2 text-xs text-slate-500">{row.year}</span>}
                      {row.profile_id && (
                        <Badge tone="idle" title="profile override">
                          profile
                        </Badge>
                      )}
                    </td>
                    <td className="py-2 pr-3">
                      <Progress row={row} />
                    </td>
                    <td className="py-2 pr-3 text-xs text-slate-600">
                      synced {ago(row.last_synced_at)}
                    </td>
                    <td className="py-2 text-right">
                      <div className="flex justify-end">
                        <Toggle
                          row={row}
                          disabled={toggle.isPending}
                          onChange={(enabled) => toggle.mutate({ row, enabled })}
                        />
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>
      </div>
    </Page>
  );
}
