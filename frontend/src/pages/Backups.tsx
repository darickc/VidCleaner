/**
 * PLAN.md §9.7 — every original being held, orphans included.
 *
 * §9.6's Settings panel answers "how much is this costing me". It cannot answer
 * "which ones, and where did they come from", and that is the question an orphan
 * actually raises: a count is not something you can check before you delete it, and
 * "the share is filling up" really means "what is the biggest thing in here?".
 *
 * Every deletion on this page is irreversible -- it is the original that "restore"
 * depends on -- so each one sits behind a confirm that names what goes and how big
 * it is, rather than a bare "purge".
 */

import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import {
  getBackups,
  itemAction,
  purgeBackup,
  purgeBackups,
  reconcileBackups,
} from "../api/client";
import type { BackupRow } from "../api/types";
import { Page } from "../components/Page";
import {
  Badge,
  Button,
  Card,
  Empty,
  ErrorNote,
  StateBadge,
  ago,
  gib,
  when,
} from "../components/ui";

type Filter = "all" | "kept" | "orphaned" | "restored" | "expired";
type Sort = "recent" | "largest";

const FILTERS: { value: Filter; label: string }[] = [
  { value: "all", label: "All" },
  { value: "kept", label: "Kept" },
  { value: "orphaned", label: "Orphaned" },
  { value: "restored", label: "Restored" },
  { value: "expired", label: "Expired" },
];

function queryFor(filter: Filter, sort: Sort) {
  if (filter === "expired") return { sort, expired_only: true };
  if (filter === "all") return { sort };
  return { sort, state: filter };
}

/** Orphans are the ones worth explaining: nothing will ever restore them. */
function OrphanNote({ count, bytes }: { count: number; bytes: number }) {
  if (count === 0) return null;
  return (
    <p className="text-sm text-slate-400">
      {count} original{count === 1 ? "" : "s"} ({gib(bytes)}) belong to files that are
      no longer in the library — an upgrade or a delete replaced them, so nothing can
      restore them and their retention clock is ignored.
    </p>
  );
}

function Row({
  row,
  busy,
  onAskPurge,
  onRestore,
}: {
  row: BackupRow;
  busy: boolean;
  onAskPurge: () => void;
  onRestore: () => void;
}) {
  return (
    <tr className="border-b border-slate-800 align-top last:border-0">
      <td className="py-2 pr-3">
        <span className="break-all text-slate-100" title={row.backup_path}>
          {row.rel_path}
        </span>
        <div className="text-xs text-slate-500">
          {row.identified ? (
            <Link to={`/items/${row.media_item_id}`} className="hover:text-sky-300">
              {row.label}
            </Link>
          ) : (
            <span title="adopted by a reconcile; no library file claims it">
              no matching item
            </span>
          )}
        </div>
      </td>
      <td className="py-2 pr-3">
        <StateBadge state={row.state} />
        {!row.exists && (
          <Badge tone="warn" title="the row is still here but the file is not">
            file gone
          </Badge>
        )}
      </td>
      <td className="py-2 pr-3 text-right whitespace-nowrap text-slate-300">
        {gib(row.size ?? 0)}
      </td>
      <td className="hidden py-2 pr-3 whitespace-nowrap text-xs text-slate-500 sm:table-cell">
        {ago(row.created_at)}
      </td>
      <td className="hidden py-2 pr-3 whitespace-nowrap text-xs text-slate-500 sm:table-cell">
        {row.purge_after ? (
          <span className={row.expired ? "text-amber-300" : undefined}>
            {row.expired ? "expired " : "purges "}
            {when(row.purge_after)}
          </span>
        ) : (
          "kept forever"
        )}
      </td>
      <td className="py-2 text-right whitespace-nowrap">
        <span className="flex items-center justify-end gap-2">
          {row.state === "kept" && row.identified && (
            <Button
              onClick={onRestore}
              disabled={busy || !row.exists}
              title={
                row.exists
                  ? "Put this original back into the library"
                  : "the backup file is missing"
              }
            >
              Restore
            </Button>
          )}
          {(row.state === "kept" || row.state === "orphaned") && (
            <Button onClick={onAskPurge} disabled={busy}>
              Delete
            </Button>
          )}
        </span>
      </td>
    </tr>
  );
}

export function BackupsPage() {
  const client = useQueryClient();
  const [filter, setFilter] = useState<Filter>("all");
  const [sort, setSort] = useState<Sort>("recent");
  const [bulk, setBulk] = useState<"expired" | "orphaned" | null>(null);
  // The per-row confirm lives on the page rather than in the row: inside the table
  // it sits in a horizontally scrolling container, so on a phone you press Delete
  // and the sentence you are agreeing to is off the right-hand edge.
  const [doomed, setDoomed] = useState<BackupRow | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const query = queryFor(filter, sort);
  const { data, isPending, isError } = useQuery({
    queryKey: ["backups", query],
    queryFn: () => getBackups(query),
    // The summary is the same whichever filter is showing, so dropping it to a
    // spinner every time a chip is clicked is pure flicker.
    placeholderData: keepPreviousData,
  });

  const refresh = () => {
    client.invalidateQueries({ queryKey: ["backups"] });
    client.invalidateQueries({ queryKey: ["health"] });
  };

  const purgeAll = useMutation({
    mutationFn: (scope: "expired" | "orphaned") => purgeBackups(scope),
    onSuccess: (result) => {
      setBulk(null);
      setNote(`Deleted ${result.purged} original(s), reclaimed ${gib(result.freed_bytes)}.`);
      refresh();
    },
  });

  const purgeOne = useMutation({
    mutationFn: (backupId: number) => purgeBackup(backupId),
    onSuccess: (result) => {
      setDoomed(null);
      setNote(`Deleted 1 original, reclaimed ${gib(result.freed_bytes)}.`);
      refresh();
    },
  });

  const restore = useMutation({
    mutationFn: (itemId: number) => itemAction(itemId, "restore"),
    onSuccess: (result) => {
      setNote(
        result.restored.length > 0
          ? "Restored. The cleaned copy was moved aside, not deleted."
          : "Nothing was restored — see the item page.",
      );
      client.invalidateQueries();
    },
  });

  const reconcile = useMutation({
    mutationFn: () => reconcileBackups(),
    onSuccess: (result) => {
      setNote(
        result.skipped
          ? result.note
          : `Adopted ${result.adopted} untracked file(s), marked ${result.purged} gone.`,
      );
      refresh();
    },
  });

  const summary = data?.summary;
  const busy = purgeAll.isPending || purgeOne.isPending || restore.isPending;

  const askBulk = (scope: "expired" | "orphaned", count: number, bytes: number) =>
    bulk === scope ? (
      <span className="flex items-center gap-2">
        <span className="text-sm text-amber-300">
          Delete {count} original{count === 1 ? "" : "s"} ({gib(bytes)})? This cannot be
          undone.
        </span>
        <Button variant="danger" onClick={() => purgeAll.mutate(scope)} disabled={busy}>
          Yes, purge
        </Button>
        <Button onClick={() => setBulk(null)}>Cancel</Button>
      </span>
    ) : (
      <Button onClick={() => setBulk(scope)} disabled={count === 0}>
        {scope === "expired" ? "Purge expired" : "Purge orphaned"} ({count})
      </Button>
    );

  return (
    <Page
      title="Backups"
      subtitle="The untouched originals. Deleting one makes that file's clean permanent."
    >
      <div className="space-y-4">
        {summary && (
          <Card title="What is being held">
            <div className="space-y-3 text-sm">
              <div className="flex flex-wrap items-center gap-3">
                <Badge>{summary.total} originals</Badge>
                <Badge>{gib(summary.total_bytes)}</Badge>
                {summary.keeps_forever ? (
                  <Badge tone="warn" title="backup_retention_days = 0">
                    kept forever
                  </Badge>
                ) : (
                  <Badge>purged after {summary.retention_days} days</Badge>
                )}
              </div>
              <p className="text-slate-400">
                They live in <code>{summary.backups_dir}</code>, which mirrors your
                library’s folder layout. Restoring a file needs its original, so
                purging one makes that episode’s clean permanent.
              </p>
              {summary.backups_dir_is_hidden && (
                <p className="text-amber-300">
                  That directory is hidden, so it will not show up while you are
                  tidying the share. Clear <code>VIDCLEANER_BACKUPS_DIR</code> (or set
                  it to <code>VidCleaner-Backups</code> beside your media folders) and
                  VidCleaner will move the originals there itself.
                </p>
              )}
              <OrphanNote count={summary.orphaned} bytes={summary.orphaned_bytes} />
              <div className="flex flex-wrap items-center gap-2">
                {askBulk("expired", summary.expired, summary.expired_bytes)}
                {askBulk("orphaned", summary.orphaned, summary.orphaned_bytes)}
                <Button
                  className="ml-auto"
                  onClick={() => reconcile.mutate()}
                  disabled={reconcile.isPending}
                  title="Re-read the directory, so this list matches what is on disk"
                >
                  {reconcile.isPending ? "Rescanning…" : "Rescan directory"}
                </Button>
              </div>
              {note && <p className="text-emerald-300">{note}</p>}
              {(purgeAll.data?.warnings ?? []).map((warning) => (
                <p key={warning} className="text-amber-300">
                  {warning}
                </p>
              ))}
              <ErrorNote
                error={
                  purgeAll.error ?? purgeOne.error ?? restore.error ?? reconcile.error
                }
              />
            </div>
          </Card>
        )}

        <div className="flex flex-wrap items-center gap-3">
          <div className="flex rounded border border-slate-800">
            {FILTERS.map((entry) => (
              <button
                key={entry.value}
                type="button"
                onClick={() => setFilter(entry.value)}
                className={`px-3 py-1.5 text-sm ${
                  filter === entry.value ? "bg-slate-800 text-slate-100" : "text-slate-400"
                }`}
              >
                {entry.label}
              </button>
            ))}
          </div>
          <label className="flex items-center gap-2 text-sm text-slate-400">
            Sort
            <select
              value={sort}
              onChange={(event) => setSort(event.target.value as Sort)}
              aria-label="Sort backups"
              className="rounded border border-slate-800 bg-slate-900/60 px-2 py-1.5 text-sm outline-none focus:border-slate-600"
            >
              <option value="recent">Newest first</option>
              <option value="largest">Largest first</option>
            </select>
          </label>
        </div>

        {doomed && (
          <Card>
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <span className="text-amber-300">
                Delete <code className="break-all">{doomed.rel_path}</code> (
                {gib(doomed.size ?? 0)})? This cannot be undone.
              </span>
              <span className="ml-auto flex items-center gap-2">
                <Button
                  variant="danger"
                  onClick={() => purgeOne.mutate(doomed.id)}
                  disabled={busy}
                >
                  Yes, delete
                </Button>
                <Button onClick={() => setDoomed(null)}>Cancel</Button>
              </span>
            </div>
          </Card>
        )}

        <Card title={data ? `${data.backups.length} shown` : "Originals"}>
          {isPending && <Empty>Loading…</Empty>}
          {isError && <p className="text-sm text-rose-400">Could not reach the API.</p>}
          {data && data.backups.length === 0 && (
            <Empty>
              {filter === "all"
                ? "Nothing is being held. Originals appear here once a file is cleaned."
                : `No ${filter} originals.`}
            </Empty>
          )}
          {data && data.backups.length > 0 && (
            <div className="-mx-1 overflow-x-auto px-1">
              <table className="w-full text-sm">
                <thead className="text-xs tracking-wide text-slate-500 uppercase">
                  <tr>
                    <th className="py-1 pr-3 text-left">Original</th>
                    <th className="py-1 pr-3 text-left">State</th>
                    <th className="py-1 pr-3 text-right">Size</th>
                    <th className="hidden py-1 pr-3 text-left sm:table-cell">Kept</th>
                    <th className="hidden py-1 pr-3 text-left sm:table-cell">Retention</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {data.backups.map((row) => (
                    <Row
                      key={row.id}
                      row={row}
                      busy={busy}
                      onAskPurge={() => setDoomed(row)}
                      onRestore={() => restore.mutate(row.media_item_id)}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </div>
    </Page>
  );
}
