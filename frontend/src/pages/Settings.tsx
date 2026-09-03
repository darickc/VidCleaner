/**
 * PLAN.md §9.6 — integrations with Test buttons, webhook setup, path mappings, and
 * the operational settings.
 *
 * The fields are described by data rather than written out as JSX: `/api/settings` is
 * a flat bag of scalars validated by pydantic, so the form's job is to name them,
 * group them and send back **only what changed**. Sending the whole object would
 * write `***` over a stored API key the form never saw.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import {
  getPathMappings,
  getSettings,
  getWebhookSetup,
  installWebhook,
  patchSettings,
  putPathMappings,
  testIntegration,
} from "../api/client";
import type { AppSettings, PathMapping, TestResponse } from "../api/types";
import { Page } from "../components/Page";
import { Badge, Button, Card, Empty, ErrorNote } from "../components/ui";

type FieldKind = "text" | "password" | "number" | "bool" | "choice";

interface Field {
  key: string;
  label: string;
  kind: FieldKind;
  help?: string;
  choices?: string[];
}

const SECTIONS: Array<{ title: string; fields: Field[] }> = [
  {
    title: "Speech to text",
    fields: [
      { key: "stt_windowed_model", label: "Windowed model", kind: "text", help: "used when subtitles narrow the search" },
      { key: "stt_full_model", label: "Full-file model", kind: "text" },
      { key: "stt_drift_model", label: "Drift probe model", kind: "text" },
      { key: "stt_full_max_hours", label: "Full-pass limit (hours)", kind: "number", help: "0 = no limit" },
      { key: "cpu_threads", label: "CPU threads", kind: "number", help: "0 = cores − 2" },
      { key: "beam_size", label: "Beam size", kind: "number" },
      { key: "preferred_language", label: "Preferred language", kind: "text" },
      { key: "initial_prompt_hint", label: "Profanity prompt hint", kind: "bool" },
      { key: "vad_filter", label: "Silero VAD (full passes only)", kind: "bool", help: "measured to cost 5× recall — see docs/eval.md" },
      { key: "drift_check", label: "Subtitle drift check", kind: "bool" },
    ],
  },
  {
    title: "Detection & muting",
    fields: [
      { key: "pad_pre_ms", label: "Pad before (ms)", kind: "number" },
      { key: "pad_post_ms", label: "Pad after (ms)", kind: "number" },
      { key: "merge_gap_ms", label: "Merge gap (ms)", kind: "number" },
      { key: "fade_edges_ms", label: "Fade edges (ms)", kind: "number" },
      { key: "mute_censored_tokens", label: "Mute censored tokens (f***)", kind: "bool" },
      { key: "redact_subtitles", label: "Redact text subtitles", kind: "bool" },
    ],
  },
  {
    title: "Output",
    fields: [
      { key: "clean_track_lossless", label: "Always use FLAC for the clean track", kind: "bool" },
      { key: "extra_eac3_downmix", label: "Add an EAC3 5.1 downmix", kind: "bool" },
      { key: "allow_cross_device_backup", label: "Allow a copy-and-delete backup", kind: "bool", help: "off means /backups must be on the library's filesystem" },
    ],
  },
  {
    title: "Scheduling & retention",
    fields: [
      { key: "audit_pass", label: "Audit pass", kind: "choice", choices: ["off", "idle", "always"] },
      { key: "backup_retention_days", label: "Keep backups (days)", kind: "number" },
      { key: "mapping_check_delay_s", label: "Mapping re-check delay (s)", kind: "number" },
      { key: "render_parallel", label: "Parallel renders", kind: "number" },
    ],
  },
];

const APPS = [
  { app: "sonarr", label: "Sonarr", url: "sonarr_url", key: "sonarr_api_key", webhook: true },
  { app: "radarr", label: "Radarr", url: "radarr_url", key: "radarr_api_key", webhook: true },
  { app: "jellyfin", label: "Jellyfin", url: "jellyfin_url", key: "jellyfin_api_key", webhook: false },
];

function Input({
  field,
  value,
  onChange,
}: {
  field: Field;
  value: string | number | boolean | undefined;
  onChange: (value: string | number | boolean) => void;
}) {
  const base =
    "w-full rounded border border-slate-800 bg-slate-900/60 px-2 py-1 text-sm outline-none focus:border-slate-600";
  if (field.kind === "bool") {
    return (
      <input
        type="checkbox"
        id={field.key}
        checked={Boolean(value)}
        onChange={(event) => onChange(event.target.checked)}
        className="h-4 w-4 accent-sky-500"
      />
    );
  }
  if (field.kind === "choice") {
    return (
      <select
        id={field.key}
        value={String(value ?? "")}
        onChange={(event) => onChange(event.target.value)}
        className={base}
      >
        {field.choices?.map((choice) => (
          <option key={choice} value={choice}>
            {choice}
          </option>
        ))}
      </select>
    );
  }
  return (
    <input
      id={field.key}
      type={field.kind === "password" ? "password" : field.kind === "number" ? "number" : "text"}
      value={String(value ?? "")}
      onChange={(event) =>
        onChange(field.kind === "number" ? Number(event.target.value) : event.target.value)
      }
      className={base}
    />
  );
}

function FieldRow({
  field,
  value,
  onChange,
}: {
  field: Field;
  value: string | number | boolean | undefined;
  onChange: (value: string | number | boolean) => void;
}) {
  return (
    <div className="grid grid-cols-[14rem_minmax(0,1fr)] items-center gap-3 py-1">
      <label htmlFor={field.key} className="text-sm text-slate-400">
        {field.label}
        {field.help && <span className="block text-xs text-slate-600">{field.help}</span>}
      </label>
      <Input field={field} value={value} onChange={onChange} />
    </div>
  );
}

function WebhookPanel({ app, label }: { app: string; label: string }) {
  const { data } = useQuery({ queryKey: ["webhook-setup", app], queryFn: () => getWebhookSetup(app) });
  const install = useMutation({ mutationFn: () => installWebhook(app) });

  if (!data) return null;
  return (
    <div className="space-y-1 border-t border-slate-800 pt-3 text-sm">
      <div className="text-slate-400">{label} webhook</div>
      <code className="block truncate rounded bg-slate-950/70 px-2 py-1 text-xs text-slate-300">
        {data.url}
      </code>
      <code className="block truncate rounded bg-slate-950/70 px-2 py-1 text-xs text-slate-300">
        {data.header_name}: {data.token}
      </code>
      <div className="flex items-center gap-2 pt-1">
        <Button onClick={() => install.mutate()} disabled={install.isPending}>
          Add to {label}
        </Button>
        {install.data && (
          <span className="text-xs text-slate-400">
            {install.data.created ? "created" : "already present"}
          </span>
        )}
        <ErrorNote error={install.error} />
      </div>
    </div>
  );
}

function PathMappings() {
  const client = useQueryClient();
  const { data } = useQuery({ queryKey: ["path-mappings"], queryFn: getPathMappings });
  const [rows, setRows] = useState<PathMapping[] | null>(null);
  useEffect(() => {
    if (data) setRows(data);
  }, [data]);

  const save = useMutation({
    mutationFn: (mappings: PathMapping[]) => putPathMappings(mappings),
    onSuccess: (saved) => {
      setRows(saved);
      client.invalidateQueries({ queryKey: ["path-mappings"] });
    },
  });

  const current = rows ?? [];
  const update = (index: number, patch: Partial<PathMapping>) =>
    setRows(current.map((row, i) => (i === index ? { ...row, ...patch } : row)));

  return (
    <Card
      title="Path mappings"
      actions={
        <Button onClick={() => save.mutate(current)} disabled={save.isPending}>
          Save mappings
        </Button>
      }
    >
      <p className="mb-2 text-xs text-slate-500">
        Empty means identical paths everywhere (§2). “Their path” is what the app reports; “our
        path” is where we see the same file.
      </p>
      {current.length === 0 && <Empty>No mappings — paths are identical.</Empty>}
      {current.map((row, index) => (
        <div key={index} className="mb-2 grid grid-cols-[7rem_minmax(0,1fr)_minmax(0,1fr)_3rem] gap-2">
          <select
            value={row.app}
            aria-label={`App for mapping ${index + 1}`}
            onChange={(event) => update(index, { app: event.target.value })}
            className="rounded border border-slate-800 bg-slate-900/60 px-2 py-1 text-sm"
          >
            {["sonarr", "radarr", "jellyfin"].map((app) => (
              <option key={app}>{app}</option>
            ))}
          </select>
          <input
            value={row.from_prefix}
            aria-label={`Their path ${index + 1}`}
            placeholder="/tv"
            onChange={(event) => update(index, { from_prefix: event.target.value })}
            className="rounded border border-slate-800 bg-slate-900/60 px-2 py-1 text-sm"
          />
          <input
            value={row.to_prefix}
            aria-label={`Our path ${index + 1}`}
            placeholder="/media/tv"
            onChange={(event) => update(index, { to_prefix: event.target.value })}
            className="rounded border border-slate-800 bg-slate-900/60 px-2 py-1 text-sm"
          />
          <button
            type="button"
            onClick={() => setRows(current.filter((_, i) => i !== index))}
            className="text-xs text-slate-600 hover:text-rose-300"
            aria-label={`Remove mapping ${index + 1}`}
          >
            remove
          </button>
        </div>
      ))}
      <Button
        onClick={() => setRows([...current, { app: "sonarr", from_prefix: "", to_prefix: "" }])}
      >
        Add mapping
      </Button>
      <ErrorNote error={save.error} />
    </Card>
  );
}

export function SettingsPage() {
  const client = useQueryClient();
  const { data, isPending, isError } = useQuery({ queryKey: ["settings"], queryFn: getSettings });
  const [draft, setDraft] = useState<Partial<AppSettings>>({});
  const [tests, setTests] = useState<Record<string, TestResponse>>({});
  const [saved, setSaved] = useState(false);

  const value = (key: string) => (key in draft ? draft[key] : data?.[key]);
  const set = (key: string, next: string | number | boolean) => {
    setSaved(false);
    setDraft((current) => ({ ...current, [key]: next }));
  };
  const dirty = Object.keys(draft).length > 0;

  const save = useMutation({
    mutationFn: (patch: Partial<AppSettings>) => patchSettings(patch),
    onSuccess: () => {
      setDraft({});
      setSaved(true);
      client.invalidateQueries({ queryKey: ["settings"] });
    },
  });

  const test = useMutation({
    // Test the values on screen, not the stored ones: the point is to check a key
    // before committing to it. A field left untouched falls through to the stored
    // value on the server.
    mutationFn: (app: (typeof APPS)[number]) =>
      testIntegration(app.app, {
        url: String(value(app.url) ?? ""),
        api_key: String(value(app.key) ?? ""),
      }),
    onSuccess: (result) => setTests((current) => ({ ...current, [result.app]: result })),
  });

  if (isError) return <Page title="Settings">Could not load settings.</Page>;
  if (isPending || !data) return <Page title="Settings">Loading…</Page>;

  return (
    <Page title="Settings" subtitle="Integrations, STT models, codec policy and retention.">
      <div className="max-w-3xl space-y-6">
        <div className="flex items-center gap-3">
          <Button
            variant="primary"
            disabled={!dirty || save.isPending}
            onClick={() => save.mutate(draft)}
          >
            {save.isPending ? "Saving…" : "Save changes"}
          </Button>
          {dirty && <span className="text-xs text-amber-300">unsaved changes</span>}
          {saved && !dirty && <span className="text-xs text-emerald-300">Saved.</span>}
          <ErrorNote error={save.error} />
        </div>

        <Card title="Integrations">
          {APPS.map((app) => (
            <div key={app.app} className="mb-5 space-y-2 last:mb-0">
              <div className="flex items-center gap-2">
                <h3 className="text-sm font-medium text-slate-200">{app.label}</h3>
                {tests[app.app] && (
                  <Badge tone={tests[app.app].ok ? "ok" : "bad"}>
                    {tests[app.app].ok
                      ? `ok · ${tests[app.app].version ?? "connected"} · ${tests[app.app].latency_ms}ms`
                      : tests[app.app].detail || "failed"}
                  </Badge>
                )}
              </div>
              <FieldRow
                field={{ key: app.url, label: `${app.label} URL`, kind: "text" }}
                value={value(app.url)}
                onChange={(next) => set(app.url, next)}
              />
              <FieldRow
                field={{
                  key: app.key,
                  label: `${app.label} API key`,
                  kind: "password",
                  help: data[app.key] === "***" ? "a key is stored; type to replace it" : undefined,
                }}
                value={value(app.key)}
                onChange={(next) => set(app.key, next)}
              />
              <Button onClick={() => test.mutate(app)} disabled={test.isPending}>
                Test {app.label}
              </Button>
              {app.webhook && <WebhookPanel app={app.app} label={app.label} />}
            </div>
          ))}
          <ErrorNote error={test.error} />
        </Card>

        <PathMappings />

        {SECTIONS.map((section) => (
          <Card key={section.title} title={section.title}>
            {section.fields.map((field) => (
              <FieldRow
                key={field.key}
                field={field}
                value={value(field.key)}
                onChange={(next) => set(field.key, next)}
              />
            ))}
          </Card>
        ))}
      </div>
    </Page>
  );
}
