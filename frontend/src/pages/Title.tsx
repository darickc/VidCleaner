/** PLAN.md §9.3 — one series or movie: its files, its word rollup, and the buttons. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { getProfiles, getTitle, patchTitle, titleAction } from "../api/client";
import type { ActionName, ActionResult } from "../api/types";
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
    mutationFn: (action: ActionName) => titleAction(id, action),
    onSuccess: (result) => {
      setNote(summarise(result));
      if (result.warnings.length) setNote(`${summarise(result)} — ${result.warnings.join("; ")}`);
      refresh();
    },
  });

  const toggle = useMutation({
    mutationFn: (enabled: boolean) => patchTitle(id, { enabled }),
    onSuccess: refresh,
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

  if (isError) return <Page title="Title">Could not load this title.</Page>;
  if (isPending || !data) return <Page title="Title">Loading…</Page>;

  const { title, items, counts } = data;

  return (
    <Page
      title={title.title}
      subtitle={`${title.kind === "series" ? "Series" : "Movie"}${title.year ? ` · ${title.year}` : ""} · ${title.clean_count}/${title.item_count} clean`}
    >
      <div className="space-y-6">
        <div className="flex flex-wrap items-center gap-2">
          <label className="mr-3 flex cursor-pointer items-center gap-2 text-sm">
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
            <label className="mr-3 flex items-center gap-2 text-sm text-slate-400">
              Profile
              <select
                aria-label={`Profile for ${title.title}`}
                className="rounded border border-slate-800 bg-slate-900/60 px-2 py-1 text-sm outline-none focus:border-slate-600"
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
              disabled={act.isPending}
              onClick={() => {
                if (confirm && !window.confirm(confirm)) return;
                act.mutate(action);
              }}
            >
              {label}
            </Button>
          ))}
          <Link
            to="/library"
            className="ml-auto text-sm text-slate-500 hover:text-slate-300"
          >
            ← Library
          </Link>
        </div>

        {note && <p className="text-sm text-sky-300">{note}</p>}
        <ErrorNote error={act.error ?? toggle.error} />

        <div className="grid gap-6 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
          <Card title="Files">
            {items.length === 0 && <Empty>No files are tracked for this title yet.</Empty>}
            {items.length > 0 && (
              <table className="w-full text-sm">
                <tbody>
                  {items.map((row) => (
                    <tr key={row.id} className="border-b border-slate-800 last:border-0">
                      <td className="py-2 pr-3">
                        <Link to={`/items/${row.id}`} className="text-slate-100 hover:text-sky-300">
                          {row.season !== null && row.episode !== null
                            ? `S${String(row.season).padStart(2, "0")}E${String(row.episode).padStart(2, "0")}`
                            : row.title}
                        </Link>
                        {row.episode_title && (
                          <span className="ml-2 text-slate-400">{row.episode_title}</span>
                        )}
                      </td>
                      <td className="py-2 pr-3">
                        <StateBadge state={row.status} />
                      </td>
                      <td className="py-2 pr-3 text-xs text-slate-500">
                        {row.detection_count > 0
                          ? `${row.detection_count} muted`
                          : row.status === "clean"
                            ? "nothing found"
                            : "—"}
                      </td>
                      <td className="py-2 pr-3 text-xs text-slate-600">
                        {duration(row.duration)} · {gib(row.size)}
                      </td>
                      <td className="py-2 text-right text-xs text-slate-600">
                        {row.cleaned_at ? `cleaned ${ago(row.cleaned_at)}` : ""}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Card>

          <Card title="Words removed">
            {counts.length === 0 && <Empty>Nothing has been detected yet.</Empty>}
            <ul className="space-y-1 text-sm">
              {counts.map((count) => (
                <li key={`${count.word_canonical}:${count.category}`} className="flex gap-2">
                  <span className="w-10 shrink-0 text-right text-slate-400">{count.total}×</span>
                  <span className="text-slate-200">{count.word_canonical}</span>
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
