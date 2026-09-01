import { useQuery } from "@tanstack/react-query";
import { apiGet } from "../api/client";
import { Page, Placeholder } from "../components/Page";

/** Read-only in M0: proves settings persist. The editor with Test buttons is M4. */
export function SettingsPage() {
  const { data } = useQuery({
    queryKey: ["settings"],
    queryFn: () => apiGet<Record<string, unknown>>("/settings"),
  });

  return (
    <Page title="Settings" subtitle="Integrations, STT models, codec policy and retention.">
      <div className="max-w-2xl space-y-6">
        {data && (
          <div className="rounded border border-slate-800 bg-slate-900/40 p-4 text-sm">
            <h2 className="mb-3 text-sm font-medium tracking-wide text-slate-300 uppercase">
              Current values
            </h2>
            <dl className="grid grid-cols-2 gap-x-6 gap-y-1">
              {Object.entries(data).map(([key, value]) => (
                <div key={key} className="contents">
                  <dt className="truncate text-slate-400">{key}</dt>
                  <dd className="truncate text-slate-200">{String(value) || "—"}</dd>
                </div>
              ))}
            </dl>
          </div>
        )}
        <Placeholder milestone="M4">
          Editable form with Test buttons for Sonarr/Radarr/Jellyfin, webhook setup, and path
          mappings.
        </Placeholder>
      </div>
    </Page>
  );
}
