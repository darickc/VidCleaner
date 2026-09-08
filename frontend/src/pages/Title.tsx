/** PLAN.md §9.3 — one series or movie: its files, its word rollup, and the buttons. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { getProfiles, getTitle, patchTitle, syncTitle, titleAction } from "../api/client";
import type { ActionName, ActionResult, ItemRow } from "../api/types";
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
} from "../components/ui";

const ACTIONS: Array<{ action: ActionName; label: string; confirm?: string }> = [
  { action: "process", label: "Process now" },
  { action: "reprocess", label: "Reprocess all", confirm: "Re-clean every file in this title?" },
  { action: "dry_run", label: "Dry run" },
  {
    action: "restore",
    label: "Restore originals",
    confirm: "Put the original files back and undo every clean in this title?",
  },
];

/** Episodes grouped by season, in the order the API already sorted them. A movie has
    no seasons, so it lands in one unlabelled group and renders as a flat list. */
function bySeason(items: ItemRow[]): Array<{ season: number | null; items: ItemRow[] }> {
  const groups: Array<{ season: number | null; items: ItemRow[] }> = [];
  for (const item of items) {
    const last = groups[groups.length - 1];
    if (last && last.season === item.season) last.items.push(item);
    else groups.push({ season: item.season, items: [item] });
  }
  return groups;
}

/** React has no declarative `indeterminate`; it is a DOM property only. */
function Check({
  checked,
  indeterminate = false,
  label,
  onChange,
}: {
  checked: boolean;
  indeterminate?: boolean;
  label: string;
  onChange: (checked: boolean) => void;
}) {
  return (
    <input
      type="checkbox"
      checked={checked}
      ref={(node) => {
        if (node) node.indeterminate = indeterminate && !checked;
      }}
      onChange={(event) => onChange(event.target.checked)}
      aria-label={label}
      className="h-4 w-4 accent-sky-500"
    />
  );
}

function summarise(result: ActionResult): string {
  const parts: string[] = [];
  if (result.queued.length) parts.push(`queued ${result.queued.length}`);
  if (result.restored.length) parts.push(`restored ${result.restored.length}`);
  for (const [reason, count] of Object.entries(result.skipped)) {
    parts.push(`${count} skipped (${reason.replace(/_/g, " ")})`);
  }
  if (!parts.length) parts.push("nothing to do");
  return `${result.action.replace("_", " ")}: ${parts.join(", ")}`;
}

export function TitlePage() {
  const { titleId } = useParams();
  const id = Number(titleId);
  const client = useQueryClient();
  const [note, setNote] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<number>>(new Set());

  const { data, isPending, isError } = useQuery({
    queryKey: ["title", id],
    queryFn: () => getTitle(id),
    enabled: Number.isFinite(id),
  });

  const refresh = () => {
    client.invalidateQueries({ queryKey: ["title", id] });
    client.invalidateQueries({ queryKey: ["queue"] });
  };

  const act = useMutation({
    mutationFn: ({ action, itemIds }: { action: ActionName; itemIds?: number[] }) =>
      titleAction(id, action, itemIds),
    onSuccess: (result) => {
      setNote(summarise(result));
      if (result.warnings.length) setNote(`${summarise(result)} — ${result.warnings.join("; ")}`);
      if (result.selected) setSelected(new Set());
      refresh();
    },
  });

  const toggle = useMutation({
    // Enabling a series queues nothing (PLAN.md §2 as amended in M7); it pulls the
    // files instead, so the picker below has something to show rather than staying
    // empty until the hourly sync runs.
    mutationFn: async (enabled: boolean) => {
      const result = await patchTitle(id, { enabled });
      if (enabled) await syncTitle(id).catch(() => undefined);
      return result;
    },
    onSuccess: (result, enabled) => {
      if (enabled)
        setNote(
          result.queued.length
            ? `queued ${result.queued.length} file(s)`
            : "cleaning on — new downloads process automatically; tick the files below to clean what is already here",
        );
      refresh();
    },
  });

  // §2's per-title profile override. The backend has honoured `titles.profile_id` since
  // M3 -- `worker/spec.plan_job` passes it into `matcher_for` and the hourly sync
  // compares the resulting hash -- but nothing listed the profiles, so there was no way
  // to set it.
  const profiles = useQuery({ queryKey: ["profiles"], queryFn: getProfiles });
  const setProfile = useMutation({
    mutationFn: (value: string) =>
      patchTitle(id, value === "" ? { clear_profile: true } : { profile_id: Number(value) }),
    onSuccess: refresh,
  });

  const items = data?.items ?? [];
  const groups = useMemo(() => bySeason(items), [items]);
  // A refetch after an action must not leave ids in the selection that no longer
  // exist -- the button would then post files the user cannot see.
  const known = items.map((row) => row.id).join(",");
  useEffect(() => {
    const live = new Set(known ? known.split(",").map(Number) : []);
    setSelected((current) => {
      const kept = new Set([...current].filter((value) => live.has(value)));
      return kept.size === current.size ? current : kept;
    });
  }, [known]);

  const toggleMany = (ids: number[], checked: boolean) =>
    setSelected((current) => {
      const next = new Set(current);
      for (const value of ids) {
        if (checked) next.add(value);
        else next.delete(value);
      }
      return next;
    });

  if (isError) return <Page title="Title">Could not load this title.</Page>;
  if (isPending || !data) return <Page title="Title">Loading…</Page>;

  const { title, counts } = data;
  const allIds = items.map((row) => row.id);
  const everySelected = allIds.length > 0 && allIds.every((value) => selected.has(value));

  return (
    <Page
      title={title.title}
      subtitle={`${title.kind === "series" ? "Series" : "Movie"}${title.year ? ` · ${title.year}` : ""} · ${title.clean_count}/${title.item_count} clean`}
    >
      <div className="space-y-6">
        <div className="flex flex-wrap items-center gap-2">
          <label className="flex w-full cursor-pointer items-center gap-2 text-sm sm:mr-3 sm:w-auto">
            <input
              type="checkbox"
              checked={title.enabled}
              disabled={toggle.isPending}
              onChange={(event) => toggle.mutate(event.target.checked)}
              aria-label={`Clean ${title.title}`}
              className="h-4 w-4 accent-sky-500"
            />
            Clean this title
          </label>
          {/* Only once there is a real choice: a dropdown whose sole option is "Default"
              is a control that cannot do anything. The Words page points here instead,
              once a profile exists. */}
          {(profiles.data?.length ?? 0) > 1 && (
            <label className="flex w-full items-center gap-2 text-sm text-slate-400 sm:mr-3 sm:w-auto">
              Profile
              <select
                aria-label={`Profile for ${title.title}`}
                className="w-full min-w-0 rounded border border-slate-800 bg-slate-900/60 px-2 py-1 text-sm outline-none focus:border-slate-600 sm:w-auto"
                value={title.profile_id ?? ""}
                disabled={setProfile.isPending}
                onChange={(event) => setProfile.mutate(event.target.value)}
              >
                <option value="">
                  Default
                  {profiles.data?.find((p) => p.is_default)
                    ? ` (${profiles.data.find((p) => p.is_default)?.name})`
                    : ""}
                </option>
                {profiles.data
                  ?.filter((p) => !p.is_default)
                  .map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name}
                    </option>
                  ))}
              </select>
            </label>
          )}
          {ACTIONS.map(({ action, label, confirm }) => (
            <Button
              key={action}
              variant={action === "restore" ? "danger" : "default"}
              className="grow sm:grow-0"
              disabled={act.isPending}
              onClick={() => {
                if (confirm && !window.confirm(confirm)) return;
                act.mutate({ action });
              }}
            >
              {label}
            </Button>
          ))}
          <Link
            to="/library"
            className="w-full text-sm text-slate-500 hover:text-slate-300 sm:ml-auto sm:w-auto"
          >
            ← Library
          </Link>
        </div>

        {note && <p className="text-sm text-sky-300">{note}</p>}
        <ErrorNote error={act.error ?? toggle.error} />

        <div className="grid gap-4 sm:gap-6 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
          <Card
            title="Files"
            actions={
              items.length > 0 && (
                <div className="flex flex-wrap items-center gap-2 sm:gap-3">
                  <label className="flex cursor-pointer items-center gap-2 text-xs text-slate-400">
                    <Check
                      checked={everySelected}
                      indeterminate={selected.size > 0}
                      label="Select all files"
                      onChange={(checked) => toggleMany(allIds, checked)}
                    />
                    All
                  </label>
                  <Button
                    variant="primary"
                    disabled={selected.size === 0 || act.isPending}
                    onClick={() =>
                      act.mutate({ action: "process", itemIds: [...selected] })
                    }
                  >
                    Process selected ({selected.size})
                  </Button>
                </div>
              )
            }
          >
            {items.length === 0 && <Empty>No files are tracked for this title yet.</Empty>}
            {items.length > 0 && (
              <div className="-mx-1 overflow-x-auto px-1">
                <table className="w-full text-sm">
                  <tbody>
                    {groups.map((group) => {
                      const ids = group.items.map((row) => row.id);
                      const all = ids.every((value) => selected.has(value));
                      const some = ids.some((value) => selected.has(value));
                      return (
                        <Fragment key={group.season ?? "movie"}>
                          {group.season !== null && (
                            <tr className="border-b border-slate-800">
                              <td className="py-2 pr-2">
                                <Check
                                  checked={all}
                                  indeterminate={some}
                                  label={`Select season ${group.season}`}
                                  onChange={(checked) => toggleMany(ids, checked)}
                                />
                              </td>
                              <td colSpan={3} className="py-2 text-xs uppercase tracking-wide text-slate-500">
                                Season {group.season}
                              </td>
                              <td colSpan={2} className="hidden sm:table-cell" />
                            </tr>
                          )}
                          {group.items.map((row) => (
                            <tr key={row.id} className="border-b border-slate-800 last:border-0">
                              <td className="py-2 pr-2">
                                <Check
                                  checked={selected.has(row.id)}
                                  label={`Select ${row.label}`}
                                  onChange={(checked) => toggleMany([row.id], checked)}
                                />
                              </td>
                              <td className="min-w-0 py-2 pr-3">
                                <Link
                                  to={`/items/${row.id}`}
                                  className="text-slate-100 hover:text-sky-300"
                                >
                                  {row.season !== null && row.episode !== null
                                    ? `S${String(row.season).padStart(2, "0")}E${String(row.episode).padStart(2, "0")}`
                                    : row.title}
                                </Link>
                                {row.episode_title && (
                                  <span className="ml-2 text-slate-400">{row.episode_title}</span>
                                )}
                              </td>
                              <td className="py-2 pr-3">
                                {row.skip_backfill && row.status !== "clean" ? (
                                  <Badge tone="idle">not queued</Badge>
                                ) : (
                                  <StateBadge state={row.status} />
                                )}
                              </td>
                              <td className="py-2 pr-3 text-xs text-slate-500">
                                {row.detection_count > 0
                                  ? `${row.detection_count} muted`
                                  : row.status === "clean"
                                    ? "nothing found"
                                    : "—"}
                              </td>
                              <td className="hidden py-2 pr-3 text-xs text-slate-600 sm:table-cell">
                                {duration(row.duration)} · {gib(row.size)}
                              </td>
                              <td className="hidden py-2 text-right text-xs text-slate-600 sm:table-cell">
                                {row.cleaned_at ? `cleaned ${ago(row.cleaned_at)}` : ""}
                              </td>
                            </tr>
                          ))}
                        </Fragment>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </Card>

          <Card title="Words removed">
            {counts.length === 0 && <Empty>Nothing has been detected yet.</Empty>}
            <ul className="space-y-1 text-sm">
              {counts.map((count) => (
                <li key={`${count.word_canonical}:${count.category}`} className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                  <span className="w-10 shrink-0 text-right text-slate-400">{count.total}×</span>
                  <span className="text-slate-200">{maskWord(count.word_canonical)}</span>
                  <Badge tone="idle">{count.category}</Badge>
                </li>
              ))}
            </ul>
          </Card>
        </div>
      </div>
    </Page>
  );
}
